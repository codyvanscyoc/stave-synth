"""Offline pad-stop, panic dispatch, and recording-name regression tests.

Production method bodies/classes are extracted without starting the app.
Recorder tests run its real WAV writer against disposable directories and
synthetic NumPy blocks, never a recording device. No Faust/SciPy is loaded.
This does not qualify the broader concurrent fade/queue/panic lifecycle.
"""

import ast
import json
import logging
import tempfile
import threading
import unittest
import wave
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np


ROOT = Path(__file__).resolve().parents[1]


def class_ast(relative, name):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    return next(node for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == name)


def method(relative, class_name, name, namespace):
    owner = class_ast(relative, class_name)
    body = next(node for node in owner.body
                if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[body], type_ignores=[]), relative, "exec"), namespace)
    return namespace[name]


class FilterSpy:
    def __init__(self, *args):
        self.state = 2.0
        self.reset_count = 0

    def reset(self):
        self.state = 0.0
        self.reset_count += 1

    def set_params(self, *args):
        pass

    def process(self, samples):
        return samples


_sample_namespace = {
    "np": np, "SAMPLE_RATE": 48000, "BiquadLowpass": FilterSpy,
    "logger": logging.getLogger(__name__),
}
exec(compile(ast.Module(body=[class_ast("stave_synth/synth_engine.py", "SamplePlayer")],
                        type_ignores=[]), "stave_synth/synth_engine.py", "exec"),
     _sample_namespace)
SamplePlayer = _sample_namespace["SamplePlayer"]
apply_panic = method("stave_synth/synth_engine.py", "SynthEngine", "_apply_panic",
                     {"ADSREnvelope": SimpleNamespace(OFF=4)})
handle_panic = method("stave_synth/main.py", "StaveSynth", "_handle_panic",
                      {"logger": logging.getLogger(__name__)})


def loaded_player():
    player = SamplePlayer()
    # Prepared sample buffers isolate hard-stop behavior from WAV decoding.
    player.samples_l = np.full(4096, 0.5, dtype=np.float64)
    player.samples_r = np.full(4096, 0.25, dtype=np.float64)
    player.length = 4096
    player.xfade_len = 256
    player.loaded = True
    return player


class SampleHardStopTests(unittest.TestCase):
    def test_hard_stop_resets_playback_rise_and_filters_but_retains_asset(self):
        player = loaded_player()
        left_asset, right_asset = player.samples_l, player.samples_r
        player.trigger(rise_seconds=3.0, rise_cutoff_open=4000.0)
        player.env = 0.7
        player.read_pos = 1024.0
        player._rise_t_samples = 4096
        player._rise_lp_l.state = player._rise_lp_r.state = 5.0
        reset_counts = (player._rise_lp_l.reset_count, player._rise_lp_r.reset_count)
        player.hard_stop()
        self.assertFalse(player.active)
        self.assertEqual(player.env, 0.0)
        self.assertEqual(player.env_target, 0.0)
        self.assertEqual(player.read_pos, 0.0)
        self.assertFalse(player._rise_active)
        self.assertFalse(player._rise_filter_engaged)
        self.assertEqual(player._rise_t_samples, 0)
        self.assertEqual(player._rise_lp_l.state, 0.0)
        self.assertEqual(player._rise_lp_r.state, 0.0)
        self.assertEqual(player._rise_lp_l.reset_count, reset_counts[0] + 1)
        self.assertEqual(player._rise_lp_r.reset_count, reset_counts[1] + 1)
        self.assertTrue(player.loaded)
        self.assertIs(player.samples_l, left_asset)
        self.assertIs(player.samples_r, right_asset)
        self.assertEqual(player.length, 4096)
        self.assertEqual(player.xfade_len, 256)
        out_l, out_r = np.ones(128), np.full(128, 2.0)
        player.process(128, out_l, out_r)
        np.testing.assert_array_equal(out_l, np.ones(128))
        np.testing.assert_array_equal(out_r, np.full(128, 2.0))

    def test_stopped_player_retriggers_like_a_fresh_player(self):
        player = loaded_player()
        player.trigger(rise_seconds=4.0)
        player.process(128, np.zeros(128), np.zeros(128))
        player.hard_stop()
        player.hard_stop()  # repeated STOP is harmless
        fresh = loaded_player()
        for rise_seconds in (0.0, 3.0):
            with self.subTest(rise_seconds=rise_seconds):
                player.hard_stop()
                fresh.hard_stop()
                player.trigger(rise_seconds=rise_seconds)
                fresh.trigger(rise_seconds=rise_seconds)
                actual_l, actual_r = np.zeros(128), np.zeros(128)
                expected_l, expected_r = np.zeros(128), np.zeros(128)
                player.process(128, actual_l, actual_r)
                fresh.process(128, expected_l, expected_r)
                np.testing.assert_array_equal(actual_l, expected_l)
                np.testing.assert_array_equal(actual_r, expected_r)
                self.assertTrue(player.active)
                self.assertGreater(float(actual_l.max()), 0.0)

    def test_hard_stop_is_safe_before_any_asset_is_loaded(self):
        player = SamplePlayer()
        player.hard_stop()
        player.trigger()
        self.assertFalse(player.loaded)
        self.assertFalse(player.active)
        self.assertIsNone(player.samples_l)
        self.assertIsNone(player.samples_r)

    def test_apply_panic_stops_every_player_and_clears_mellow_history(self):
        players = {60: loaded_player(), 62: loaded_player(), 64: SamplePlayer()}
        players[60].trigger(rise_seconds=3.0)
        players[62].trigger()
        players[62].release()
        for player in players.values():
            player.hard_stop = Mock(wraps=player.hard_stop)
        voice = SimpleNamespace(adsr_osc1=SimpleNamespace(stage=1, level=0.8),
                                adsr_osc2=SimpleNamespace(stage=2, level=0.7), faust_slot=0)
        engine = SimpleNamespace(
            _render_lock=threading.RLock(), _pad_samples=players, voices=[voice],
            _pitch_bend_semitones=2.0, _drone_fade_scale=0.4,
            _faust_osc_bank=SimpleNamespace(panic=Mock()), _faust_nvoices=12,
            _faust_sympathetic=None, _sympathetic_state={60: {}},
            freeze_enabled=True, reverb=SimpleNamespace(panic=Mock()),
            _faust_ping_pong=SimpleNamespace(clear=Mock()),
            _faust_pad_bus=SimpleNamespace(clear=Mock()),
        )
        filter_names = (
            "filter_l", "filter_r", "filter2_l", "filter2_r", "filter_hp_l", "filter_hp_r",
            "osc1_indep_filter_l", "osc1_indep_filter_r", "osc2_indep_filter_l", "osc2_indep_filter_r",
            "reverb_filter_l", "reverb_filter_r", "reverb_filter2_l", "reverb_filter2_r", "_shimmer_hp",
            "_rev_send_filter_l", "_rev_send_filter_r", "_rev_send_filter2_l", "_rev_send_filter2_r",
            "_pad_mellow_lp_l", "_pad_mellow_lp_r",
        )
        for name in filter_names:
            setattr(engine, name, FilterSpy())
        for name in ("_haas_buf_l", "_haas_buf_r", "_shimmer_delay_l", "_shimmer_delay_r",
                     "_delay_buf_l", "_delay_buf_r"):
            setattr(engine, name, np.ones(8))
        with engine._render_lock:
            apply_panic(engine)
        self.assertIs(engine._pad_samples, players)
        for player in players.values():
            player.hard_stop.assert_called_once_with()
            self.assertFalse(player.active)
        for name in ("_pad_mellow_lp_l", "_pad_mellow_lp_r"):
            self.assertEqual(getattr(engine, name).state, 0.0)
            self.assertEqual(getattr(engine, name).reset_count, 1)
        self.assertEqual(engine._drone_fade_scale, 1.0)
        self.assertEqual(engine._pitch_bend_semitones, 0.0)
        self.assertFalse(engine.freeze_enabled)
        self.assertEqual(voice.adsr_osc1.stage, 4)
        self.assertEqual(voice.adsr_osc2.stage, 4)
        engine._faust_osc_bank.panic.assert_called_once_with()
        engine.reverb.panic.assert_called_once_with()
        engine._faust_ping_pong.clear.assert_called_once_with()
        engine._faust_pad_bus.clear.assert_called_once_with()


class MainPanicDispatchTests(unittest.TestCase):
    def app(self, native_organ=True, drone_cancel=True):
        app = SimpleNamespace(
            _crossfade_cancel=threading.Event(), synth=SimpleNamespace(panic=Mock()),
            piano=SimpleNamespace(all_notes_off=Mock()), organ=SimpleNamespace(all_notes_off=Mock()),
            jack=SimpleNamespace(panic=Mock(), fade_reset=Mock()),
            midi=SimpleNamespace(all_notes_off=Mock()),
            state={"synth_pad": {"freeze_enabled": True, "drone_enabled": True,
                                 "drone_key": 60, "osc1_blend": 0.42}},
        )
        if native_organ:
            app.organ.hard_panic = Mock()
        if drone_cancel:
            app._drone_fade_cancel = threading.Event()
        return app

    def test_panic_cancels_both_fades_before_dispatch_and_requests_native_hard_stop(self):
        app = self.app()

        def check_cancelled_before_synth_panic():
            self.assertTrue(app._crossfade_cancel.is_set())
            self.assertTrue(app._drone_fade_cancel.is_set())

        app.synth.panic.side_effect = check_cancelled_before_synth_panic
        result = handle_panic(app)
        self.assertEqual(result, {"type": "panic_ack", "fade_reset": True})
        app.synth.panic.assert_called_once_with()
        app.piano.all_notes_off.assert_called_once_with()
        app.organ.hard_panic.assert_called_once_with()
        app.organ.all_notes_off.assert_not_called()
        app.jack.panic.assert_called_once_with()
        app.jack.fade_reset.assert_called_once_with()
        app.midi.all_notes_off.assert_called_once_with()
        self.assertFalse(app.state["synth_pad"]["freeze_enabled"])
        self.assertFalse(app.state["synth_pad"]["drone_enabled"])
        self.assertIsNone(app.state["synth_pad"]["drone_key"])
        self.assertEqual(app.state["synth_pad"]["osc1_blend"], 0.42)

    def test_fallback_organ_and_missing_drone_fade_handle_repeated_panic(self):
        app = self.app(native_organ=False, drone_cancel=False)
        handle_panic(app)
        handle_panic(app)
        self.assertTrue(app._crossfade_cancel.is_set())
        self.assertEqual(app.organ.all_notes_off.call_count, 2)
        self.assertEqual(app.synth.panic.call_count, 2)


def recorder_namespace(data_dir):
    # Execute the actual recorder/helper modules without package/config side
    # effects. DATA_DIR is explicit before module execution/Recorder creation.
    relative = "stave_synth/recorder.py"
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body
                 if not (isinstance(node, ast.ImportFrom) and node.level == 1
                         and node.module in {"config", "state_store"})]
    namespace = {"__name__": "isolated_recording_test", "DATA_DIR": data_dir, "SAMPLE_RATE": 48000}
    store_namespace = {"__name__": "isolated_recording_state_store"}
    store_path = ROOT / "stave_synth/state_store.py"
    exec(compile(store_path.read_text(encoding="utf-8"), str(store_path), "exec"), store_namespace)
    namespace["atomic_write_json"] = store_namespace["atomic_write_json"]
    exec(compile(tree, relative, "exec"), namespace)

    class FixedDatetime:
        @staticmethod
        def now():
            return datetime(2026, 9, 14, 12, 34, 56)

    namespace["datetime"] = FixedDatetime
    return namespace


class RecordingFilenameTests(unittest.TestCase):
    def existing_take(self, recorder, state=None):
        if state is None:
            state = {"take": "preserved"}
        take = recorder.start(state)
        recorder.feed(np.full(16, 0.25, dtype=np.float32), np.full(16, -0.25, dtype=np.float32))
        recorder.stop()
        path = Path(take["path"])
        return path, path.read_bytes(), path.with_suffix(".state.json").read_bytes()

    def assert_failed_start_preserved_existing(self, recorder, saved):
        path, wav_data, state_data = saved
        self.assertIsNone(recorder._take)
        self.assertFalse(recorder.is_recording())
        self.assertEqual(path.read_bytes(), wav_data)
        self.assertEqual(path.with_suffix(".state.json").read_bytes(), state_data)
        self.assertEqual(set(path.parent.iterdir()),
                         {path, path.with_suffix(".state.json"), path.with_suffix(".meta.json")})

    def test_open_failure_removes_only_new_reserved_file_and_allows_retry(self):
        with tempfile.TemporaryDirectory(prefix="stave-recording-failure-") as temporary:
            namespace = recorder_namespace(Path(temporary))
            recorder = namespace["Recorder"]()
            saved = self.existing_take(recorder)
            failure = OSError("simulated WAV open failure")
            with patch.object(namespace["wave"], "open", side_effect=failure):
                with self.assertRaises(OSError) as caught:
                    recorder.start({"take": "failed"})
            self.assertIs(caught.exception, failure)
            self.assert_failed_start_preserved_existing(recorder, saved)
            retry = recorder.start({"take": "retry"})
            recorder.stop()
            self.assertNotEqual(Path(retry["path"]), saved[0])
            self.assertEqual(saved[0].read_bytes(), saved[1])

    def test_each_header_setter_failure_closes_handle_and_preserves_previous_take(self):
        for setter in ("setnchannels", "setsampwidth", "setframerate"):
            with self.subTest(setter=setter), tempfile.TemporaryDirectory(
                    prefix="stave-recording-header-failure-") as temporary:
                namespace = recorder_namespace(Path(temporary))
                recorder = namespace["Recorder"]()
                saved = self.existing_take(recorder)
                failure = ValueError(f"simulated {setter} failure")
                real_open = namespace["wave"].open
                handles = []

                def open_with_bad_header(*args, **kwargs):
                    handle = real_open(*args, **kwargs)
                    setattr(handle, setter, Mock(side_effect=failure))
                    handle.close = Mock(wraps=handle.close)
                    handles.append(handle)
                    return handle

                with patch.object(namespace["wave"], "open", side_effect=open_with_bad_header):
                    # wave.close may itself reject the incomplete header but
                    # its finally block still closes the actual file handle.
                    with self.assertLogs("isolated_recording_test", level="WARNING"):
                        with self.assertRaises(ValueError) as caught:
                            recorder.start({"take": "failed"})
                self.assertIs(caught.exception, failure)
                self.assertEqual(len(handles), 1)
                handles[0].close.assert_called_once_with()
                self.assertIsNone(handles[0]._file)
                self.assert_failed_start_preserved_existing(recorder, saved)

    def test_secondary_close_error_does_not_skip_owned_file_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="stave-recording-close-failure-") as temporary:
            namespace = recorder_namespace(Path(temporary))
            recorder = namespace["Recorder"]()
            saved = self.existing_take(recorder)
            failure = ValueError("primary header failure")
            handle = SimpleNamespace(
                setnchannels=Mock(side_effect=failure),
                close=Mock(side_effect=OSError("secondary close failure")),
            )
            with patch.object(namespace["wave"], "open", return_value=handle):
                with self.assertLogs("isolated_recording_test", level="WARNING"):
                    with self.assertRaises(ValueError) as caught:
                        recorder.start({"take": "failed"})
            self.assertIs(caught.exception, failure)
            handle.close.assert_called_once_with()
            self.assert_failed_start_preserved_existing(recorder, saved)

    def test_same_second_takes_keep_distinct_wavs_and_state_sidecars(self):
        for automatic_stop in (False, True):
            with self.subTest(automatic_stop=automatic_stop), tempfile.TemporaryDirectory(
                    prefix="stave-recording-test-") as temporary:
                data_dir = Path(temporary)
                namespace = recorder_namespace(data_dir)
                recorder = namespace["Recorder"]()
                first_state = {"take": 1, "synth_pad": {"osc1_blend": 0.25}}
                second_state = {"take": 2, "synth_pad": {"osc1_blend": 0.75}}
                first_block = np.full(128, 0.25, dtype=np.float32)
                second_block = np.full(128, -0.5, dtype=np.float32)
                try:
                    first = recorder.start(first_state)
                    recorder.feed(first_block, first_block)
                    if not automatic_stop:
                        recorder.stop()
                    second = recorder.start(second_state)
                    recorder.feed(second_block, second_block)
                    recorder.stop()
                finally:
                    recorder.stop()

                first_path, second_path = Path(first["path"]), Path(second["path"])
                self.assertEqual(first["started_at"], second["started_at"])
                self.assertNotEqual(first_path, second_path)
                self.assertEqual(first_path.parent, data_dir / "recordings")
                self.assertEqual(second_path.parent, data_dir / "recordings")
                self.assertEqual(len(list((data_dir / "recordings").glob("*.wav"))), 2)
                self.assertEqual(len(list((data_dir / "recordings").glob("*.state.json"))), 2)
                for path, block, state in ((first_path, first_block, first_state),
                                           (second_path, second_block, second_state)):
                    self.assertTrue(path.name.startswith("2026-09-14_12-34-56"))
                    with wave.open(str(path), "rb") as recorded:
                        self.assertEqual(recorded.getnchannels(), 2)
                        self.assertEqual(recorded.getsampwidth(), 2)
                        self.assertEqual(recorded.getframerate(), 48000)
                        self.assertEqual(recorded.getnframes(), 128)
                        pcm = np.frombuffer(recorded.readframes(128), dtype="<i2")
                    expected = (np.repeat(block, 2) * 32767.0).astype(np.int16)
                    np.testing.assert_array_equal(pcm, expected)
                    self.assertEqual(json.loads(path.with_suffix(".state.json").read_text()), state)
                    self.assertEqual(namespace["Recorder"].load_state_snapshot(path.name), state)
                self.assertEqual(len(namespace["Recorder"].list_takes()), 2)


if __name__ == "__main__":
    unittest.main()
