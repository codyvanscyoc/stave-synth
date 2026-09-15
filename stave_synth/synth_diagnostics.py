"""Opt-in, bounded sub-stage timing for the single synth render writer."""

from collections import deque
import threading
import time


class SynthDiagnostics:
    """Collect paired wall/calling-thread CPU timings without blocking render."""

    CAPACITY = 64
    SPANS = ("preparation", "merged_native", "post_native_glue",
             "reverb", "post_mixing")

    def __init__(self, sample_rate, *, wall_clock=None, cpu_clock=None):
        if type(sample_rate) is not int or sample_rate <= 0:
            raise ValueError("sample_rate must be a positive integer")
        self.sample_rate = sample_rate
        self._wall_clock = wall_clock or time.monotonic_ns
        self._cpu_clock = cpu_clock or time.thread_time_ns
        self._lock = threading.Lock()
        self._active = None
        self._count = self._missed = self._invalid = self._incomplete = 0
        self._diagnostic_errors = 0
        self._zero_length = 0
        self._merged = self._nonmerged = self._overwritten = 0
        self._wall_sums = [0] * len(self.SPANS)
        self._cpu_sums = [0] * len(self.SPANS)
        self._wall_maxima = [0] * len(self.SPANS)
        self._cpu_maxima = [0] * len(self.SPANS)
        self._records = deque(maxlen=self.CAPACITY)

    def _now(self):
        try:
            return self._wall_clock(), self._cpu_clock()
        except Exception:
            self._diagnostic_errors += 1
            return None

    def _abandon(self):
        self._active = None
        self._reverb_end = None
        self._incomplete += 1

    def begin(self, n_samples):
        if self._active is not None:
            self._incomplete += 1
        now = self._now()
        if now is None:
            self._abandon()
            return False
        wall, cpu = now
        self._active = [n_samples, wall, cpu, None, None, None, None, None, None]
        self._reverb_end = None
        return True

    def record_zero_length(self):
        self._zero_length += 1

    def mark_merged_start(self):
        if self._active is not None:
            now = self._now()
            if now is None:
                self._abandon()
                return False
            self._active[3:5] = now
            return True
        return False

    def mark_merged_end(self):
        if self._active is not None:
            now = self._now()
            if now is None:
                self._abandon()
                return False
            self._active[5:7] = now
            return True
        return False

    def mark_reverb_start(self):
        if self._active is not None:
            now = self._now()
            if now is None:
                self._abandon()
                return False
            self._active[7:9] = now
            return True
        return False

    def abort(self):
        if self._active is not None:
            self._abandon()

    def complete(self):
        active = self._active
        if active is None:
            return False
        now = self._now()
        if now is None:
            self._abandon()
            return False
        self._active = None
        end_wall, end_cpu = now
        n_samples, start_wall, start_cpu = active[:3]
        merged_start_wall, merged_start_cpu = active[3:5]
        merged_end_wall, merged_end_cpu = active[5:7]
        reverb_start_wall, reverb_start_cpu = active[7:9]
        values = (n_samples, start_wall, start_cpu, reverb_start_wall,
                  reverb_start_cpu, end_wall, end_cpu)
        merged_available = merged_start_wall is not None or merged_end_wall is not None
        if merged_available:
            values += (merged_start_wall, merged_start_cpu,
                       merged_end_wall, merged_end_cpu)
        if (any(type(value) is not int or value < 0 for value in values)
                or n_samples <= 0):
            self._invalid += 1
            return False

        if merged_available:
            wall_points = (start_wall, merged_start_wall, merged_end_wall,
                           reverb_start_wall, end_wall)
            cpu_points = (start_cpu, merged_start_cpu, merged_end_cpu,
                          reverb_start_cpu, end_cpu)
            if (any(a > b for a, b in zip(wall_points, wall_points[1:]))
                    or any(a > b for a, b in zip(cpu_points, cpu_points[1:]))):
                self._invalid += 1
                return False
            wall_spans = (merged_start_wall - start_wall,
                          merged_end_wall - merged_start_wall,
                          reverb_start_wall - merged_end_wall,
                          0, 0)
            cpu_spans = (merged_start_cpu - start_cpu,
                         merged_end_cpu - merged_start_cpu,
                         reverb_start_cpu - merged_end_cpu,
                         0, 0)
        else:
            if start_wall > reverb_start_wall or start_cpu > reverb_start_cpu:
                self._invalid += 1
                return False
            # No merged call occurred: do not label fallback/tail preparation
            # as native. The first span explicitly contains all pre-reverb work.
            wall_spans = (reverb_start_wall - start_wall, 0, 0, 0, 0)
            cpu_spans = (reverb_start_cpu - start_cpu, 0, 0, 0, 0)

        reverb_end = getattr(self, "_reverb_end", None)
        self._reverb_end = None
        if reverb_end is None:
            self._invalid += 1
            return False
        reverb_end_wall, reverb_end_cpu = reverb_end
        if (type(reverb_end_wall) is not int or reverb_end_wall < 0
                or type(reverb_end_cpu) is not int or reverb_end_cpu < 0):
            self._invalid += 1
            return False
        if not (reverb_start_wall <= reverb_end_wall <= end_wall
                and reverb_start_cpu <= reverb_end_cpu <= end_cpu):
            self._invalid += 1
            return False
        wall_spans = (*wall_spans[:3],
                      reverb_end_wall - reverb_start_wall,
                      end_wall - reverb_end_wall)
        cpu_spans = (*cpu_spans[:3],
                     reverb_end_cpu - reverb_start_cpu,
                     end_cpu - reverb_end_cpu)

        if not self._lock.acquire(blocking=False):
            self._missed += 1
            return False
        try:
            self._count += 1
            self._merged += int(merged_available)
            self._nonmerged += int(not merged_available)
            for index, value in enumerate(wall_spans):
                self._wall_sums[index] += value
                self._wall_maxima[index] = max(self._wall_maxima[index], value)
            for index, value in enumerate(cpu_spans):
                self._cpu_sums[index] += value
                self._cpu_maxima[index] = max(self._cpu_maxima[index], value)
            total_wall = end_wall - start_wall
            if total_wall > n_samples * 1_000_000_000 // self.sample_rate:
                if len(self._records) == self.CAPACITY:
                    self._overwritten += 1
                self._records.append((self._count, merged_available,
                                      start_wall, end_wall, end_cpu - start_cpu,
                                      *wall_spans, *cpu_spans))
            return True
        finally:
            self._lock.release()

    def mark_reverb_end(self):
        if self._active is not None:
            now = self._now()
            if now is None:
                self._abandon()
                return False
            self._reverb_end = now
            return True
        return False

    def snapshot(self):
        with self._lock:
            rows = tuple(self._records)
            result = {
                "enabled": True,
                "wall_clock": "monotonic_ns",
                "cpu_clock": "thread_time_ns",
                "cpu_scope": "calling_thread_only",
                "count": self._count,
                "lost": self._missed,
                "missed": self._missed,
                "invalid": self._invalid,
                "incomplete": self._incomplete,
                "diagnostic_errors": self._diagnostic_errors,
                "zero_length_blocks": self._zero_length,
                "merged_blocks": self._merged,
                "nonmerged_blocks": self._nonmerged,
                "wall_sum_ns": list(self._wall_sums),
                "thread_cpu_sum_ns": list(self._cpu_sums),
                "wall_max_ns": list(self._wall_maxima),
                "thread_cpu_max_ns": list(self._cpu_maxima),
                "capacity": self.CAPACITY,
                "overwritten": self._overwritten,
            }
        result["spans"] = list(self.SPANS)
        result["nonmerged_attribution"] = (
            "preparation includes all work before reverb; merged_native and "
            "post_native_glue are unavailable and reported as zero")
        result["record_fields"] = [
            "sample", "merged_native_available", "started_monotonic_ns",
            "ended_monotonic_ns", "thread_cpu_ns",
            *["wall_" + name + "_ns" for name in self.SPANS],
            *["thread_cpu_" + name + "_ns" for name in self.SPANS],
        ]
        result["records"] = [list(row) for row in rows]
        return result
