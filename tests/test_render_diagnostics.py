import unittest
from stave_synth.render_diagnostics import RenderDiagnostics


class RenderDiagnosticsTests(unittest.TestCase):
    def test_stage_sums_cpu_histogram_and_outlier(self):
        metrics = RenderDiagnostics(100)
        self.assertTrue(metrics.record(1000, 1120, 70, 1030, 1090, 2, 3))
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["stage_sum_ns"], [30, 60, 30])
        self.assertEqual(snapshot["thread_cpu_sum_ns"], 70)
        self.assertEqual(snapshot["thread_cpu_histogram_counts"][70], 1)
        self.assertEqual(snapshot["min_pre_ring_fill_blocks"], 2)
        self.assertEqual(snapshot["records"], [[1, 1000, 1120, 70, 30, 60, 30, None, 2, 3]])

    def test_records_are_bounded_and_detached(self):
        metrics = RenderDiagnostics(100)
        for index in range(140):
            base = index * 200
            metrics.record(base, base + 120, 170, base + 30, base + 60, 1, 2)
        snapshot = metrics.snapshot()
        self.assertEqual(len(snapshot["records"]), 128)
        self.assertEqual(snapshot["overwritten"], 12)
        self.assertEqual(snapshot["thread_cpu_histogram_counts"][-1], 140)
        snapshot["records"][0][0] = -1
        snapshot["stage_sum_ns"][0] = -1
        self.assertNotEqual(metrics.snapshot()["records"][0][0], -1)
        self.assertEqual(metrics.snapshot()["stage_sum_ns"][0], 4200)

    def test_writer_never_waits_for_snapshot(self):
        metrics = RenderDiagnostics(100)
        with metrics._lock:
            self.assertFalse(metrics.record(100, 110, 5, 103, 106, 2, 3))
        self.assertEqual(metrics.snapshot()["missed"], 1)
        self.assertEqual(metrics.snapshot()["count"], 0)

    def test_missed_telemetry_does_not_inflate_next_render_gap(self):
        metrics = RenderDiagnostics(100)
        metrics.record(100, 180, 50, 120, 150, 2, 3)
        with metrics._lock:
            metrics.record(200, 280, 50, 220, 250, 2, 3)
        metrics.record(300, 430, 50, 320, 350, 2, 3)
        self.assertEqual(metrics.snapshot()["records"][-1][7], 20)

    def test_invalid_data_and_periods_are_explicit(self):
        for period in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                RenderDiagnostics(period)
        metrics = RenderDiagnostics(100)
        self.assertFalse(metrics.record(100, 110, 5, 99, 106, 2, 3))
        self.assertFalse(metrics.record(100, 110, -5, 103, 106, 2, 3))
        self.assertFalse(metrics.record(100, 110, 5, 103, 106, True, 3))
        self.assertEqual(metrics.snapshot()["invalid"], 3)

    def test_gap_includes_intentional_idle_not_claimed_as_scheduler_delay(self):
        metrics = RenderDiagnostics(100)
        metrics.record(100, 110, 5, 103, 106, 2, 3)
        metrics.record(300, 310, 5, 303, 306, 1, 2)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["records"][0][7], 190)
        self.assertEqual(snapshot["min_pre_ring_fill_blocks"], 1)


if __name__ == "__main__":
    unittest.main()
