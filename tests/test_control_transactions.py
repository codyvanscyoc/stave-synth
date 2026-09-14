"""Exact main-controller methods with device objects mocked; no app imports."""

import ast
import copy
import logging
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from stave_synth.config import DEFAULT_STATE
from stave_synth.control_queue import ControlQueue
from stave_synth.control_mapping import macro_commands
from stave_synth.state_schema import GLOBAL_MASTER_KEYS, ValidationError, normalize_state, validate_message, validate_setting
from stave_synth.preset_manager import PresetManager


def controller():
    path = Path(__file__).resolve().parents[1] / "stave_synth" / "main.py"
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "StaveSynth")
    namespace = {"copy": copy, "threading": threading, "time": time, "Path": Path,
                 "logger": logging.getLogger(__name__), "validate_message": validate_message,
                 "validate_setting": validate_setting, "normalize_state": normalize_state,
                 "ValidationError": ValidationError, "GLOBAL_MASTER_KEYS": GLOBAL_MASTER_KEYS,
                 "macro_commands": macro_commands, "DEFAULT_STATE": DEFAULT_STATE,
                 "save_state": Mock(), "_EQ_BAND_RE": re.compile(r"^eq_band(\d+)_(freq|gain|q|enabled)$")}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), "exec"), namespace)
    app = namespace["StaveSynth"].__new__(namespace["StaveSynth"])
    app.state = normalize_state({})
    app._control_lock = threading.RLock()
    app._ui_lifecycle_lock = threading.Lock()
    app._stopping = False
    app._running = True
    app._control_error = None
    app._stop_event = threading.Event()
    app._crossfade_cancel = threading.Event()
    app._crossfade_thread = None
    app._midi_learn_lock = threading.Lock()
    app._midi_learn_active = False
    app._midi_learn_target = None
    app._cc_last_value = {}
    app._cc_map = {}
    app._controls = ControlQueue(app._apply_queued_control)
    app.synth = Mock(voices=[])
    app.piano = Mock()
    app.organ = Mock()
    app.midi = Mock()
    app.jack = Mock()
    app.ws_server = Mock(stop=Mock(return_value=True))
    app.instrument_mode = "piano"
    app._start_preset_crossfade = Mock()
    return app, namespace


class ControlTransactionTests(unittest.TestCase):
    def test_get_state_uses_jack_recorder_owner_and_detaches_hydration(self):
        for has_jack in (True, False):
            app, namespace = controller()
            namespace["FluidSynthPlayer"] = SimpleNamespace(list_available_soundfonts=lambda: [])
            namespace["LOW_RAM_MODE"] = True
            app._health_status = Mock(return_value={"healthy": True})
            app.synth.reverb.available_types.return_value = {}
            status = {"recording": False, "writer_pending": True, "status": "stopping"}
            recorder = SimpleNamespace(current_status=lambda: status)
            app.jack = SimpleNamespace(_fade_target=1.0, recorder=recorder) if has_jack else None
            self.assertFalse(hasattr(app, "recorder"))
            result = app._handle_ws_message({"type": "get_state"})
            self.assertEqual(result["type"], "state")
            self.assertEqual(result["record_status"]["status"], "stopping" if has_jack else "idle")
            if has_jack:
                status["status"] = "complete"
                self.assertEqual(result["record_status"]["status"], "stopping")

    def test_malformed_message_never_mutates_or_dispatches(self):
        for bad in (float("nan"), float("inf"), [], {}, "false"):
            app, _ = controller()
            before = copy.deepcopy(app.state)
            result = app._handle_ws_message({"type": "setting", "section": "master", "param": "volume", "value": bad})
            self.assertEqual(result["type"], "error")
            self.assertEqual(app.state, before)
            app.synth.update_params.assert_not_called()

    def test_macro_applies_on_server_with_zero_or_multiple_clients_once(self):
        for clients in (None, Mock(clients={1, 2})):
            app, _ = controller()
            app.ws_server = clients
            app.state["macros"][0]["assignments"] = [
                {"kind": "fader", "fader_id": 4, "fader_alt": 0, "min": 0, "max": 1},
                {"kind": "param", "section": "piano", "param": "comp_wet", "min": 0, "max": 100},
            ]
            result = app._handle_ws_message({"type": "macro_value", "idx": 0, "value": .25})
            self.assertEqual(result["type"], "macro_value_ack")
            self.assertEqual(app.jack.master_volume, .25)
            self.assertEqual(app.state["piano"]["comp_wet"], .25)
            app.piano.update_params.assert_called_once_with({"comp_wet": .25})

    def test_midi_macro_changes_audio_without_browser(self):
        app, _ = controller()
        app.ws_server = None
        app._cc_map = {"1": {"kind": "macro", "macro_idx": 0}}
        app.state["macros"][0]["assignments"] = [{"kind": "fader", "fader_id": 4, "min": 0, "max": 1}]
        app._apply_cc(1, 127)
        self.assertEqual(app.jack.master_volume, 1)

    def test_latency_failure_does_not_ack_or_store_request(self):
        app, _ = controller()
        old = app.state["master"]["low_latency_mode"]
        app.jack.set_low_latency_mode.return_value = False
        result = app._handle_ws_message({"type": "setting", "section": "master", "param": "low_latency_mode", "value": not old})
        self.assertEqual(result["type"], "error")
        self.assertEqual(app.state["master"]["low_latency_mode"], old)

    def test_scene_preserves_global_control_policy_and_independent_bed(self):
        app, _ = controller()
        app.state["master"].update(audio_output_pref="stage-dac", pitch_bend_enabled=False, midi_clock_enabled=True)
        app.state["synth_pad"].update(drone_key=60, drone_enabled=True)
        app.state["ui"]["preset_labels"][0] = "keep"
        app.state["midi_cc_map"] = {"1": {"kind": "macro", "macro_idx": 0}}
        previous = copy.deepcopy(app.state)
        app._begin_scene({"master": {"volume": .2}})
        for key in GLOBAL_MASTER_KEYS:
            self.assertEqual(app.state["master"].get(key), previous["master"].get(key))
        for key in ("ui", "midi_cc_map", "setlists"):
            self.assertEqual(app.state[key], previous[key])
        self.assertEqual(app.state["synth_pad"]["drone_key"], 60)
        self.assertTrue(app.state["synth_pad"]["drone_enabled"])

    def test_unavailable_soundfont_rolls_back_before_success(self):
        app, _ = controller()
        previous = copy.deepcopy(app.state)
        def piano_apply(params):
            if params.get("soundfont") == "Rhodes":
                raise RuntimeError("not preloaded")
        app.piano.update_params.side_effect = piano_apply
        with self.assertRaisesRegex(RuntimeError, "not preloaded"):
            app._begin_scene({"piano": {"soundfont": "Rhodes"}, "synth_pad": {"osc1_waveform": "saw"}})
        self.assertEqual(app.state, previous)
        app._start_preset_crossfade.assert_not_called()

    def test_completion_emits_loaded_only_after_successful_frame(self):
        app, _ = controller()
        old = normalize_state(app.state, scene=True)
        new = copy.deepcopy(old)
        new["master"]["volume"] = .2
        app._run_preset_crossfade(old, new, 1, app._crossfade_cancel, {"type": "preset_loaded", "slot": 3})
        self.assertEqual(app.state["master"]["volume"], .2)
        self.assertEqual(app.ws_server.broadcast_sync.call_args.args[0], {"type": "preset_loaded", "slot": 3})

    def test_same_mode_organ_scene_and_rollback_sync_mixer_filter_switch(self):
        app, _ = controller()
        app.instrument_mode = "organ"
        app.state["master"]["instrument_mode"] = "organ"
        app.state["organ"]["shared_filter_enabled"] = False
        app.jack.organ_filter_enabled = False
        old = normalize_state(app.state, scene=True)
        new = copy.deepcopy(old)
        new["organ"]["shared_filter_enabled"] = True
        app._apply_scene_frame(new, old)
        self.assertTrue(app.jack.organ_filter_enabled)
        app._restore_failed_scene(old, RuntimeError("later frame failed"))
        self.assertFalse(app.jack.organ_filter_enabled)
        self.assertFalse(app.state["organ"]["shared_filter_enabled"])

    def test_cancelled_transition_cannot_overwrite_panic_state(self):
        app, _ = controller()
        old = normalize_state(app.state, scene=True)
        new = copy.deepcopy(old)
        new["synth_pad"]["freeze_enabled"] = True
        app._handle_panic()
        app.synth.update_params.reset_mock()
        app._run_preset_crossfade(old, new, 1, app._crossfade_cancel)
        self.assertFalse(app.state["synth_pad"]["freeze_enabled"])
        app.synth.update_params.assert_not_called()

    def test_frame_failure_rolls_back_and_does_not_emit_loaded(self):
        app, _ = controller()
        old = normalize_state(app.state, scene=True)
        new = copy.deepcopy(old)
        new["piano"]["volume"] = .1
        app.piano.update_params.side_effect = [RuntimeError("native fault"), None]
        app._run_preset_crossfade(old, new, 1, app._crossfade_cancel, {"type": "preset_loaded", "slot": 3})
        self.assertEqual(app.state["piano"]["volume"], old["piano"]["volume"])
        self.assertFalse(any(c.args[0].get("type") == "preset_loaded" for c in app.ws_server.broadcast_sync.call_args_list))

    def test_panic_invalidates_already_dequeued_control(self):
        app, _ = controller()
        app._handle_panic()
        app._apply_program_change = Mock()
        app._apply_queued_control(("program", 1), 0)
        app._apply_program_change.assert_not_called()

    def test_scene_bank_success_does_not_depend_on_redundant_ui_cache_write(self):
        app, namespace = controller()
        namespace["save_state"].side_effect = OSError("config unavailable")
        with tempfile.TemporaryDirectory(prefix="stave-controller-bank-") as temp:
            app.presets = PresetManager(directory=temp)
            result = app._handle_ws_message({"type": "preset_save", "slot": 0})
            self.assertEqual(result["type"], "preset_saved")
            self.assertIsNotNone(app.presets.load_checked(0))
        namespace["save_state"].assert_not_called()

    def test_nonquiescent_audio_is_never_followed_by_piano_disposal(self):
        app, _ = controller()
        app.jack.stop.return_value = False
        with self.assertRaisesRegex(RuntimeError, "Audio workers"):
            app.stop(save_final_state=False)
        app.piano.stop.assert_not_called()

    def test_nonquiescent_control_is_never_followed_by_native_shutdown(self):
        app, _ = controller()
        app.ws_server.stop.return_value = False
        with self.assertRaisesRegex(RuntimeError, "Control workers"):
            app.stop(save_final_state=False)
        app.jack.stop.assert_not_called()
        app.piano.stop.assert_not_called()


class ControlQueueTests(unittest.TestCase):
    def test_stalled_consumer_has_bounded_mailboxes_and_latest_value(self):
        queue = ControlQueue(Mock(), ordered_limit=4)
        for i in range(10000):
            queue.submit(("cc", 1, i), key=("cc", 1))
        for i in range(100):
            queue.submit(("program", i))
        self.assertEqual(queue.status()["pending"], 5)
        self.assertEqual(queue.status()["dropped"], 96)
        self.assertEqual(queue._latest[("cc", 1)][1][-1], 9999)
        self.assertTrue(queue.stop())

    def test_worker_runs_off_submitter_and_preserves_ordered_edges(self):
        seen = []
        done = threading.Event()
        owner = threading.get_ident()
        def handler(event, generation):
            seen.append((event, threading.get_ident()))
            if len(seen) == 2:
                done.set()
        queue = ControlQueue(handler)
        self.addCleanup(queue.stop)
        queue.submit(("cc", 1, 127))
        queue.submit(("cc", 1, 0))
        queue.start()
        self.assertTrue(done.wait(1))
        self.assertEqual([row[0][-1] for row in seen], [127, 0])
        self.assertTrue(all(row[1] != owner for row in seen))

    def test_stop_timeout_retains_single_worker_and_refuses_restart(self):
        entered, release = threading.Event(), threading.Event()
        def handler(*args):
            entered.set()
            release.wait(2)
        queue = ControlQueue(handler)
        queue.start()
        queue.submit(("cc", 1, 127))
        self.assertTrue(entered.wait(1))
        try:
            self.assertFalse(queue.stop(timeout=.01))
            self.assertFalse(queue.submit(("cc", 1, 0)))
            with self.assertRaises(RuntimeError):
                queue.start()
        finally:
            release.set()
            self.assertTrue(queue.stop())


if __name__ == "__main__":
    unittest.main()
