"""Protocol-only performance probe tests; no app/native/device/network imports."""

import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tools import run_performance_probe as probe
from tools.run_audition import parse_runtime_config
from tests.test_feature_probe import FakeApplication, assign


class PerformanceApplication(FakeApplication):
    def __init__(self):
        super().__init__()
        for command in probe.setup_commands():
            if command["param"] not in self.state.setdefault(command["section"], {}):
                assign(self.state, command["section"], command["param"], command["value"])
        self.state["master"]["instrument_mode"] = "piano"
        self.state["piano"]["enabled"] = True
        self.state["organ"]["enabled"] = False
        self.state["organ"]["leslie_speed"] = "fast"
        self.state["master"]["split_octave_snapshot"] = {
            "osc1_octave": -1, "osc2_octave": 1, "piano_octave": 1, "shimmer_high": True}
        self.saved = True  # Prior probe's inactive C sample must be acceptable.
        self.original = copy.deepcopy(self.state)
        self.fail_rhodes = False
        self.ignore_stop = False
        self.hidden_mutation = False
        self.piano_gap = False
        self.soundfonts = ["Fluid", "Rhodes", "Suitcase"]
        # Separate runtime state models _apply_instrument_mode side effects;
        # it is not the persisted section flags returned by get_state.
        self.runtime_mode = "piano"
        self.runtime_piano_enabled = True
        self.runtime_organ_enabled = False
        self.runtime_routed_instrument = "piano"
        self.runtime_transpose = self.state["master"]["transpose_semitones"]
        self.runtime_at_cancel = None

    def runtime_snapshot(self):
        return {"mode": self.runtime_mode, "piano_enabled": self.runtime_piano_enabled,
                "organ_enabled": self.runtime_organ_enabled,
                "routed_instrument": self.runtime_routed_instrument,
                "transpose": self.runtime_transpose}

    def dispatch(self, message, connection):
        kind = message["type"]
        if kind == "transpose":
            self.messages.append(copy.deepcopy(message))
            self.state["master"]["transpose_semitones"] = message["semitones"]
            self.runtime_transpose = message["semitones"]
            return {"type": "transpose_ack", "semitones": message["semitones"]}
        if kind == "freeze_toggle":
            self.messages.append(copy.deepcopy(message))
            self.state["synth_pad"]["freeze_enabled"] = message["enabled"]
            return {"type": "freeze_ack", "enabled": message["enabled"]}
        if kind == "setting":
            section, name, value = message["section"], message["param"], message["value"]
            if self.fail_rhodes and name == "soundfont" and value == "Rhodes":
                self.messages.append(copy.deepcopy(message))
                return {"type": "error", "message": "Rhodes is not preloaded"}
            if self.ignore_stop and name == "leslie_speed" and value == "stop":
                self.messages.append(copy.deepcopy(message))
                return {**message, "type": "setting_ack"}  # False ack must fail state verification.
            if section == "master" and name == "split_enabled" and value != self.state["master"][name]:
                current = {"osc1_octave": self.state["synth_pad"]["osc1_octave"],
                           "osc2_octave": self.state["synth_pad"]["osc2_octave"],
                           "piano_octave": self.state["master"]["piano_octave"],
                           "shimmer_high": self.state["synth_pad"]["shimmer_high"]}
                stash = self.state["master"].get("split_octave_snapshot")
                if not isinstance(stash, dict):
                    stash = dict(current)
                for key in ("osc1_octave", "osc2_octave"):
                    self.state["synth_pad"][key] = int(stash.get(key, 0))
                self.state["synth_pad"]["shimmer_high"] = bool(stash.get("shimmer_high", False))
                self.state["master"]["piano_octave"] = int(stash.get("piano_octave", 0))
                self.state["master"]["split_octave_snapshot"] = current
            if section == "master" and name == "instrument_mode" and value != self.runtime_mode:
                self.runtime_mode = value
                self.runtime_piano_enabled = value == "piano"
                self.runtime_organ_enabled = value == "organ"
                self.runtime_routed_instrument = value if value != "off" else None
            elif section == "piano" and name == "enabled":
                self.runtime_piano_enabled = value
            elif section == "organ" and name == "enabled":
                self.runtime_organ_enabled = value
            elif section == "master" and name == "transpose_semitones":
                self.runtime_transpose = value
            if name == "leslie_speed" and value == "stop":
                if self.hidden_mutation:
                    self.state["master"]["unexpected_change"] = 1
                if self.piano_gap:
                    self.health["audio"]["piano_midi"]["native_render_lock_misses"] += 1
        reply = super().dispatch(message, connection)
        if kind == "get_state":
            reply["soundfonts_available"] = self.soundfonts.copy()
        return reply


class PerformanceProbeTests(unittest.TestCase):
    def run_mock(self, app=None, *, identity_failure=None, cancel=False, late=False,
                 short_freeze=False, cancel_after_freeze=False, cancel_at=None):
        app = app or PerformanceApplication()
        identity_calls = []
        clock_ns = [10_000_000_000]

        async def runtime():
            identity_calls.append(len(app.messages))
            instance = "stage" if len(identity_calls) == identity_failure else "audition"
            return parse_runtime_config('window.STAVE_RUNTIME = ' + json.dumps(
                {"websocket_port": 18765, "instance": instance}) + ';')

        async def at(_self, seconds, *, latest=None):
            if late:
                raise RuntimeError("automation missed its musical window")
            clock_ns[0] = int((10 + (34 if short_freeze and seconds == 39 else seconds)) * 1_000_000_000)

        async def driver(*args, automation):
            if cancel:
                raise asyncio.CancelledError()
            await automation(args[3], args[4], asyncio.get_running_loop().time())
            return {"returncode": 0, "events": {"capture_complete": {}}, "sweep_timeline": []}

        original_checkpoint = probe.PerformanceProbe.checkpoint

        async def checkpoint(instance, label, expected):
            result = await original_checkpoint(instance, label, expected)
            if (cancel_after_freeze and label == "freeze_on_state") or label == cancel_at:
                app.runtime_at_cancel = app.runtime_snapshot()
                raise asyncio.CancelledError()
            return result

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence"
            args = SimpleNamespace(driver=sys.executable, output_dir=str(output))
            with patch.object(probe, "verify_runtime", side_effect=runtime), \
                    patch.object(probe.websockets, "connect", side_effect=app.connect), \
                    patch.object(probe, "run_driver", side_effect=driver), \
                    patch.object(probe.PerformanceProbe, "at", new=at), \
                    patch.object(probe.PerformanceProbe, "checkpoint", new=checkpoint), \
                    patch.object(probe.asyncio, "sleep", new=AsyncMock()), \
                    patch.object(probe.time, "monotonic_ns", side_effect=lambda: clock_ns[0]), \
                    patch.object(probe, "validate_capture", return_value={"all_samples_finite": True}):
                try:
                    code = asyncio.run(probe.run(args))
                except asyncio.CancelledError:
                    code = "cancelled"
            report = json.loads((output / "performance-probe.result.json").read_text())
            original_exists = (output / "00-original-state.json").exists()
        return code, report, app, identity_calls, original_exists

    def test_complete_sequence_restores_every_field_and_changes_no_libraries(self):
        code, report, app, checks, original_exists = self.run_mock()
        self.assertEqual(code, 0, report["failures"] + report.get("restore_failures", []))
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(app.state, app.original)
        self.assertTrue(app.saved)
        self.assertFalse(app.active)
        self.assertEqual(app.toggle_count, 0)
        self.assertTrue(original_exists)
        self.assertEqual(len(checks), 2)
        self.assertEqual(app.messages[checks[1]]["type"], "panic")
        allowed = {"get_state", "list_pad_slots", "setting", "panic", "debug", "transpose", "freeze_toggle"}
        self.assertTrue(all(message["type"] in allowed for message in app.messages))
        self.assertFalse(any(message.get("param") == "reverb_type" for message in app.messages))
        leslie = [message["value"] for message in app.messages if message.get("param") == "leslie_speed"]
        self.assertEqual(leslie, ["slow", "fast", "stop", "slow", "fast"])
        modes = [message["value"] for message in app.messages if message.get("param") == "instrument_mode"]
        self.assertEqual(modes, ["piano", "organ", "piano", "piano"])
        for key in ("before_state", "before_debug", "after_state", "after_debug", "pre_panic_state", "pre_panic_debug"):
            self.assertIn(key, report)
        self.assertTrue(all("reply" in entry for entry in report["timeline"]))
        self.assertTrue(any(entry["label"].startswith("restore_") for entry in report["timeline"]))

    def test_original_enabled_split_and_nontrivial_stash_restore_exactly(self):
        app = PerformanceApplication()
        app.state["master"]["split_enabled"] = True
        app.state["synth_pad"]["osc1_octave"] = -2
        app.state["synth_pad"]["osc2_octave"] = 2
        app.state["master"]["piano_octave"] = -1
        app.original = copy.deepcopy(app.state)
        code, report, app, _, _ = self.run_mock(app)
        self.assertEqual(code, 0, report["failures"] + report.get("restore_failures", []))
        self.assertEqual(app.state, app.original)

    def test_none_empty_and_partial_split_stashes_restore_for_either_mode(self):
        for enabled in (False, True):
            for stash in (None, {}, {"osc1_octave": -1, "shimmer_high": True}):
                with self.subTest(enabled=enabled, stash=stash):
                    app = PerformanceApplication()
                    app.state["master"]["split_enabled"] = enabled
                    app.state["master"]["split_octave_snapshot"] = copy.deepcopy(stash)
                    app.state["synth_pad"]["osc1_octave"] = -2
                    app.state["synth_pad"]["osc2_octave"] = 2
                    app.state["master"]["piano_octave"] = -1
                    app.original = copy.deepcopy(app.state)
                    code, report, app, _, _ = self.run_mock(app)
                    self.assertEqual(code, 0, report["failures"] + report.get("restore_failures", []))
                    self.assertEqual(app.state, app.original)

    def test_both_identity_gates_refuse_before_first_mutation(self):
        for which in (1, 2):
            with self.subTest(gate=which):
                code, report, app, _, _ = self.run_mock(identity_failure=which)
                self.assertEqual(code, 1)
                self.assertTrue(report["failures"])
                self.assertTrue(all(message["type"] in {"get_state", "list_pad_slots"} for message in app.messages))

    def test_unrestorable_fields_active_bed_and_missing_preset_refuse(self):
        for variant in ("missing", "transpose", "stash", "bed", "font", "recorder"):
            app = PerformanceApplication()
            if variant == "missing":
                del app.state["organ"]["leslie_depth"]
            elif variant == "transpose":
                app.state["master"]["transpose_semitones"] = 24
            elif variant == "stash":
                app.state["master"]["split_octave_snapshot"]["unknown"] = 2
            elif variant == "bed":
                app.active = True
            elif variant == "font":
                app.soundfonts = ["Fluid"]
            else:
                app.record_status["writer_pending"] = True
            with self.subTest(variant=variant):
                code, report, app, _, _ = self.run_mock(app)
                self.assertEqual(code, 1)
                self.assertTrue(report["failures"])
                self.assertTrue(all(message["type"] in {"get_state", "list_pad_slots"} for message in app.messages))

    def test_false_stop_ack_or_program_failure_fails_and_restores(self):
        for attribute in ("ignore_stop", "fail_rhodes"):
            app = PerformanceApplication()
            setattr(app, attribute, True)
            with self.subTest(attribute=attribute):
                code, report, app, _, _ = self.run_mock(app)
                self.assertEqual(code, 1)
                self.assertEqual(app.state, app.original)
                self.assertEqual(report["restore_failures"], [])

    def test_hidden_state_change_and_source_counter_growth_remain_failures(self):
        for attribute in ("hidden_mutation", "piano_gap"):
            app = PerformanceApplication()
            setattr(app, attribute, True)
            with self.subTest(attribute=attribute):
                code, report, _, _, _ = self.run_mock(app)
                self.assertEqual(code, 1)
                self.assertTrue(report["restore_failures"] if attribute == "hidden_mutation" else report["failures"])

    def test_short_freeze_or_late_command_fails_and_restores(self):
        for option in ("short_freeze", "late"):
            with self.subTest(option=option):
                code, report, app, _, _ = self.run_mock(**{option: True})
                self.assertEqual(code, 1)
                self.assertEqual(app.state, app.original)
                self.assertEqual(report["restore_failures"], [])

    def test_cancellation_restores_and_retains_report(self):
        code, report, app, _, _ = self.run_mock(cancel=True)
        self.assertEqual(code, "cancelled")
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(app.state, app.original)
        self.assertEqual(report["restore_failures"], [])

    def test_cancellation_while_frozen_panics_and_restores_without_toggling(self):
        code, report, app, _, _ = self.run_mock(cancel_after_freeze=True)
        self.assertEqual(code, "cancelled")
        self.assertEqual(app.state, app.original)
        self.assertEqual(report["restore_failures"], [])
        freezes = [message for message in app.messages if message["type"] == "freeze_toggle"]
        self.assertEqual(freezes, [{"type": "freeze_toggle", "enabled": True}])
        self.assertFalse(app.state["synth_pad"]["freeze_enabled"])

    def test_cancellation_during_organ_restores_runtime_enables_and_routing(self):
        code, report, app, _, _ = self.run_mock(cancel_at="intentional_piano_to_organ_state")
        self.assertEqual(code, "cancelled")
        self.assertEqual(app.runtime_at_cancel, {"mode": "organ", "piano_enabled": False,
                         "organ_enabled": True, "routed_instrument": "organ", "transpose": 0})
        self.assertEqual(app.runtime_snapshot(), {"mode": "piano", "piano_enabled": True,
                         "organ_enabled": False, "routed_instrument": "piano", "transpose": 0})
        self.assertEqual(app.state, app.original)
        self.assertEqual(report["restore_failures"], [])

    def test_cancellation_after_transpose_restores_runtime_and_saved_pitch(self):
        code, report, app, _, _ = self.run_mock(cancel_at="transposed_state")
        self.assertEqual(code, "cancelled")
        self.assertEqual(app.runtime_at_cancel["transpose"], 2)
        self.assertEqual(app.runtime_snapshot(), {"mode": "piano", "piano_enabled": True,
                         "organ_enabled": False, "routed_instrument": "piano", "transpose": 0})
        self.assertEqual(app.state, app.original)
        self.assertEqual(report["restore_failures"], [])

    def test_schedule_spans_raw_split_and_releases_held_transposed_notes(self):
        schedule = probe.build_schedule()
        self.assertEqual(len(schedule), 111)
        self.assertEqual(schedule, sorted(schedule, key=lambda event: event[0]))
        self.assertEqual(schedule[-2:], [(47.0, 0xB0, 64, 0), (47.0, 0xB0, 123, 0)])
        for onset in (22, 27):
            notes = [note for when, status, note, _ in schedule if when == onset and status == 0x90]
            self.assertTrue(any(note < 60 for note in notes) and any(note >= 60 for note in notes))
        releases = {note for when, status, note, _ in schedule if 23 < when < 26 and status == 0x80}
        self.assertEqual(releases, {48, 55, 60, 64, 67})

    def test_restore_plan_reasserts_mode_split_side_effects_and_enable_flags(self):
        state = PerformanceApplication().state
        plan = probe.restoration_plan(state)
        self.assertEqual((plan[0]["section"], plan[0]["param"]), ("master", "instrument_mode"))
        self.assertEqual((plan[1]["section"], plan[1]["param"]), ("master", "split_enabled"))
        self.assertEqual([(item["section"], item["param"]) for item in plan[-2:]],
                         [("piano", "enabled"), ("organ", "enabled")])
        self.assertEqual(plan[-3]["param"], "split_octave_snapshot")
        self.assertFalse(any(item["param"] == "reverb_type" for item in plan))

    def test_existing_output_directory_refuses_without_network(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(probe, "verify_runtime", new=AsyncMock()) as runtime:
            with self.assertRaises(RuntimeError):
                asyncio.run(probe.run(SimpleNamespace(driver=sys.executable, output_dir=temporary)))
            runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
