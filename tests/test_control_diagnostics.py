"""Offline telemetry and AST-extracted control/save checks; no app startup."""
import ast
import copy
from contextlib import nullcontext
import json
import logging
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from stave_synth.control_diagnostics import ControlDiagnostics


ROOT = Path(__file__).resolve().parents[1]


def method(name, **namespace):
    tree = ast.parse((ROOT / "stave_synth/main.py").read_text())
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == "StaveSynth")
    node = next(node for node in cls.body
                if isinstance(node, ast.FunctionDef) and node.name == name)
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    values = dict(copy=copy, json=json, logger=logging.getLogger(__name__),
                  ValidationError=ValueError, validate_message=lambda value: value,
                  __package__="stave_synth")
    values.update(namespace)
    exec(compile(module, str(ROOT / "stave_synth/main.py"), "exec"), values)
    return values[name]


class ControlDiagnosticsTests(unittest.TestCase):
    def test_complete_span_fields_and_cpu_scope(self):
        diagnostics = ControlDiagnostics()
        with patch("stave_synth.control_diagnostics.time.monotonic_ns", side_effect=[100, 2_000_100]), \
             patch("stave_synth.control_diagnostics.time.thread_time_ns", side_effect=[10, 110]):
            with diagnostics.span("control_dispatch"):
                pass
        status = diagnostics.snapshot()
        self.assertEqual(status["records"]["control"],
                         [(1, "control_dispatch", 100, 2_000_100, 100, False, True)])
        self.assertEqual(status["operations"]["control_dispatch"], dict(
            calls=1, failures=0, incomplete=0, complete=1,
            wall_sum_ns=2_000_000, wall_max_ns=2_000_000,
            thread_cpu_sum_ns=100, thread_cpu_max_ns=100))
        self.assertEqual(status["cpu_scope"], "calling_thread_excludes_helpers")

    def test_original_exception_and_base_exception_are_preserved(self):
        diagnostics = ControlDiagnostics()
        for error in (RuntimeError("body"), KeyboardInterrupt()):
            with self.assertRaises(type(error)) as caught:
                with diagnostics.span("control_dispatch"):
                    raise error
            self.assertIs(caught.exception, error)
        status = diagnostics.snapshot()
        self.assertEqual(status["operations"]["control_dispatch"]["failures"], 2)
        self.assertTrue(all(row[-2:] == (True, True)
                            for row in status["records"]["control"]))

    def test_clock_failures_record_incomplete_without_changing_body(self):
        diagnostics = ControlDiagnostics()
        seen = []
        with patch("stave_synth.control_diagnostics.time.monotonic_ns", side_effect=OSError("clock")):
            with diagnostics.span("autosave_save"):
                seen.append("saved")
        status = diagnostics.snapshot()
        self.assertEqual(seen, ["saved"])
        self.assertEqual(status["diagnostic_errors"], 2)
        self.assertEqual(status["incomplete_spans"], 1)
        self.assertEqual(status["operations"]["autosave_save"]["complete"], 0)
        self.assertEqual(status["records"]["autosave"][0][2:],
                         (None, None, None, False, False))
        json.dumps(status, allow_nan=False)

    def test_invalid_clock_values_are_incomplete_and_json_safe(self):
        diagnostics = ControlDiagnostics()
        for clocks in ((20, 10, 1, 2), (1, 2, 4, 3), (object(), 2, 1, 2)):
            diagnostics._record("control_snapshot", *clocks, False)
        status = diagnostics.snapshot()
        self.assertEqual(status["incomplete_spans"], 3)
        self.assertEqual(status["operations"]["control_snapshot"]["wall_sum_ns"], 0)
        json.dumps(status, allow_nan=False)

    def test_record_failure_cannot_mask_body_exception(self):
        diagnostics = ControlDiagnostics()
        error = ValueError("original")
        with patch.object(diagnostics, "_record", side_effect=RuntimeError("telemetry")):
            with self.assertRaises(ValueError) as caught:
                with diagnostics.span("control_dispatch"):
                    raise error
            with diagnostics.span("autosave_save"):
                pass
        self.assertIs(caught.exception, error)
        self.assertEqual(diagnostics.snapshot()["dropped_spans"], 2)
        self.assertEqual(diagnostics.snapshot()["diagnostic_errors"], 2)

    def test_unknown_operations_do_not_grow_storage(self):
        diagnostics = ControlDiagnostics()
        for index in range(100):
            with diagnostics.span(f"unknown-{index}"):
                pass
        status = diagnostics.snapshot()
        self.assertEqual(set(status["operations"]), set(diagnostics.OPERATIONS))
        self.assertEqual(status["count"], 0)
        self.assertEqual(status["dropped_spans"], 100)

    def test_contended_writer_and_reader_never_wait(self):
        diagnostics = ControlDiagnostics()
        owned, release = threading.Event(), threading.Event()

        def hold():
            with diagnostics._lock:
                owned.set()
                release.wait(2)

        worker = threading.Thread(target=hold)
        worker.start()
        try:
            self.assertTrue(owned.wait(1))
            with diagnostics.span("control_dispatch"):
                pass
            status = diagnostics.snapshot()
            self.assertFalse(status["available"])
            self.assertTrue(status["lock_contended"])
            self.assertEqual(status["dropped_spans"], 1)
        finally:
            release.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_histories_are_bounded_and_slider_traffic_cannot_evict_saves(self):
        diagnostics = ControlDiagnostics()
        for index in range(80):
            diagnostics._record("autosave_save", index, index + 1, 1, 2, False)
        before = diagnostics.snapshot()["records"]["autosave"]
        for index in range(200):
            diagnostics._record("control_snapshot", index, index + 2_000_000, 1, 2, False)
            diagnostics._record("control_dispatch", index, index + 1, 1, 2, False)
        status = diagnostics.snapshot()
        self.assertEqual(status["records"]["autosave"], before)
        self.assertEqual([len(rows) for rows in status["records"].values()], [64, 64])
        self.assertEqual(status["evicted"], dict(control=136, autosave=16))
        self.assertEqual(status["operations"]["control_dispatch"]["calls"], 200)
        self.assertEqual(status["count"], 480)

    def test_snapshot_is_detached_and_reports_actual_interval_without_setting_it(self):
        diagnostics = ControlDiagnostics()
        diagnostics._record("autosave_save", 1, 3, 1, 2, False)
        with patch("stave_synth.control_diagnostics.sys.getswitchinterval", return_value=.001), \
             patch("stave_synth.control_diagnostics.sys.setswitchinterval") as setter:
            status = diagnostics.snapshot()
        self.assertEqual(status["switch_interval_seconds"], .001)
        setter.assert_not_called()
        status["operations"]["autosave_save"]["calls"] = -1
        status["records"]["autosave"].clear()
        self.assertEqual(diagnostics.snapshot()["operations"]["autosave_save"]["calls"], 1)
        self.assertEqual(len(diagnostics.snapshot()["records"]["autosave"]), 1)


class ControlDiagnosticsIntegrationTests(unittest.TestCase):
    def make_app(self, diagnostics):
        state = {key: {"value": 1} for key in
                 ("synth_pad", "piano", "organ", "master", "macros")}
        app = SimpleNamespace(
            state=state, _control_lock=threading.RLock(), _stopping=False,
            _control_diagnostics=diagnostics, _control_error=None,
            _crossfade_cancel=threading.Event(),
            synth=SimpleNamespace(sympathetic_set_suppress=Mock()),
        )
        app._restore_failed_scene = Mock(side_effect=lambda previous, error: setattr(app, "state", previous))

        def dispatch(command):
            app.state["master"]["value"] = 2
            return {"type": "ack", "state": app.state}

        app._dispatch_ws_message = dispatch
        return app

    def test_control_success_matches_disabled_and_response_is_detached(self):
        handle = method("_handle_ws_message")
        for diagnostics in (None, ControlDiagnostics()):
            app = self.make_app(diagnostics)
            result = handle(app, {"type": "setting"})
            self.assertEqual(result["state"]["master"]["value"], 2)
            result["state"]["master"]["value"] = 99
            self.assertEqual(app.state["master"]["value"], 2)
            app._restore_failed_scene.assert_not_called()
            if diagnostics:
                operations = diagnostics.snapshot()["operations"]
                for name in ("control_snapshot", "control_dispatch", "control_response_detach"):
                    self.assertEqual(operations[name]["calls"], 1)

    def test_dispatch_exception_and_error_response_preserve_rollback(self):
        handle = method("_handle_ws_message")
        for error_response in (False, True):
            diagnostics = ControlDiagnostics()
            app = self.make_app(diagnostics)

            def dispatch(command):
                app.state["master"]["value"] = 9
                if error_response:
                    return {"type": "error", "message": "rejected"}
                raise ValueError("rejected")

            app._dispatch_ws_message = dispatch
            with self.assertLogs(level="WARNING") if not error_response else nullcontext():
                result = handle(app, {"type": "setting"})
            self.assertEqual(result["type"], "error")
            self.assertEqual(app.state["master"]["value"], 1)
            app._restore_failed_scene.assert_called_once()
            self.assertEqual(diagnostics.snapshot()["operations"]["control_dispatch"]["failures"],
                             int(not error_response))

    def test_snapshot_and_detach_exceptions_keep_existing_application_semantics(self):
        handle = method("_handle_ws_message")
        original_copy = copy.deepcopy
        for failing_call, operation, expected_value in (
                (1, "control_snapshot", 1), (2, "control_response_detach", 2)):
            diagnostics = ControlDiagnostics()
            app = self.make_app(diagnostics)
            calls = 0

            def copy_or_fail(value):
                nonlocal calls
                calls += 1
                if calls == failing_call:
                    raise ValueError("copy failed")
                return original_copy(value)

            with patch.object(copy, "deepcopy", side_effect=copy_or_fail), self.assertLogs(level="WARNING"):
                result = handle(app, {"type": "setting"})
            self.assertEqual(result["type"], "error")
            self.assertEqual(app.state["master"]["value"], expected_value)
            app._restore_failed_scene.assert_not_called()
            self.assertEqual(diagnostics.snapshot()["operations"][operation]["failures"], 1)

    def run_autosaves(self, diagnostics, *, fail_save=False):
        saves = Mock(side_effect=ValueError("save failed") if fail_save else None)
        run = method("_autosave_loop", AUTOSAVE_INTERVAL=.05, save_state=saves)
        app = SimpleNamespace(
            _running=True, _stop_event=SimpleNamespace(wait=Mock(side_effect=[False, False, True])),
            _control_lock=threading.RLock(), _control_diagnostics=diagnostics,
            jack=None, ws_server=None, state={"value": 1},
        )
        with patch.dict(sys.modules, {"psutil": SimpleNamespace(Process=lambda: object())}):
            if fail_save:
                with self.assertLogs(level="WARNING"):
                    run(app)
            else:
                run(app)
        return app, saves

    def test_autosave_save_once_when_changed_and_retains_both_snapshot_spans(self):
        diagnostics = ControlDiagnostics()
        app, saves = self.run_autosaves(diagnostics)
        saves.assert_called_once_with(app.state)
        self.assertEqual(app._last_saved_snapshot, json.dumps(app.state, sort_keys=True, allow_nan=False))
        operations = diagnostics.snapshot()["operations"]
        self.assertEqual(operations["autosave_snapshot"]["calls"], 2)
        self.assertEqual(operations["autosave_save"]["calls"], 1)

    def test_autosave_exception_is_recorded_and_does_not_mark_unsaved_state_saved(self):
        diagnostics = ControlDiagnostics()
        app, saves = self.run_autosaves(diagnostics, fail_save=True)
        self.assertEqual(saves.call_count, 2)
        self.assertFalse(hasattr(app, "_last_saved_snapshot"))
        self.assertEqual(diagnostics.snapshot()["operations"]["autosave_save"]["failures"], 2)

    def test_autosave_snapshot_exception_prevents_save_and_remains_retryable(self):
        diagnostics = ControlDiagnostics()
        with patch.object(json, "dumps", side_effect=ValueError("snapshot failed")), \
             self.assertLogs(level="WARNING"):
            app, saves = self.run_autosaves(diagnostics)
        saves.assert_not_called()
        self.assertFalse(hasattr(app, "_last_saved_snapshot"))
        self.assertEqual(diagnostics.snapshot()["operations"]["autosave_snapshot"]["failures"], 2)
        self.assertEqual(diagnostics.snapshot()["operations"]["autosave_save"]["calls"], 0)

    def test_disabled_paths_take_no_diagnostic_clocks(self):
        with patch("stave_synth.control_diagnostics.time.monotonic_ns", side_effect=AssertionError("clock")) as mono, \
             patch("stave_synth.control_diagnostics.time.thread_time_ns", side_effect=AssertionError("clock")) as cpu:
            result = method("_handle_ws_message")(self.make_app(None), {"type": "setting"})
            _, saves = self.run_autosaves(None)
        self.assertEqual(result["type"], "ack")
        saves.assert_called_once()
        mono.assert_not_called()
        cpu.assert_not_called()

    def test_health_exposes_separate_snapshot_and_fails_open(self):
        health = method("_health_status")
        app = SimpleNamespace(_control_error=None, _controls=SimpleNamespace(status=lambda: {}),
                              _ui_recovery=SimpleNamespace(status=lambda: {}), ws_server=None, jack=None)
        self.assertNotIn("control_diagnostics", health(app))
        app._control_diagnostics = ControlDiagnostics()
        self.assertTrue(health(app)["control_diagnostics"]["available"])
        app._control_diagnostics = SimpleNamespace(snapshot=Mock(side_effect=RuntimeError("snapshot")))
        self.assertFalse(health(app)["control_diagnostics"]["available"])
        app._control_diagnostics = None
        app._control_diagnostics_unavailable = True
        self.assertEqual(health(app)["control_diagnostics"]["diagnostic_errors"], 1)

    def test_constructor_gate_is_exact_and_does_not_change_switch_interval(self):
        source = (ROOT / "stave_synth/main.py").read_text()
        tree = ast.parse(source)
        gates = [node for node in ast.walk(tree) if isinstance(node, ast.If)
                 and ast.unparse(node.test) == "os.environ.get('STAVE_DIAGNOSTICS') == '1'"]
        self.assertEqual(len(gates), 1)
        self.assertIn("self._control_diagnostics = ControlDiagnostics()", ast.unparse(gates[0]))
        self.assertNotIn("setswitchinterval", source)


if __name__ == "__main__":
    unittest.main()
