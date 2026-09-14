"""Small, bounded render-path telemetry.

``RenderMetrics`` has one real-time-adjacent writer.  ``record`` never waits
for a concurrent snapshot: if the fixed accumulator is busy, that telemetry
sample is counted as missed and the caller continues immediately.
"""
from __future__ import annotations

import math
import threading


_BIN_COUNT = 160
_QUANTILES = (("p50", 0.50), ("p95", 0.95), ("p99", 0.99),
              ("p99_9", 0.999))


class RenderMetrics:
    """Cumulative fixed-size histogram of whole render-cycle durations.

    Histogram bins are ``period_seconds / 100`` wide through 160% of the
    period, followed by one overflow bin. Ring fill is reported in blocks.
    """

    def __init__(self, period_seconds: float):
        try:
            period = float(period_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("period_seconds must be finite and positive") from exc
        bin_width = period / 100.0
        if (isinstance(period_seconds, bool) or not math.isfinite(period)
                or period <= 0.0 or bin_width <= 0.0):
            raise ValueError("period_seconds must be finite and positive")
        self.period_seconds = period
        self._bin_width = bin_width
        self._histogram = [0] * (_BIN_COUNT + 1)
        self._count = 0
        self._sum = 0.0
        self._max = 0.0
        self._over_budget = 0
        self._rejected_writes = 0
        self._min_ring_fill = None
        self._invalid_samples = 0
        self._missed_samples = 0
        self._lock = threading.Lock()

    def record(self, duration_seconds: float, ring_fill: float,
               written: bool = True) -> bool:
        """Record one cycle, returning false if telemetry was rejected/dropped.

        ``written=False`` is still a valid duration sample and increments the
        rejected-output-write counter. Invalid/non-finite inputs are rejected.
        """
        try:
            duration = float(duration_seconds)
            fill = float(ring_fill)
        except (TypeError, ValueError, OverflowError):
            self._invalid_samples += 1
            return False
        if (isinstance(duration_seconds, bool)
                or isinstance(ring_fill, bool)
                or not isinstance(written, bool)
                or not math.isfinite(duration) or duration < 0.0
                or not math.isfinite(fill) or fill < 0.0):
            self._invalid_samples += 1
            return False

        if not self._lock.acquire(blocking=False):
            self._missed_samples += 1
            return False
        try:
            if duration >= _BIN_COUNT * self._bin_width:
                index = _BIN_COUNT
            else:
                index = int(duration / self._bin_width)
            self._histogram[index] += 1
            self._count += 1
            self._sum += duration
            if duration > self._max:
                self._max = duration
            if duration > self.period_seconds:
                self._over_budget += 1
            if not written:
                self._rejected_writes += 1
            if self._min_ring_fill is None or fill < self._min_ring_fill:
                self._min_ring_fill = fill
            return True
        finally:
            self._lock.release()

    def snapshot(self) -> dict:
        """Return a short cumulative snapshot suitable for external differencing."""
        with self._lock:
            histogram = self._histogram.copy()
            count = self._count
            total = self._sum
            maximum = self._max
            over_budget = self._over_budget
            rejected_writes = self._rejected_writes
            min_ring_fill = self._min_ring_fill
            invalid_samples = self._invalid_samples
            missed_samples = self._missed_samples

        cumulative = []
        running = 0
        for bucket_count in histogram:
            running += bucket_count
            cumulative.append(running)

        quantile_bounds = {}
        for name, quantile in _QUANTILES:
            if count == 0:
                quantile_bounds[name] = {
                    "lower_seconds": None, "upper_seconds": None,
                }
                continue
            rank = max(1, math.ceil(quantile * count))
            index = next(i for i, value in enumerate(cumulative)
                         if value >= rank)
            quantile_bounds[name] = {
                "lower_seconds": index * self._bin_width,
                "upper_seconds": (
                    None if index == _BIN_COUNT
                    else (index + 1) * self._bin_width
                ),
            }

        return {
            "period_seconds": self.period_seconds,
            "duration_count": count,
            "duration_sum_seconds": total,
            "duration_max_seconds": maximum if count else None,
            "over_budget_count": over_budget,
            "rejected_write_count": rejected_writes,
            "min_ring_fill_blocks": min_ring_fill,
            "invalid_samples": invalid_samples,
            "missed_samples": missed_samples,
            "histogram": {
                "bin_width_seconds": self._bin_width,
                "upper_bounds_seconds": [
                    (i + 1) * self._bin_width for i in range(_BIN_COUNT)
                ] + [None],
                "cumulative_counts": cumulative,
            },
            "quantile_bounds": quantile_bounds,
        }
