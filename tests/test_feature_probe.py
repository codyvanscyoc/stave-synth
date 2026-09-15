"""Device-free feature-probe orchestration tests: no app/native imports."""

import asyncio
import copy
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tools import run_feature_probe as probe
from tools.run_audition import CASES, PIANO_FAILURE_COUNTERS, parse_runtime_config


def assign(state, section, param, value):
    target = state.setdefault(section, {})
    parts = param.split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = copy.deepcopy(value)


def baseline():
    state = {"master": {"split_octave_snapshot": None}, "piano": {"soundfont": "Fluid"},
             "synth_pad": {"osc1_octave": 0, "osc2_octave": 0, "shimmer_high": False,
                           "freeze_enabled": False, "drone_enabled": False,
                           "drone_key": None, "drone_level": 0.83}}
    for case in CASES:
        for command in probe.case_commands(case, state):
            assign(state, command["section"], command["param"], command["value"])
    state["master"]["volume"] = 0.91
    state["synth_pad"]["reverb_type"] = "wash"
    return state


def completed_take():
    frames = 6 * 48000
    return {"metadata_version": 1, "filename": "feature-take.wav", "sample_rate": 48000,
            "frames": frames, "input_frames": frames, "accepted_frames": frames,
            "dropped_blocks": 0, "dropped_frames": 0, "write_errors": 0, "stop_timeouts": 0,
            "recording": False, "writer_pending": False, "status": "complete", "complete": True,
            "finalized": True, "audio_synced": True, "metadata_saved": True, "state_saved": True,
            "error": None, "errors": [], "duration_seconds": 6.0,
            "size_bytes": 44 + frames * 4, "has_state": True}


class FakeApplication:
    def __init__(self):
        self.state = baseline()
        self.original = copy.deepcopy(self.state)
        self.health = {"native_profile": {"ready": True}, "audio": {
            "sample_rate": 48000, "block_frames": 512, "graph_error": 0,
            "piano_midi": {"available": True, "recovery_pending": False,
                           "pending": 0, "queued": 0, **dict.fromkeys(PIANO_FAILURE_COUNTERS, 0)}}}
        self.record_status = {"recording": False, "writer_pending": False, "status": "idle"}
        self.take = completed_take()
        self.saved = False
        self.active = False
        self.faded_out = False
        self.messages = []
        self.connections = 0
        self.toggle_count = 0
        self.bad_take = False
        self.truncated = False
        self.raced_slot = False
        self.hydration_bad = False
        self.fail_record_off = False
        self.library = []

    def slots(self):
        return {"type": "pad_slots", "slots": [
            {"note": note, "loaded": self.saved if note == 60 else False,
             "file_present": self.saved if note == 60 else False,
             "active": self.active if note == 60 else False, "preparing": False,
             "error": None, "duration_seconds": 6.0 if self.saved else 0,
             "memory_bytes": 48000 if self.saved else 0} for note in range(60, 72)]}

    def dispatch(self, message, connection):
        self.messages.append(copy.deepcopy(message))
        kind = message["type"]
        if kind == "get_state":
            return {"type": "state", "state": copy.deepcopy(self.state),
                    "health": copy.deepcopy(self.health), "record_status": copy.deepcopy(self.record_status),
                    "faded_out": False,
                    "drone_faded_out": False if self.hydration_bad and connection > 1 else self.faded_out}
        if kind == "debug":
            return {"type": "debug"}
        if kind == "setting":
            assign(self.state, message["section"], message["param"], message["value"])
            return {**message, "type": "setting_ack"}
        if kind == "panic":
            self.active = self.faded_out = False
            self.state["synth_pad"].update(drone_enabled=False, drone_key=None, freeze_enabled=False)
            return {"type": "panic_ack", "fade_reset": True}
        if kind == "record_toggle":
            self.toggle_count += 1
            if self.toggle_count == 1:
                self.record_status = {**self.take, "recording": True, "writer_pending": True, "status": "recording"}
            else:
                self.record_status = copy.deepcopy(self.take)
                if self.bad_take:
                    self.record_status.update(status="incomplete", complete=False, dropped_frames=512)
                self.library.append(copy.deepcopy(self.record_status))
                if self.truncated:
                    self.library[-1]["size_bytes"] = 100
                if self.fail_record_off:
                    return {"type": "error", "message": "stop ack unavailable"}
            return {"type": "record_ack", "recording": self.record_status["recording"],
                    "take": copy.deepcopy(self.record_status), "status": copy.deepcopy(self.record_status)}
        if kind == "list_recordings":
            return {"type": "recordings_list", "takes": copy.deepcopy(self.library)}
        if kind == "list_pad_slots":
            if self.raced_slot and self.toggle_count:
                self.saved = True
            return self.slots()
        if kind == "save_to_pad_slot":
            self.saved = True
            return {**self.slots(), "type": "pad_slot_saved", "note": 60, "warnings": []}
        if kind == "drone_key":
            self.active = True
            self.state["synth_pad"].update(drone_enabled=True, drone_key=60)
            return {"type": "drone_key_ack", "note": 60, "enabled": True, "source": "sample"}
        if kind == "drone_fade":
            self.faded_out = message.get("faded_out", not self.faded_out)
            return {"type": "drone_fade_ack", "faded_out": self.faded_out}
        raise AssertionError(f"unexpected command {message}")

    def connect(self, url, **kwargs):
        if url != probe.WS_URL:
            raise AssertionError("non-isolated WebSocket endpoint")
        self.connections += 1
        return FakeConnection(self, self.connections)


class FakeConnection:
    def __init__(self, app, connection):
        self.app, self.connection = app, connection
        self.incoming = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, payload):
        reply = self.app.dispatch(json.loads(payload), self.connection)
        # The real dispatcher must ignore this unsolicited, non-authoritative state.
        await self.incoming.put('{"type":"state","state":{"stale":true}}')
        await self.incoming.put(json.dumps(reply))

    async def recv(self):
        return await self.incoming.get()


class FeatureProbeTests(unittest.TestCase):
    def run_mock(self, app=None, *, bad_identity=False, driver_failure=False, late_window=False):
        app = app or FakeApplication()
        runtime_calls = []

        async def runtime():
            runtime_calls.append(len(app.messages))
            instance = "stage" if bad_identity else "audition"
            return parse_runtime_config('window.STAVE_RUNTIME = ' + json.dumps(
                {"websocket_port": 18765, "instance": instance}) + ';')

        async def driver(*args, automation):
            if driver_failure == "cancel":
                raise asyncio.CancelledError()
            if driver_failure:
                raise RuntimeError("mock driver refused")
            await automation(args[3], args[4], asyncio.get_running_loop().time() - (30 if late_window else 0))
            return {"returncode": 0, "events": {"capture_complete": {}}, "sweep_timeline": []}

        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "evidence"
            args = SimpleNamespace(driver=sys.executable, output_dir=str(output_dir))
            with patch.object(probe, "verify_runtime", side_effect=runtime), \
                    patch.object(probe.websockets, "connect", side_effect=app.connect), \
                    patch.object(probe, "run_driver", side_effect=driver), \
                    patch.object(probe.Probe, "at", new=probe.Probe.at if late_window else AsyncMock()), \
                    patch.object(probe.asyncio, "sleep", new=AsyncMock()), \
                    patch.object(probe, "validate_capture", return_value={"all_samples_finite": True}):
                try:
                    code = asyncio.run(probe.run(args))
                except asyncio.CancelledError:
                    code = "cancelled"
            report = json.loads((output_dir / "feature-probe.result.json").read_text())
            original_exists = (output_dir / "00-original-state.json").exists()
        return code, report, app, runtime_calls, original_exists

    def test_complete_protocol_restores_state_and_retains_new_evidence(self):
        code, report, app, runtime_calls, original_exists = self.run_mock()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(app.toggle_count, 2)
        self.assertEqual(app.connections, 2)
        self.assertEqual(app.state, app.original)
        self.assertTrue(app.saved)
        self.assertFalse(app.active)
        self.assertFalse(app.faded_out)
        self.assertTrue(original_exists)
        self.assertEqual(len(runtime_calls), 3)
        first_mutation = next(i for i, message in enumerate(app.messages)
                              if message["type"] not in {"get_state", "list_pad_slots", "list_recordings"})
        self.assertEqual(runtime_calls[1], first_mutation)
        kinds = [message["type"] for message in app.messages]
        self.assertNotIn("delete_recording", kinds)
        self.assertNotIn("clear_pad_slot", kinds)
        self.assertEqual(kinds.count("save_to_pad_slot"), 1)
        fades = [message for message in app.messages if message["type"] == "drone_fade"]
        self.assertNotIn("faded_out", fades[0])
        self.assertNotIn("faded_out", fades[1])
        self.assertIs(fades[2]["faded_out"], True)
        self.assertIs(fades[3]["faded_out"], False)
        self.assertTrue(report["completed_take"]["complete"])
        self.assertIn("before_debug", report)
        self.assertIn("after_state", report)
        self.assertIn("after_debug", report)

    def test_wrong_runtime_refuses_before_connection_and_retains_failure(self):
        code, report, app, _, _ = self.run_mock(bad_identity=True)
        self.assertEqual(code, 1)
        self.assertEqual(app.messages, [])
        self.assertEqual(app.connections, 0)
        self.assertEqual(report["status"], "FAIL")

    def test_occupied_slot_and_busy_recorder_refuse_without_mutation(self):
        for busy in ("slot", "recorder", "health"):
            with self.subTest(busy=busy):
                app = FakeApplication()
                if busy == "slot":
                    app.saved = True
                elif busy == "recorder":
                    app.record_status["recording"] = True
                else:
                    del app.health["audio"]["piano_midi"]["native_render_lock_misses"]
                code, report, app, _, _ = self.run_mock(app)
                self.assertEqual(code, 1)
                self.assertTrue(report["failures"])
                self.assertTrue(all(message["type"] in {"get_state", "list_pad_slots", "list_recordings"}
                                    for message in app.messages))

    def test_incomplete_or_truncated_recording_never_saved_and_settings_restored(self):
        for attribute in ("bad_take", "truncated"):
            with self.subTest(attribute=attribute):
                app = FakeApplication()
                setattr(app, attribute, True)
                code, report, app, _, _ = self.run_mock(app)
                self.assertEqual(code, 1)
                self.assertEqual(app.toggle_count, 2)
                self.assertFalse(app.saved)
                self.assertNotIn("save_to_pad_slot", [item["type"] for item in app.messages])
                self.assertEqual(app.state, app.original)
                self.assertEqual(report["restore_failures"], [])

    def test_slot_rechecked_before_saving_and_never_overwritten(self):
        app = FakeApplication()
        app.raced_slot = True
        code, report, app, _, _ = self.run_mock(app)
        self.assertEqual(code, 1)
        self.assertNotIn("save_to_pad_slot", [item["type"] for item in app.messages])
        self.assertEqual(app.state, app.original)
        self.assertTrue(report["failures"])

    def test_bad_fresh_hydration_fails_and_panic_restores(self):
        app = FakeApplication()
        app.hydration_bad = True
        code, report, app, _, _ = self.run_mock(app)
        self.assertEqual(code, 1)
        self.assertEqual(app.connections, 2)
        self.assertFalse(app.active)
        self.assertFalse(app.faded_out)
        self.assertEqual(app.state, app.original)
        self.assertTrue(report["timeline"])

    def test_lost_stop_ack_does_not_toggle_on_a_second_recording(self):
        app = FakeApplication()
        app.fail_record_off = True
        code, report, app, _, _ = self.run_mock(app)
        self.assertEqual(code, 1)
        self.assertEqual(app.toggle_count, 2)
        self.assertFalse(app.record_status["recording"])
        self.assertEqual(app.state, app.original)
        self.assertEqual(report["restore_failures"], [])

    def test_driver_refusal_still_restores(self):
        code, report, app, _, _ = self.run_mock(driver_failure=True)
        self.assertEqual(code, 1)
        self.assertEqual(app.state, app.original)
        self.assertEqual(report["restore_failures"], [])

    def test_cancellation_restores_and_retains_failure_evidence(self):
        code, report, app, _, _ = self.run_mock(driver_failure="cancel")
        self.assertEqual(code, "cancelled")
        self.assertEqual(app.state, app.original)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["restore_failures"], [])
        self.assertTrue(any("CancelledError" in failure for failure in report["failures"]))

    def test_missed_musical_window_refuses_late_recording_and_restores(self):
        code, report, app, _, _ = self.run_mock(late_window=True)
        self.assertEqual(code, 1)
        self.assertEqual(app.toggle_count, 0)
        self.assertEqual(app.state, app.original)
        self.assertEqual(report["restore_failures"], [])
        self.assertTrue(any("missed its musical window" in failure for failure in report["failures"]))

    def test_schedule_records_within_held_chord_and_always_releases(self):
        schedule = probe.build_schedule()
        self.assertEqual(schedule, sorted(schedule, key=lambda item: item[0]))
        self.assertEqual(schedule, probe.build_schedule())
        self.assertEqual(len(schedule), 63)
        self.assertEqual(schedule[-2:], [(47.0, 0xB0, 64, 0), (47.0, 0xB0, 123, 0)])
        first_off = min(when for when, status, _, _ in schedule if status == 0x80)
        self.assertEqual(first_off, 18.0)
        self.assertGreater(first_off, 12.0)

    def test_take_validation_never_defaults_missing_integrity_to_good(self):
        for key in ("complete", "frames", "dropped_frames", "writer_pending", "audio_synced", "metadata_saved"):
            take = completed_take()
            del take[key]
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                probe.validate_take(take, "feature-take.wav", listed=True)

    def test_native_wav_finite_scan_and_truncation_refusal(self):
        def wav(left, right):
            return (b"RIFF" + struct.pack("<I", 58) + b"WAVEfmt "
                    + struct.pack("<IHHIIHHH", 18, 3, 2, 48000, 384000, 8, 32, 0)
                    + b"fact" + struct.pack("<II", 4, 1) + b"data" + struct.pack("<Iff", 8, left, right))
        report = {"returncode": 0, "stdout_truncated": False, "stderr_truncated": False,
                  "events": {"capture_complete": {"nonfinite": 0, "xruns": 0, "errors": 0,
                             "sample_rate_hz": 48000, "block_frames": 512, "frames": 1, "midi_events": 63}}}
        with tempfile.TemporaryDirectory() as temporary, patch.object(probe, "DURATION_SECONDS", 1 / 48000):
            path = Path(temporary) / "capture.wav"
            path.write_bytes(wav(0.5, -0.3))
            self.assertTrue(probe.validate_capture(report, path, 63)["all_samples_finite"])
            for data in (wav(float("nan"), 0.1), wav(0.5, 0.3)[:-1], wav(0.0, 0.0)):
                path.write_bytes(data)
                with self.assertRaises(RuntimeError):
                    probe.validate_capture(report, path, 63)
            path.write_bytes(wav(0.5, 0.3))
            del report["events"]["capture_complete"]["nonfinite"]
            with self.assertRaises(RuntimeError):
                probe.validate_capture(report, path, 63)

    def test_existing_output_directory_refuses_without_network(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(probe, "verify_runtime", new=AsyncMock()) as runtime:
            args = SimpleNamespace(driver=sys.executable, output_dir=temporary)
            with self.assertRaises(RuntimeError):
                asyncio.run(probe.run(args))
            runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
