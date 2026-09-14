import math
import threading
import unittest

from stave_synth.render_metrics import RenderMetrics


class RenderMetricsTests(unittest.TestCase):
    def test_validates_period_and_samples(self):
        for value in (False, 0, -1, math.nan, math.inf, 5e-324, 10 ** 1000):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    RenderMetrics(value)

        metrics = RenderMetrics(0.01)
        bad = ((math.nan, 1, True), (math.inf, 1, True), (-0.1, 1, True),
               (0.001, math.nan, True), (0.001, -1, True),
               (True, 1, True), (0.001, False, True),
               (0.001, 1, 1))
        for args in bad:
            self.assertFalse(metrics.record(*args))
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["invalid_samples"], len(bad))
        self.assertEqual(snapshot["duration_count"], 0)

    def test_histogram_boundaries_counters_and_units(self):
        metrics = RenderMetrics(0.01)
        self.assertTrue(metrics.record(0.0, 8))
        self.assertTrue(metrics.record(0.0001, 7))
        self.assertTrue(metrics.record(0.01, 4, written=False))
        self.assertTrue(metrics.record(0.010001, 3))
        self.assertTrue(metrics.record(0.016, 2))
        snapshot = metrics.snapshot()

        self.assertEqual(snapshot["duration_count"], 5)
        self.assertAlmostEqual(snapshot["duration_sum_seconds"], 0.036101)
        self.assertEqual(snapshot["duration_max_seconds"], 0.016)
        self.assertEqual(snapshot["over_budget_count"], 2)
        self.assertEqual(snapshot["rejected_write_count"], 1)
        self.assertEqual(snapshot["min_ring_fill_blocks"], 2.0)
        histogram = snapshot["histogram"]
        self.assertEqual(histogram["bin_width_seconds"], 0.0001)
        self.assertEqual(len(histogram["upper_bounds_seconds"]), 161)
        self.assertEqual(len(histogram["cumulative_counts"]), 161)
        self.assertEqual(histogram["cumulative_counts"][-1], 5)
        self.assertIsNone(histogram["upper_bounds_seconds"][-1])

    def test_quantiles_are_bucket_bounds_including_overflow(self):
        metrics = RenderMetrics(1.0)
        for _ in range(19):
            metrics.record(0.501, 1)
        metrics.record(2.0, 1)
        bounds = metrics.snapshot()["quantile_bounds"]
        self.assertEqual(bounds["p50"], {
            "lower_seconds": 0.5, "upper_seconds": 0.51,
        })
        self.assertEqual(bounds["p99"], {
            "lower_seconds": 1.6, "upper_seconds": None,
        })

    def test_record_never_waits_for_snapshot_lock(self):
        metrics = RenderMetrics(0.01)
        metrics._lock.acquire()
        try:
            self.assertFalse(metrics.record(0.001, 4))
        finally:
            metrics._lock.release()
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["missed_samples"], 1)
        self.assertEqual(snapshot["duration_count"], 0)

    def test_concurrent_snapshots_keep_fixed_consistent_shape(self):
        metrics = RenderMetrics(0.01)
        finished = threading.Event()
        snapshots = []

        def reader():
            while not finished.is_set():
                snapshots.append(metrics.snapshot())

        thread = threading.Thread(target=reader)
        thread.start()
        attempts = 5000
        try:
            for _ in range(attempts):
                metrics.record(0.005, 6)
        finally:
            finished.set()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

        final = metrics.snapshot()
        self.assertEqual(final["duration_count"] + final["missed_samples"], attempts)
        for snapshot in snapshots:
            cumulative = snapshot["histogram"]["cumulative_counts"]
            self.assertEqual(len(cumulative), 161)
            self.assertEqual(cumulative[-1], snapshot["duration_count"])
            self.assertTrue(all(a <= b for a, b in zip(cumulative, cumulative[1:])))


if __name__ == "__main__":
    unittest.main()
