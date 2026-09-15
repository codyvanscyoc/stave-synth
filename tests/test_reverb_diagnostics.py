"""Opt-in native ownership diagnostics, without native/audio imports.

Extracts the real decorator/collector and only the pre-native constructor
prefix/status accessor. Fake DSP methods exercise return, exception and lock
semantics. No CFFI, SciPy, JACK, service, file output or network is used.
"""

import ast
import os
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "stave_synth/faust_reverb.py"


def load_diagnostics(clock_module=time):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id.startswith("_DIAGNOSTIC_")
                for target in node.targets):
            nodes.append(node)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in {
                "_ReverbDiagnostics", "_native_owned"}:
            nodes.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "FaustReverb":
            selected = []
            for method in node.body:
                if not isinstance(method, ast.FunctionDef):
                    continue
                if method.name == "__init__":
                    # Stop before even the native allocation call. Assert the
                    # selected prefix owns only the two intended attributes.
                    method.body = method.body[:2]
                    targets = [item.targets[0].attr for item in method.body]
                    if targets != ["_native_lock", "_diagnostics"]:
                        raise AssertionError("Native constructor prefix changed")
                    selected.append(method)
                elif method.name == "get_diagnostics_status":
                    selected.append(method)
            node.body = selected
            nodes.append(node)
    namespace = {"os": os, "threading": threading, "time": clock_module}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 str(SOURCE), "exec"), namespace)
    return namespace


def make_owner(namespace, enabled=True):
    owned = namespace["_native_owned"]

    class Owner(namespace["FaustReverb"]):
        @owned
        def process(self, value):
            return self.compute(value)

        @owned
        def set_shimmer_feedback(self, value):
            return self.control(value)

        @owned
        def set_damp(self, value):
            return self._mirror_zone(value)

        @owned
        def _mirror_zone(self, value):
            return value

    with patch.dict(os.environ, {"STAVE_DIAGNOSTICS": "1" if enabled else "0"}):
        owner = Owner()
    owner.compute = lambda value: value
    owner.control = lambda value: value
    return owner


class ScriptedClocks:
    def __init__(self, wall, cpu):
        self.wall = iter(wall)
        self.cpu = iter(cpu)

    def monotonic_ns(self):
        return next(self.wall)

    def thread_time_ns(self):
        return next(self.cpu)


class ReverbDiagnosticTests(unittest.TestCase):
    def test_environment_is_opt_in_exactly_one(self):
        namespace = load_diagnostics()
        for value in (None, "", "0", "true", "yes", "1"):
            with self.subTest(value=value), patch.dict(os.environ):
                if value is None:
                    os.environ.pop("STAVE_DIAGNOSTICS", None)
                else:
                    os.environ["STAVE_DIAGNOSTICS"] = value
                owner = namespace["FaustReverb"]()
                self.assertEqual(owner.get_diagnostics_status()["enabled"], value == "1")

    def test_disabled_path_never_reads_clocks_or_records(self):
        class ForbiddenClocks:
            def __getattr__(self, name):
                raise AssertionError("Disabled diagnostic path read a clock")

        namespace = load_diagnostics(ForbiddenClocks())
        owner = make_owner(namespace, enabled=False)
        token = object()
        self.assertIs(owner.process(token), token)
        self.assertIs(owner.set_shimmer_feedback(token), token)
        self.assertIsNone(owner._diagnostics)
        self.assertEqual(owner.get_diagnostics_status(), {"enabled": False, "available": True})

    def test_missing_native_lock_preserves_unowned_initialization_behavior(self):
        namespace = load_diagnostics(ScriptedClocks([], []))
        owner = make_owner(namespace)
        del owner._native_lock
        token = object()
        self.assertIs(owner.process(token), token)
        self.assertEqual(owner.get_diagnostics_status()["operations"]["process"]["calls"], 0)

    def test_known_call_records_exact_wait_held_and_cpu_intervals(self):
        namespace = load_diagnostics(ScriptedClocks(
            [100, 2_000_100, 14_000_100], [1_000, 201_000]))
        owner = make_owner(namespace)
        token = object()
        self.assertIs(owner.process(token), token)
        status = owner.get_diagnostics_status()
        self.assertEqual(status["operations"]["process"], {
            "calls": 1, "failures": 0,
            "wait_ns": 2_000_000, "wait_max_ns": 2_000_000,
            "held_wall_ns": 12_000_000, "held_wall_max_ns": 12_000_000,
            "thread_cpu_ns": 200_000, "thread_cpu_max_ns": 200_000,
        })
        self.assertEqual(status["clock"], "time.monotonic_ns")
        self.assertIn("excludes_helpers", status["cpu_scope"])
        self.assertEqual(status["outliers"][0], {
            "sequence": 0, "operation": "process", "started_monotonic_ns": 100,
            "acquired_monotonic_ns": 2_000_100, "ended_monotonic_ns": 14_000_100,
            "wait_ns": 2_000_000, "held_wall_ns": 12_000_000,
            "thread_cpu_ns": 200_000, "failed": False,
        })

    def test_original_exception_identity_and_lock_release_are_preserved(self):
        namespace = load_diagnostics(ScriptedClocks([100, 200, 500], [1, 3]))
        owner = make_owner(namespace)
        failure = ValueError("simulated native operation failure")

        def raise_failure(value):
            raise failure

        owner.control = raise_failure
        with self.assertRaises(ValueError) as caught:
            owner.set_shimmer_feedback(0.3)
        self.assertIs(caught.exception, failure)
        acquired_elsewhere = []

        def inspect_released_lock():
            acquired = owner._native_lock.acquire(blocking=False)
            acquired_elsewhere.append(acquired)
            if acquired:
                owner._native_lock.release()

        thread = threading.Thread(target=inspect_released_lock)
        thread.start()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(acquired_elsewhere, [True])
        status = owner.get_diagnostics_status()
        self.assertEqual(status["operations"]["set_shimmer_feedback"]["failures"], 1)
        self.assertTrue(status["outliers"][0]["failed"])

    def test_nested_rlock_operations_report_inclusive_not_additive_time(self):
        namespace = load_diagnostics(ScriptedClocks(
            [100, 200, 300, 400, 500, 600], [10, 20, 30, 40]))
        owner = make_owner(namespace)
        self.assertEqual(owner.set_damp(0.5), 0.5)
        status = owner.get_diagnostics_status()
        self.assertEqual(status["operations"]["set_damp"]["held_wall_ns"], 400)
        self.assertEqual(status["operations"]["_mirror_zone"]["held_wall_ns"], 100)
        self.assertEqual(status["operations"]["set_damp"]["thread_cpu_ns"], 30)
        self.assertEqual(status["operations"]["_mirror_zone"]["thread_cpu_ns"], 10)
        self.assertEqual(status["nested_operations"], "inclusive_do_not_sum_across_operations")

    def test_collector_contention_never_blocks_audio_or_snapshot(self):
        namespace = load_diagnostics()
        owner = make_owner(namespace)
        diagnostics = owner._diagnostics
        with diagnostics._lock:
            token = object()
            self.assertIs(owner.process(token), token)
            unavailable = owner.get_diagnostics_status()
            self.assertFalse(unavailable["available"])
            self.assertTrue(unavailable["lock_contended"])
            self.assertEqual(unavailable["missed_records"], 1)
        status = owner.get_diagnostics_status()
        self.assertEqual(status["operations"]["process"]["calls"], 0)
        self.assertEqual(status["missed_records"], 1)

    def test_snapshot_does_not_take_native_lock_and_detaches_results(self):
        namespace = load_diagnostics()
        owner = make_owner(namespace)
        owner._diagnostics.record("process", 1, 2_000_001, 2_000_100, 3, 4, False)

        class ForbiddenNativeLock:
            def __enter__(self):
                raise AssertionError("Snapshot must not enter native lock")

            def acquire(self, *args, **kwargs):
                raise AssertionError("Snapshot must not acquire native lock")

        owner._native_lock = ForbiddenNativeLock()
        status = owner.get_diagnostics_status()
        status["operations"]["process"]["calls"] = -1
        status["outliers"][0]["wait_ns"] = -1
        fresh = owner.get_diagnostics_status()
        self.assertEqual(fresh["operations"]["process"]["calls"], 1)
        self.assertEqual(fresh["outliers"][0]["wait_ns"], 2_000_000)

    def test_outliers_and_operation_keys_are_bounded(self):
        namespace = load_diagnostics()
        diagnostics = namespace["_ReverbDiagnostics"]()
        capacity = namespace["_DIAGNOSTIC_OUTLIER_CAPACITY"]
        for index in range(capacity + 19):
            started = index * 20_000_000
            diagnostics.record("process", started, started + 1_000_000,
                               started + 12_000_000, 1, 100, False)
        diagnostics.record("unexpected_operation_name", 1, 2, 3, 1, 2, False)
        status = diagnostics.snapshot()
        self.assertEqual(len(status["outliers"]), capacity)
        self.assertEqual(status["outlier_total"], capacity + 19)
        self.assertEqual(status["outlier_evicted"], 19)
        self.assertEqual(status["outliers"][0]["sequence"], 19)
        self.assertEqual(status["outliers"][-1]["sequence"], capacity + 18)
        self.assertEqual(status["invalid_records"], 1)
        self.assertEqual(set(status["operations"]), set(namespace["_DIAGNOSTIC_OPERATIONS"]))
        self.assertEqual(status["operations"]["process"]["calls"], capacity + 19)

    def test_threshold_boundaries_and_invalid_measurements(self):
        namespace = load_diagnostics()
        diagnostics = namespace["_ReverbDiagnostics"]()
        wait = namespace["_DIAGNOSTIC_WAIT_THRESHOLD_NS"]
        held = namespace["_DIAGNOSTIC_HELD_THRESHOLD_NS"]
        diagnostics.record("process", 0, wait - 1, wait - 1 + held - 1, 0, 1, False)
        self.assertEqual(diagnostics.snapshot()["outlier_total"], 0)
        diagnostics.record("process", 0, wait, wait + held - 1, 0, 1, False)
        diagnostics.record("process", 0, 0, held, 0, 1, False)
        diagnostics.record("process", 0, 0, 0, 0, 0, True)
        for values in ((None, 0, 0, 0, 0), (True, 1, 1, 0, 0),
                       (-1, 0, 0, 0, 0), (3, 2, 4, 0, 0),
                       (1, 2, 1, 0, 0), (0, 0, 0, 2, 1)):
            diagnostics.record("process", *values, False)
        status = diagnostics.snapshot()
        self.assertEqual(status["operations"]["process"]["calls"], 4)
        self.assertEqual(status["outlier_total"], 3)
        self.assertEqual(status["invalid_records"], 6)

    def test_clock_and_record_failure_do_not_replace_audio_result_or_error(self):
        def bad_clock():
            raise OSError("simulated clock unavailable")

        namespace = load_diagnostics(SimpleNamespace(
            monotonic_ns=bad_clock, thread_time_ns=bad_clock))
        owner = make_owner(namespace)
        token = object()
        self.assertIs(owner.process(token), token)
        status = owner.get_diagnostics_status()
        self.assertEqual(status["diagnostic_errors"], 5)
        self.assertEqual(status["invalid_records"], 1)

        namespace = load_diagnostics()
        owner = make_owner(namespace)
        with patch.object(owner._diagnostics, "record", side_effect=RuntimeError("collector fault")):
            self.assertIs(owner.process(token), token)
            original = ValueError("original compute failure")

            def raise_original(value):
                raise original

            owner.compute = raise_original
            with self.assertRaises(ValueError) as caught:
                owner.process(token)
            self.assertIs(caught.exception, original)
        self.assertEqual(owner.get_diagnostics_status()["diagnostic_errors"], 2)

    def test_real_concurrency_measures_control_wait_without_changing_ownership(self):
        namespace = load_diagnostics()
        owner = make_owner(namespace)
        entered = threading.Event()
        release = threading.Event()
        control_started = threading.Event()
        control_done = threading.Event()
        errors = []

        def compute(value):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test compute release timed out")
            return value

        def run_render():
            try:
                owner.process(0.5)
            except Exception as exc:
                errors.append(exc)

        def run_control():
            try:
                owner.set_shimmer_feedback(0.3)
            except Exception as exc:
                errors.append(exc)
            finally:
                control_done.set()

        owner.compute = compute
        renderer = threading.Thread(target=run_render)
        controller = threading.Thread(target=run_control)

        def monotonic_with_control_entry():
            stamp = time.monotonic_ns()
            if threading.current_thread() is controller:
                # Signal from inside the real decorator's pre-lock clock,
                # not before calling it (which leaves a scheduling race).
                control_started.set()
            return stamp

        namespace["time"] = SimpleNamespace(
            monotonic_ns=monotonic_with_control_entry,
            thread_time_ns=time.thread_time_ns,
        )
        try:
            renderer.start()
            self.assertTrue(entered.wait(1))
            controller.start()
            self.assertTrue(control_started.wait(1))
            self.assertFalse(control_done.wait(0.03))
            # Snapshot remains available while the renderer owns native DSP.
            self.assertTrue(owner.get_diagnostics_status()["available"])
        finally:
            release.set()
            renderer.join(2)
            if controller.ident is not None:
                controller.join(2)
        self.assertFalse(renderer.is_alive())
        self.assertFalse(controller.is_alive())
        self.assertEqual(errors, [])
        stats = owner.get_diagnostics_status()["operations"]
        self.assertEqual(stats["process"]["calls"], 1)
        self.assertEqual(stats["set_shimmer_feedback"]["calls"], 1)
        self.assertGreater(stats["process"]["held_wall_ns"], 1_000_000)
        self.assertGreater(stats["set_shimmer_feedback"]["wait_ns"], 1_000_000)


if __name__ == "__main__":
    unittest.main()
