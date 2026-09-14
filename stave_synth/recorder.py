"""Bounded, per-take WAV recording of the Stave Synth master output.

``feed`` copies stereo blocks into the current take's bounded queue, without
disk I/O or helper threads. Each writer owns its queue, WAV and lifecycle;
stopping never transfers those resources to another take. ``stop`` has a
bounded wait and reports a still-running writer honestly. A new take cannot
start until the previous writer has finished.

Takes use ``YYYY-MM-DD_HH-MM-SS_<unique>.wav`` in RECORDINGS_DIR, with an
atomic ``.state.json`` start snapshot and ``.meta.json`` integrity record.
Queue loss and write/close errors make a take incomplete, not silently
successful. Files from before lifecycle metadata was introduced remain
readable, but their recording integrity is unknown (``status=legacy``).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import tempfile
import time
import wave
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import numpy as np

from .config import SAMPLE_RATE, DATA_DIR
from .state_store import atomic_write_json

logger = logging.getLogger(__name__)

RECORDINGS_DIR = DATA_DIR / "recordings"
MAX_QUEUE = 400  # ~2 s of 256-sample blocks at 48 kHz
MAX_TAKE_SECONDS = 30 * 60
MAX_RECORDINGS_BYTES = 1024 * 1024 * 1024
STOP_TIMEOUT_SECONDS = 3.0

# Shared by the in-process library handlers and all Recorder instances. Keep
# a take registered until the writer's LAST file operation has completed,
# including after a stop timeout. The application's instance lock provides
# process isolation; this registry is not a cross-process filesystem lock.
_LIVE_TAKES = {}
_READ_CLAIMS = {}
_LIBRARY_LOCK = threading.Lock()


def _take_path(filename: str) -> Optional[Path]:
    if (not isinstance(filename, str) or not filename.endswith(".wav")
            or "/" in filename or "\\" in filename or ".." in filename):
        return None
    return (RECORDINGS_DIR / filename).absolute()


def _json_object(value: dict) -> dict:
    """Make a stable JSON-object snapshot; reject non-finite/non-JSON state."""
    if not isinstance(value, dict):
        raise ValueError("Recording state must be a JSON object")
    return json.loads(json.dumps(value, allow_nan=False))


def _load_object(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        # A round trip with allow_nan=False also rejects NaN/Infinity accepted
        # by Python's decoder, including values nested in lists or objects.
        return _json_object(json.load(stream))


def _load_final_metadata(path: Path) -> dict:
    meta = _load_object(path)
    if meta.get("status") not in {"complete", "incomplete", "error"}:
        raise ValueError("Take was interrupted before finalization")
    if meta.get("metadata_version") != 1:
        raise ValueError("Unknown recording metadata version")
    counters = ("frames", "input_frames", "accepted_frames", "dropped_blocks",
                "dropped_frames", "write_errors", "stop_timeouts", "sample_rate")
    if any(type(meta.get(key)) is not int or meta[key] < 0 for key in counters):
        raise ValueError("Invalid recording frame/error counters")
    flags = ("recording", "writer_pending", "complete", "finalized",
             "audio_synced", "metadata_saved", "state_saved")
    if any(type(meta.get(key)) is not bool for key in flags):
        raise ValueError("Invalid recording lifecycle flags")
    if (not meta["sample_rate"] or meta["recording"] or meta["writer_pending"]
            or type(meta.get("duration_seconds")) not in (int, float)
            or meta["duration_seconds"] < 0
            or not isinstance(meta.get("errors"), list)
            or any(not isinstance(error, str) for error in meta["errors"])):
        raise ValueError("Invalid final recording metadata")
    if (meta["status"] == "complete") != meta["complete"]:
        raise ValueError("Inconsistent recording completion status")
    if meta["complete"] and not (
            meta["finalized"] and meta["audio_synced"] and meta["metadata_saved"]
            and meta["dropped_blocks"] == meta["dropped_frames"] == meta["write_errors"] == 0
            and meta["frames"] == meta["accepted_frames"] == meta["input_frames"]
            and not meta["errors"] and not meta.get("error")):
        raise ValueError("Inconsistent take metadata")
    return meta


class _Take:
    def __init__(self, path, wav_file, started_at, sample_rate):
        self.path = path
        self.wav = wav_file
        self.started_at = started_at
        self.sample_rate = sample_rate
        self.queue = queue.Queue(maxsize=MAX_QUEUE)
        self.stop_event = threading.Event()
        self.done = threading.Event()
        # Only short state/queue bookkeeping is protected. No file operation
        # or conversion holds this lock, so feed never waits for disk I/O.
        self.lock = threading.Lock()
        self.thread = None
        self.accepting = True
        self.input_frames = 0
        self.accepted_frames = 0
        self.frames_written = 0
        self.dropped_blocks = 0
        self.dropped_frames = 0
        self.write_errors = 0
        self.errors = []
        self.stop_reason = None
        self.stop_timeouts = 0
        self.finalized = False
        self.audio_synced = False
        self.metadata_saved = False
        self.state_saved = False


def _metadata(take, *, finished=None, metadata_saved=None):
    with take.lock:
        pending = not (take.done.is_set() if finished is None else finished)
        saved = take.metadata_saved if metadata_saved is None else metadata_saved
        complete = (not pending and take.finalized and take.audio_synced
                    and saved and not take.errors and not take.dropped_blocks)
        recording = pending and take.accepting
        status = ("recording" if recording else "stopping" if pending
                  else "error" if take.errors else "complete" if complete
                  else "incomplete")
        return {
            "metadata_version": 1,
            "filename": take.path.name,
            "path": str(take.path),
            "started_at": take.started_at,
            "sample_rate": take.sample_rate,
            "duration_seconds": round(take.frames_written / take.sample_rate, 2),
            "frames": take.frames_written,
            "input_frames": take.input_frames,
            "accepted_frames": take.accepted_frames,
            "dropped_blocks": take.dropped_blocks,
            "dropped_frames": take.dropped_frames,
            "write_errors": take.write_errors,
            "error": take.errors[0] if take.errors else None,
            "errors": list(take.errors),
            "recording": recording,
            "writer_pending": pending,
            "status": status,
            "complete": complete,
            "finalized": take.finalized,
            "audio_synced": take.audio_synced,
            "metadata_saved": saved,
            "state_saved": take.state_saved,
            "stop_reason": take.stop_reason,
            "stop_timeouts": take.stop_timeouts,
        }


def _error(take, message):
    with take.lock:
        if len(take.errors) < 8:
            take.errors.append(str(message))
    logger.warning("Recording %s: %s", take.path.name, message)


def prune_recordings(max_bytes: int = MAX_RECORDINGS_BYTES) -> int:
    """Prune oldest inactive takes at startup; never remove an owned writer."""
    try:
        sizes = {path: (path.stat().st_size, path.stat().st_mtime)
                 for path in RECORDINGS_DIR.glob("*.wav")}
    except OSError:
        return 0
    total = sum(size for size, _mtime in sizes.values())
    removed = 0
    for path in sorted(sizes, key=lambda item: sizes[item][1]):
        if total <= max_bytes:
            break
        if Recorder.delete_take(path.name):
            total -= sizes[path][0]
            removed += 1
            logger.info("Pruned old recording %s", path.name)
    return removed


class Recorder:
    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = int(sample_rate)
        if self.sample_rate <= 0:
            raise ValueError("Recording sample rate must be positive")
        self._take = None
        self._last_meta = None
        self._lock = threading.Lock()
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    def is_recording(self) -> bool:
        take = self._take
        return take is not None and take.accepting

    def current_duration_seconds(self) -> float:
        take = self._take
        if take is not None:
            return take.frames_written / float(self.sample_rate)
        return (self._last_meta or {}).get("duration_seconds", 0.0)

    def current_status(self) -> dict:
        """Current/last take status, including a writer still finalizing."""
        take = self._take
        if take is not None:
            return _metadata(take)
        return dict(self._last_meta or {
            "recording": False, "writer_pending": False,
            "status": "idle", "complete": None,
        })

    # ─────────────────────── control plane ───────────────────────

    def start(self, state_snapshot: Optional[dict] = None) -> dict:
        """Start a unique take, or raise if an older writer is still pending.

        Validate state BEFORE stopping an existing take or opening a WAV.
        Sidecar/open/header/thread-start failures remove only the new take's
        files and propagate to the caller; no partial start is acknowledged.
        """
        snapshot = _json_object(state_snapshot) if state_snapshot is not None else None
        with self._lock:
            if self._take is not None:
                self._stop_locked(STOP_TIMEOUT_SECONDS)
                if self._take is not None:
                    raise RuntimeError("Previous recording is still finalizing; new take not started")

            ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            wav_file = None
            take = None
            wav_path = None
            try:
                # Reservation and registry publication are atomic relative to
                # in-process delete/prune, even before the WAV is opened.
                with _LIBRARY_LOCK:
                    fd, filename = tempfile.mkstemp(prefix=f"{ts}_", suffix=".wav", dir=RECORDINGS_DIR)
                    os.close(fd)
                    wav_path = Path(filename).absolute()
                    _LIVE_TAKES[wav_path] = None  # reserved, not yet initialized
                wav_file = wave.open(str(wav_path), "wb")
                wav_file.setnchannels(2)
                wav_file.setsampwidth(2)
                wav_file.setframerate(self.sample_rate)
                take = _Take(wav_path, wav_file, ts, self.sample_rate)
                if snapshot is not None:
                    atomic_write_json(wav_path.with_suffix(".state.json"), snapshot)
                    take.state_saved = True
                atomic_write_json(wav_path.with_suffix(".meta.json"),
                                  _metadata(take, metadata_saved=True))
                take.metadata_saved = True
                take.thread = threading.Thread(
                    target=self._writer_loop, args=(take,), daemon=True,
                    name="stave-synth-recorder-writer",
                )
                with _LIBRARY_LOCK:
                    _LIVE_TAKES[wav_path] = take
                # Publish to the render producer only after the writer starts.
                take.thread.start()
                self._take = take
            except Exception:
                if wav_file is not None:
                    try:
                        wav_file.close()
                    except Exception as exc:
                        logger.warning("Incomplete recording close failed: %s", exc)
                if wav_path is not None:
                    with _LIBRARY_LOCK:
                        for path in (wav_path, wav_path.with_suffix(".state.json"),
                                     wav_path.with_suffix(".meta.json")):
                            try:
                                path.unlink(missing_ok=True)
                            except OSError as exc:
                                logger.warning("Incomplete recording cleanup failed for %s: %s", path.name, exc)
                        _LIVE_TAKES.pop(wav_path, None)
                raise
            logger.info("Recording started: %s", wav_path.name)
            return _metadata(take)

    def stop(self) -> Optional[dict]:
        """Request drain/close, waiting at most STOP_TIMEOUT_SECONDS.

        A timeout returns status=stopping/complete=False; it never closes or
        discards the still-owned file. Busy control operations raise a timeout
        instead of falsely claiming the recording stopped.
        """
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        if not self._lock.acquire(timeout=STOP_TIMEOUT_SECONDS):
            raise TimeoutError("Recorder control operation busy; stop not confirmed")
        try:
            return self._stop_locked(max(0.0, deadline - time.monotonic()))
        finally:
            self._lock.release()

    def _stop_locked(self, timeout) -> Optional[dict]:
        take = self._take
        if take is None:
            return None
        with take.lock:
            take.accepting = False
            take.stop_reason = take.stop_reason or "manual"
            take.stop_event.set()
        take.thread.join(timeout=timeout)
        if not take.done.is_set():
            with take.lock:
                take.stop_timeouts += 1
        meta = _metadata(take)
        # The writer can finish just after this snapshot. Only detach a take
        # whose snapshot itself is final, never cache a stale "stopping" state.
        if not meta["writer_pending"]:
            self._last_meta = meta
            self._take = None
        return meta

    # ─────────────────────── render-thread feed ───────────────────────

    def feed(self, left_f32: np.ndarray, right_f32: np.ndarray):
        """Copy/enqueue without disk I/O; saturated queues report lost blocks.

        The cap counts input frames (including queue loss), not writer speed.
        The last block is trimmed at the cap. A short state lock makes enqueue
        and stop mutually exclusive, including when stop races the copy.
        """
        take = self._take
        if take is None or not take.accepting:
            return
        # Copy outside the state lock. Recheck ownership/acceptance afterwards:
        # a late producer must not enqueue behind an already-stopped writer.
        try:
            if left_f32.ndim != 1 or right_f32.ndim != 1 or left_f32.size != right_f32.size:
                raise ValueError("Recorder requires equal-length mono channel buffers")
            left, right = left_f32.copy(), right_f32.copy()
        except Exception as exc:
            # No logging/disk I/O on the render path. Stop this take rather
            # than continuing with an unaccounted discontinuity.
            with take.lock:
                if take.accepting:
                    take.errors.append(f"Invalid recording input: {exc}")
                    take.accepting = False
                    take.stop_reason = "input_error"
                    take.stop_event.set()
            return
        with take.lock:
            if not take.accepting:
                return
            remaining = max(0, int(MAX_TAKE_SECONDS * self.sample_rate) - take.input_frames)
            count = min(left.size, remaining)
            if count:
                take.input_frames += count
                try:
                    take.queue.put_nowait((left[:count], right[:count]))
                    take.accepted_frames += count
                except queue.Full:
                    take.dropped_blocks += 1
                    take.dropped_frames += count
            if take.input_frames >= int(MAX_TAKE_SECONDS * self.sample_rate):
                take.accepting = False
                take.stop_reason = "duration_limit"
                take.stop_event.set()

    # ─────────────────────── take-owned writer ───────────────────────

    @staticmethod
    def _writer_loop(take):
        """Only this thread writes/closes this take, never another take's WAV."""
        try:
            while True:
                try:
                    left, right = take.queue.get(timeout=0.05)
                except queue.Empty:
                    # A final producer may enqueue and set stop after get()
                    # timed out. Recheck emptiness under the producer lock.
                    with take.lock:
                        if take.stop_event.is_set() and take.queue.empty():
                            break
                    continue
                try:
                    interleaved = np.empty(left.size * 2, dtype=np.float32)
                    interleaved[0::2] = left
                    interleaved[1::2] = right
                    if not np.isfinite(interleaved).all():
                        raise ValueError("Non-finite samples in recording block")
                    np.clip(interleaved, -1.0, 1.0, out=interleaved)
                    int16 = (interleaved * 32767.0).astype("<i2")
                    take.wav.writeframes(int16.tobytes())
                    with take.lock:
                        take.frames_written += left.size
                except Exception as exc:
                    # A failed block may have partially reached the file. The
                    # count below means frames not successfully committed as
                    # a whole block; never claim they were all written.
                    with take.lock:
                        take.write_errors += 1
                        take.dropped_blocks += 1
                        take.dropped_frames += left.size
                    raise RuntimeError(f"Block write failed: {exc}") from exc
        except Exception as exc:
            _error(take, str(exc))
            with take.lock:
                take.accepting = False
                take.stop_reason = "write_error"
                take.stop_event.set()
            # Discard the rest only after rejecting every producer. Continuing
            # writes after a disk/encoding failure could produce a false take.
            while True:
                try:
                    left, _right = take.queue.get_nowait()
                except queue.Empty:
                    break
                with take.lock:
                    take.dropped_blocks += 1
                    take.dropped_frames += left.size
        finally:
            try:
                take.wav.close()
                with take.lock:
                    take.finalized = True
                # wave.close flushes its header/buffer. Request data durability
                # before publishing a completed lifecycle sidecar.
                with take.path.open("rb") as audio_file:
                    os.fsync(audio_file.fileno())
                with take.lock:
                    take.audio_synced = True
            except Exception as exc:
                _error(take, f"WAV close/sync failed: {exc}")
            with take.lock:
                take.accepting = False
                take.metadata_saved = False
            try:
                atomic_write_json(take.path.with_suffix(".meta.json"),
                                  _metadata(take, finished=True, metadata_saved=True))
                with take.lock:
                    take.metadata_saved = True
            except Exception as exc:
                _error(take, f"Recording metadata save failed: {exc}")
            # No file operation is permitted after releasing library ownership.
            with _LIBRARY_LOCK:
                _LIVE_TAKES.pop(take.path, None)
                take.done.set()

    # ─────────────────────── library queries ───────────────────────

    @staticmethod
    def is_take_active(filename: str) -> bool:
        """True through final close/metadata save, even after stop timed out."""
        path = _take_path(filename)
        with _LIBRARY_LOCK:
            return path in _LIVE_TAKES

    @staticmethod
    @contextmanager
    def claim_take(filename: str):
        """Pin an inactive, usable source during a pad-copy transaction.

        Yields its Path without holding the library lock during disk reads.
        Known incomplete/error/interrupted takes are rejected. Legacy takes
        lacking lifecycle metadata remain eligible for the caller's bounded
        WAV validation; unknown legacy integrity is not asserted complete.
        """
        path = _take_path(filename)
        if path is None:
            raise ValueError("Invalid recording filename")
        with _LIBRARY_LOCK:
            if path in _LIVE_TAKES:
                raise RuntimeError("Stop this recording and wait for finalization before using it")
            if not path.is_file():
                raise FileNotFoundError(f"Recording not found: {filename}")
            _READ_CLAIMS[path] = _READ_CLAIMS.get(path, 0) + 1
        try:
            metadata_path = path.with_suffix(".meta.json")
            if metadata_path.exists():
                try:
                    meta = _load_final_metadata(metadata_path)
                    if not meta["complete"]:
                        raise ValueError(meta.get("error") or f"Take status is {meta['status']}")
                    with wave.open(str(path), "rb") as audio_file:
                        if (audio_file.getnframes() != meta["frames"]
                                or audio_file.getframerate() != meta["sample_rate"]
                                or audio_file.getnchannels() != 2 or audio_file.getsampwidth() != 2
                                or path.stat().st_size < 44 + 4 * meta["frames"]):
                            raise ValueError("WAV does not match completed take metadata")
                except Exception as exc:
                    raise ValueError(f"Recording is not a verified complete take: {exc}") from exc
            yield path
        finally:
            with _LIBRARY_LOCK:
                remaining = _READ_CLAIMS[path] - 1
                if remaining:
                    _READ_CLAIMS[path] = remaining
                else:
                    del _READ_CLAIMS[path]

    @staticmethod
    def list_takes() -> list:
        """Return newest takes, preserving unknown integrity for legacy files."""
        takes = []
        for wav in RECORDINGS_DIR.glob("*.wav"):
            path = wav.absolute()
            try:
                stat = path.stat()
            except OSError:
                continue
            with _LIBRARY_LOCK:
                active = path in _LIVE_TAKES
                live = _LIVE_TAKES.get(path)
            meta_path = path.with_suffix(".meta.json")
            if live is not None:
                meta = _metadata(live)
            elif active:
                meta = {"status": "starting", "recording": False,
                        "writer_pending": True, "complete": False, "finalized": False}
            elif meta_path.exists():
                try:
                    meta = _load_final_metadata(meta_path)
                except Exception as exc:
                    meta = {"status": "incomplete", "complete": False,
                            "finalized": False, "error": str(exc)}
                meta.update(recording=False, writer_pending=False)
            else:
                meta = {"status": "legacy", "complete": None, "finalized": None,
                        "recording": False, "writer_pending": False}
            try:
                with wave.open(str(path), "rb") as audio_file:
                    sr = audio_file.getframerate()
                    duration = audio_file.getnframes() / float(sr) if sr else 0.0
                    if meta.get("complete") is True and (
                            audio_file.getnframes() != meta["frames"]
                            or sr != meta["sample_rate"] or audio_file.getnchannels() != 2
                            or audio_file.getsampwidth() != 2
                            # Our PCM writer emits a standard 44-byte header.
                            # Check for a truncated payload without reading a
                            # potentially hundreds-of-MB take into memory.
                            or stat.st_size < 44 + 4 * meta["frames"]):
                        raise ValueError("WAV does not match completed take metadata")
            except Exception as exc:
                duration = 0.0
                if not active:
                    meta.update(status="error", complete=False,
                                error=f"WAV validation failed: {exc}")
            # Identity/library fields come from the actual path, never JSON.
            meta.update(filename=path.name, url=f"/recordings/{path.name}",
                        path=str(path), size_bytes=stat.st_size,
                        has_state=path.with_suffix(".state.json").exists(), mtime=stat.st_mtime)
            meta.setdefault("duration_seconds", round(duration, 2))
            takes.append(meta)
        return sorted(takes, key=lambda item: item["mtime"], reverse=True)

    @staticmethod
    def delete_take(filename: str) -> bool:
        """Delete an inactive WAV and sidecars; reject active/finalizing takes."""
        path = _take_path(filename)
        if path is None:
            return False
        with _LIBRARY_LOCK:
            if path in _LIVE_TAKES or path in _READ_CLAIMS:
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                return False
            except OSError as exc:
                logger.warning("delete_take: %s: %s", filename, exc)
                return False
            for sidecar in (path.with_suffix(".state.json"), path.with_suffix(".meta.json")):
                try:
                    sidecar.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("delete_take sidecar: %s: %s", sidecar.name, exc)
            return True

    @staticmethod
    def load_state_snapshot(filename: str) -> Optional[dict]:
        """Load finite JSON-object state; reject malformed or unsafe filenames."""
        path = _take_path(filename)
        if path is None:
            return None
        try:
            return _load_object(path.with_suffix(".state.json"))
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.warning("state load failed: %s", exc)
            return None
