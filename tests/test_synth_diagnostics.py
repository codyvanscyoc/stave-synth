"""Deterministic tests for opt-in synth sub-stage attribution."""

import ast
import unittest
from pathlib import Path

from stave_synth.synth_diagnostics import SynthDiagnostics


ROOT = Path(__file__).parents[1]
SYNTH_SOURCE = ROOT / "stave_synth" / "synth_engine.py"


class PairedClocks:
    def __init__(self, wall, cpu):
        self.wall_values = iter(wall)
        self.cpu_values = iter(cpu)

    def wall(self):
        value = next(self.wall_values)
        if isinstance(value, Exception):
            raise value
        return value

    def cpu(self):
        value = next(self.cpu_values)
        if isinstance(value, Exception):
            raise value
        return value


def diagnostic(wall, cpu, sample_rate=48_000):
    clocks = PairedClocks(wall, cpu)
    return SynthDiagnostics(sample_rate, wall_clock=clocks.wall,
                            cpu_clock=clocks.cpu)


class SynthDiagnosticsTests(unittest.TestCase):
    def test_merged_cycle_reports_five_paired_spans(self):
        diag = diagnostic(
            [0, 10_000, 30_000, 50_000, 70_000, 90_000],
            [0, 8_000, 25_000, 40_000, 58_000, 75_000])
        diag.begin(512)
        diag.mark_merged_start()
        diag.mark_merged_end()
        diag.mark_reverb_start()
        diag.mark_reverb_end()
        self.assertTrue(diag.complete())
        status = diag.snapshot()
        self.assertEqual(status["spans"], [
            "preparation", "merged_native", "post_native_glue",
            "reverb", "post_mixing"])
        self.assertEqual(status["wall_sum_ns"],
                         [10_000, 20_000, 20_000, 20_000, 20_000])
        self.assertEqual(status["thread_cpu_sum_ns"],
                         [8_000, 17_000, 15_000, 18_000, 17_000])
        self.assertEqual(status["merged_blocks"], 1)
        self.assertEqual(status["nonmerged_blocks"], 0)
        self.assertEqual(status["records"], [])

    def test_nonmerged_cycle_does_not_claim_native_timing(self):
        diag = diagnostic([0, 50_000, 70_000, 90_000],
                          [0, 40_000, 58_000, 75_000])
        diag.begin(512)
        diag.mark_reverb_start()
        diag.mark_reverb_end()
        self.assertTrue(diag.complete())
        status = diag.snapshot()
        self.assertEqual(status["wall_sum_ns"],
                         [50_000, 0, 0, 20_000, 20_000])
        self.assertEqual(status["thread_cpu_sum_ns"],
                         [40_000, 0, 0, 18_000, 17_000])
        self.assertEqual(status["nonmerged_blocks"], 1)
        self.assertIn("unavailable", status["nonmerged_attribution"])

    def test_abort_is_incomplete_not_a_completed_or_invalid_cycle(self):
        diag = diagnostic([0], [0])
        diag.begin(512)
        diag.abort()
        status = diag.snapshot()
        self.assertEqual(status["count"], 0)
        self.assertEqual(status["invalid"], 0)
        self.assertEqual(status["incomplete"], 1)

    def test_zero_length_return_is_counted_without_clocks_or_fake_cycle(self):
        diag = diagnostic([], [])
        diag.record_zero_length()
        status = diag.snapshot()
        self.assertEqual(status["zero_length_blocks"], 1)
        self.assertEqual(status["count"], 0)
        self.assertEqual(status["incomplete"], 0)

    def test_invalid_order_is_rejected(self):
        diag = diagnostic([100, 90, 110, 120], [100, 90, 110, 120])
        diag.begin(512)
        diag.mark_reverb_start()
        diag.mark_reverb_end()
        self.assertFalse(diag.complete())
        self.assertEqual(diag.snapshot()["invalid"], 1)

    def test_clock_failure_at_begin_is_contained_and_next_cycle_recovers(self):
        diag = diagnostic(
            [RuntimeError("wall unavailable"), 0, 50_000, 70_000, 90_000],
            [0, 40_000, 58_000, 75_000])
        self.assertFalse(diag.begin(512))
        self.assertFalse(diag.complete())
        self.assertTrue(diag.begin(512))
        diag.mark_reverb_start()
        diag.mark_reverb_end()
        self.assertTrue(diag.complete())
        status = diag.snapshot()
        self.assertEqual(status["diagnostic_errors"], 1)
        self.assertEqual(status["incomplete"], 1)
        self.assertEqual(status["count"], 1)

    def test_clock_failure_midcycle_is_contained_and_abandons_cycle(self):
        diag = diagnostic(
            [0, RuntimeError("checkpoint failed"), 100_000, 150_000,
             170_000, 190_000],
            [0, 100_000, 140_000, 158_000, 175_000])
        self.assertTrue(diag.begin(512))
        self.assertFalse(diag.mark_merged_start())
        self.assertFalse(diag.mark_reverb_start())
        self.assertFalse(diag.complete())
        self.assertTrue(diag.begin(512))
        diag.mark_reverb_start()
        diag.mark_reverb_end()
        self.assertTrue(diag.complete())
        status = diag.snapshot()
        self.assertEqual(status["diagnostic_errors"], 1)
        self.assertEqual(status["incomplete"], 1)
        self.assertEqual(status["count"], 1)

    def test_clock_failure_at_complete_is_contained(self):
        diag = diagnostic(
            [0, 50_000, 70_000, RuntimeError("complete failed")],
            [0, 40_000, 58_000])
        diag.begin(512)
        diag.mark_reverb_start()
        diag.mark_reverb_end()
        self.assertFalse(diag.complete())
        status = diag.snapshot()
        self.assertEqual(status["diagnostic_errors"], 1)
        self.assertEqual(status["incomplete"], 1)
        self.assertEqual(status["count"], 0)

    def test_malformed_reverb_checkpoint_is_invalid_not_an_exception(self):
        diag = diagnostic([0, 50_000, "bad", 90_000],
                          [0, 40_000, 58_000, 75_000])
        diag.begin(512)
        diag.mark_reverb_start()
        diag.mark_reverb_end()
        self.assertFalse(diag.complete())
        status = diag.snapshot()
        self.assertEqual(status["invalid"], 1)
        self.assertEqual(status["diagnostic_errors"], 0)
        self.assertEqual(status["count"], 0)

    def test_render_writer_never_waits_for_snapshot_lock(self):
        diag = diagnostic([0, 10, 20, 30], [0, 10, 20, 30],
                          sample_rate=1_000_000_000)
        diag.begin(1)
        diag.mark_reverb_start()
        diag.mark_reverb_end()
        diag._lock.acquire()
        try:
            self.assertFalse(diag.complete())
        finally:
            diag._lock.release()
        status = diag.snapshot()
        self.assertEqual(status["count"], 0)
        self.assertEqual(status["lost"], 1)
        self.assertEqual(status["missed"], 1)

    def test_overrun_rows_are_bounded_and_normal_cycles_are_not_saved(self):
        values = []
        for index in range(70):
            base = index * 1_000
            values.extend((base, base + 100, base + 200, base + 900))
        diag = diagnostic(values, values, sample_rate=1_000_000_000)
        for _ in range(70):
            diag.begin(500)
            diag.mark_reverb_start()
            diag.mark_reverb_end()
            self.assertTrue(diag.complete())
        status = diag.snapshot()
        self.assertEqual(status["count"], 70)
        self.assertEqual(len(status["records"]), diag.CAPACITY)
        self.assertEqual(status["overwritten"], 70 - diag.CAPACITY)

    def test_source_places_checkpoints_around_exact_native_calls(self):
        tree = ast.parse(SYNTH_SOURCE.read_text(encoding="utf-8"))
        engine = next(node for node in tree.body
                      if isinstance(node, ast.ClassDef) and node.name == "SynthEngine")
        render_locked = next(node for node in engine.body
                             if isinstance(node, ast.FunctionDef)
                             and node.name == "_render_locked")
        calls = [node for node in ast.walk(render_locked) if isinstance(node, ast.Call)]
        attrs = [node.func.attr for node in calls if isinstance(node.func, ast.Attribute)]
        self.assertEqual(attrs.count("mark_merged_start"), 1)
        self.assertEqual(attrs.count("mark_merged_end"), 1)
        self.assertEqual(attrs.count("mark_reverb_start"), 1)
        self.assertEqual(attrs.count("mark_reverb_end"), 1)
        self.assertEqual(attrs.count("complete"), 2)

        render = next(node for node in engine.body
                      if isinstance(node, ast.FunctionDef) and node.name == "render")
        render_attrs = [node.func.attr for node in ast.walk(render)
                        if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)]
        self.assertEqual(render_attrs.count("abort"), 1)

        text = SYNTH_SOURCE.read_text(encoding="utf-8")
        self.assertLess(text.index("mark_merged_start()"),
                        text.index("self._faust_merged.process(n_samples)"))
        self.assertLess(text.index("self._faust_merged.process(n_samples)"),
                        text.index("mark_merged_end()"))
        self.assertLess(text.index("mark_reverb_start()"),
                        text.index("self.reverb.process(self._stereo_out"))
        self.assertLess(text.index("self.reverb.process(self._stereo_out"),
                        text.index("mark_reverb_end()"))

    def test_source_enables_profiler_only_for_exact_opt_in(self):
        text = SYNTH_SOURCE.read_text(encoding="utf-8")
        self.assertIn('os.environ.get("STAVE_DIAGNOSTICS") == "1"', text)
        self.assertIn("self._synth_diagnostics = None", text)


if __name__ == "__main__":
    unittest.main()
