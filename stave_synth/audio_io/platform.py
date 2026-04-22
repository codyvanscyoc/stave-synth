"""Platform-gated helpers for Linux-only syscalls used by the render
and GC threads.

On Mac these become no-ops or use equivalent BSD/Mach APIs:
  - SCHED_FIFO: Mac uses thread_policy_set(THREAD_TIME_CONSTRAINT_POLICY).
    First pass is a no-op — PortAudio manages its own realtime callback
    thread via Core Audio, so our Python render thread is less critical.
  - malloc_trim: glibc-only. Mac's allocator (libmalloc) doesn't expose
    an equivalent; returns None so callers skip the trim step.
  - /proc/self/status: Linux-only. Mac uses resource.getrusage() which
    gives a compatible RSS number.
"""
import logging
import sys

logger = logging.getLogger(__name__)

_IS_LINUX = sys.platform.startswith("linux")


def set_realtime_priority(priority: int = 80) -> bool:
    """Request realtime scheduling for the current thread.

    Returns True on success, False on failure or unsupported platform.
    Never raises — caller just logs and proceeds.
    """
    if _IS_LINUX:
        import os
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(priority))
            logger.info("render thread: SCHED_FIFO priority %d", priority)
            return True
        except Exception as e:
            logger.warning("couldn't set realtime priority: %s", e)
            return False

    # macOS: thread_policy_set(THREAD_TIME_CONSTRAINT_POLICY) would be the
    # right call, but PortAudio already runs its audio callback in a Core
    # Audio managed RT thread. Leaving our Python helper thread at normal
    # priority is fine until we profile Mac xruns.
    logger.info("set_realtime_priority: skipped (platform=%s)", sys.platform)
    return False


def try_malloc_trim():
    """Return a callable that trims allocator pages, or None if unsupported.

    Usage:
        trim = try_malloc_trim()
        if trim:
            trim()  # or trim(0) — caller passes pad bytes
    """
    if not _IS_LINUX:
        return None
    import ctypes
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=False)
        libc.malloc_trim.argtypes = [ctypes.c_size_t]
        libc.malloc_trim.restype = ctypes.c_int

        def _trim(pad: int = 0) -> int:
            return libc.malloc_trim(pad)
        return _trim
    except Exception as e:
        logger.info("malloc_trim unavailable (not glibc?): %s", e)
        return None


def get_rss_kb() -> int:
    """Return current process RSS in KB, or 0 if unavailable."""
    if _IS_LINUX:
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1])
        except Exception:
            pass
        return 0

    try:
        import resource
        ru = resource.getrusage(resource.RUSAGE_SELF)
        # macOS returns ru_maxrss in bytes; Linux in KB. We only hit this
        # branch on non-Linux, so assume bytes.
        return int(ru.ru_maxrss / 1024)
    except Exception:
        return 0
