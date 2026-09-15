"""Opt-in, bounded render attribution; never an end-to-end latency measure.

The single render writer tries a separate telemetry lock and drops a sample
instead of waiting. Snapshots copy bounded data only. Calling-thread CPU does
not include native helper threads; wall minus CPU is not a mutex-wait measure.
"""
from collections import deque
import threading


class RenderDiagnostics:
    CAPACITY = 128
    BINS = 160

    def __init__(self, period_ns):
        if type(period_ns) is not int or period_ns <= 0:
            raise ValueError("period_ns must be a positive integer")
        self.period_ns = period_ns
        self._lock = threading.Lock()
        self._count = self._missed = self._invalid = self._overwritten = 0
        self._wall_sum = self._cpu_sum = self._wall_max = self._cpu_max = 0
        self._stage_sums = [0, 0, 0]
        self._stage_maxima = [0, 0, 0]
        self._cpu_bins = [0] * (self.BINS + 1)
        self._records = deque(maxlen=self.CAPACITY)
        self._previous_end = None
        self._minimum_pre_fill = None

    def record(self, started_ns, ended_ns, cpu_ns, piano_end_ns, synth_end_ns,
               pre_fill, post_fill):
        values = (started_ns, ended_ns, cpu_ns, piano_end_ns, synth_end_ns,
                  pre_fill, post_fill)
        if (any(type(value) is not int or value < 0 for value in values)
                or not started_ns <= piano_end_ns <= synth_end_ns <= ended_ns):
            self._invalid += 1
            return False
        # Single-writer cursor advances even when a snapshot rejects this
        # telemetry sample. Otherwise the next gap would include lost renders.
        gap_ns = (None if self._previous_end is None
                  else max(0, started_ns - self._previous_end))
        self._previous_end = ended_ns
        if not self._lock.acquire(blocking=False):
            self._missed += 1
            return False
        try:
            wall_ns = ended_ns - started_ns
            stages = (piano_end_ns - started_ns,
                      synth_end_ns - piano_end_ns, ended_ns - synth_end_ns)
            # This gap includes intentional ring-fill sleeps and telemetry.
            # It must NOT be labelled scheduler delay or note latency.
            self._count += 1
            self._wall_sum += wall_ns
            self._cpu_sum += cpu_ns
            self._wall_max = max(self._wall_max, wall_ns)
            self._cpu_max = max(self._cpu_max, cpu_ns)
            for index in range(3):
                self._stage_sums[index] += stages[index]
                self._stage_maxima[index] = max(self._stage_maxima[index], stages[index])
            self._cpu_bins[min(self.BINS, cpu_ns * 100 // self.period_ns)] += 1
            if self._minimum_pre_fill is None or pre_fill < self._minimum_pre_fill:
                self._minimum_pre_fill = pre_fill
            if wall_ns > self.period_ns or (gap_ns is not None and gap_ns > self.period_ns):
                if len(self._records) == self.CAPACITY:
                    self._overwritten += 1
                self._records.append((self._count, started_ns, ended_ns, cpu_ns,
                                      *stages, gap_ns, pre_fill, post_fill))
            return True
        finally:
            self._lock.release()

    def snapshot(self):
        with self._lock:
            rows = tuple(self._records)
            result = {
                "enabled": True, "clock": "monotonic_ns", "period_ns": self.period_ns,
                "count": self._count, "missed": self._missed, "invalid": self._invalid,
                "wall_sum_ns": self._wall_sum, "thread_cpu_sum_ns": self._cpu_sum,
                "wall_max_ns": self._wall_max, "thread_cpu_max_ns": self._cpu_max,
                "stage_sum_ns": list(self._stage_sums),
                "stage_max_ns": list(self._stage_maxima),
                "thread_cpu_histogram_counts": list(self._cpu_bins),
                "min_pre_ring_fill_blocks": self._minimum_pre_fill,
                "capacity": self.CAPACITY, "overwritten": self._overwritten,
            }
        result["stages"] = ["piano_and_sends", "synth", "master_and_write"]
        result["thread_cpu_histogram_bin_width_ns"] = self.period_ns / 100
        result["record_fields"] = [
            "sample", "started_monotonic_ns", "ended_monotonic_ns", "thread_cpu_ns",
            "piano_and_sends_ns", "synth_ns", "master_and_write_ns",
            "gap_since_previous_render_attempt_ns", "pre_ring_fill_blocks", "post_ring_fill_blocks",
        ]
        result["records"] = [list(row) for row in rows]
        return result
