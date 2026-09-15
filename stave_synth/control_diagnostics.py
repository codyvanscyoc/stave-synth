"""Bounded, opt-in control/save attribution, independent of audio ownership.

Calling-thread CPU excludes native helper threads. Wall minus CPU does not
identify GIL, scheduler, mutex or storage delay. Completed spans are published
after the operation; an operation still running is not a completed sample.
"""
from collections import deque
from contextlib import contextmanager
import sys
import threading
import time


class ControlDiagnostics:
    OPERATIONS = (
        "control_snapshot", "control_dispatch", "control_response_detach",
        "autosave_snapshot", "autosave_save",
    )
    CAPACITY_PER_GROUP = 64
    CONTROL_THRESHOLD_NS = 1_000_000

    def __init__(self):
        self._lock = threading.Lock()
        self._operations = {
            name: dict(calls=0, failures=0, incomplete=0, complete=0,
                       wall_sum_ns=0, wall_max_ns=0,
                       thread_cpu_sum_ns=0, thread_cpu_max_ns=0)
            for name in self.OPERATIONS
        }
        # Frequent slider spans cannot evict the rare autosave observations.
        self._records = {name: deque(maxlen=self.CAPACITY_PER_GROUP)
                         for name in ("control", "autosave")}
        self._evicted = dict(control=0, autosave=0)
        self._retained_total = dict(control=0, autosave=0)
        self._sequence = 0
        self._dropped = self._incomplete = self._errors = 0

    def _clock(self, name):
        try:
            return getattr(time, name)()
        except Exception:
            self._errors += 1
            return None

    @contextmanager
    def span(self, operation):
        """Observe a fixed operation without changing its return/exception."""
        started = self._clock("monotonic_ns")
        cpu_started = self._clock("thread_time_ns")
        failed = True
        try:
            yield
            failed = False
        finally:
            cpu_ended = self._clock("thread_time_ns")
            ended = self._clock("monotonic_ns")
            try:
                self._record(operation, started, ended,
                             cpu_started, cpu_ended, failed)
            except Exception:
                # No logging, application lock, or replacement exception.
                self._errors += 1
                self._dropped += 1

    def _record(self, operation, started, ended, cpu_started, cpu_ended, failed):
        if operation not in self._operations:
            self._errors += 1
            self._dropped += 1
            return
        values = (started, ended, cpu_started, cpu_ended)
        complete = (all(type(value) is int and value >= 0 for value in values)
                    and ended >= started and cpu_ended >= cpu_started)
        if not self._lock.acquire(blocking=False):
            self._dropped += 1
            return
        try:
            self._sequence += 1
            stats = self._operations[operation]
            stats["calls"] += 1
            stats["failures"] += int(failed)
            stats["incomplete"] += int(not complete)
            stats["complete"] += int(complete)
            self._incomplete += int(not complete)
            wall = ended - started if complete else None
            cpu = cpu_ended - cpu_started if complete else None
            if complete:
                stats["wall_sum_ns"] += wall
                stats["wall_max_ns"] = max(stats["wall_max_ns"], wall)
                stats["thread_cpu_sum_ns"] += cpu
                stats["thread_cpu_max_ns"] = max(stats["thread_cpu_max_ns"], cpu)
            group = "autosave" if operation.startswith("autosave_") else "control"
            if (group == "autosave" or not complete or failed
                    or wall >= self.CONTROL_THRESHOLD_NS):
                rows = self._records[group]
                self._evicted[group] += int(len(rows) == self.CAPACITY_PER_GROUP)
                self._retained_total[group] += 1
                # Normalize failed clocks to JSON-safe null, not arbitrary data.
                start_value = started if type(started) is int and started >= 0 else None
                end_value = ended if type(ended) is int and ended >= 0 else None
                rows.append((self._sequence, operation, start_value, end_value,
                             cpu, bool(failed), complete))
        finally:
            self._lock.release()

    def snapshot(self):
        """Try a separate telemetry lock; never acquire an application lock."""
        try:
            interval = sys.getswitchinterval()
        except Exception:
            self._errors += 1
            interval = None
        base = {
            "enabled": True, "switch_interval_seconds": interval,
            "dropped_spans": self._dropped, "incomplete_spans": self._incomplete,
            "diagnostic_errors": self._errors,
        }
        if not self._lock.acquire(blocking=False):
            return {**base, "available": False, "lock_contended": True}
        try:
            result = {
                **base, "available": True, "lock_contended": False,
                "clock": "time.monotonic_ns", "cpu_clock": "time.thread_time_ns",
                "cpu_scope": "calling_thread_excludes_helpers",
                "format": 1, "count": self._sequence,
                "counts_exclude_dropped_spans": True,
                "sums_include_complete_spans_only": True,
                "failure_policy": "operation_raised_exception_not_error_response",
                "control_scope": "excludes_validation_controller_lock_wait_and_rollback",
                "capacity_per_group": self.CAPACITY_PER_GROUP,
                "control_threshold_ns": self.CONTROL_THRESHOLD_NS,
                "record_policy": "control_wall_at_least_threshold_or_failed_or_incomplete;autosave_all",
                "operations": {key: value.copy() for key, value in self._operations.items()},
                "evicted": self._evicted.copy(),
                "retained_total": self._retained_total.copy(),
                "records": {key: list(value) for key, value in self._records.items()},
            }
        finally:
            self._lock.release()
        result["record_fields"] = [
            "sequence", "operation", "started_monotonic_ns", "ended_monotonic_ns",
            "thread_cpu_ns", "failed", "complete",
        ]
        return result
