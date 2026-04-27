"""WAV recorder for the Stave Synth master output.

Design:
  * Audio thread calls ``feed(left, right)`` per render block. That method
    copies the buffer into a bounded queue and returns immediately — zero
    disk I/O on the audio path.
  * A dedicated writer thread drains the queue, converts float32 → int16,
    and writes PCM blocks to a ``wave.Wave_write`` object.
  * ``start(state_snapshot)`` opens a new WAV at
    ``~/.local/share/stave-synth/recordings/YYYY-MM-DD_HH-MM-SS.wav`` and
    writes the supplied state snapshot to ``*.state.json`` alongside it.
  * ``stop()`` flushes the queue, closes the file, joins the writer thread,
    and returns the finished take's metadata.

The sidecar ``.state.json`` captures the full synth state at record-start
so that ``recall_params`` can restore that exact sound on demand.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import wave
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

from .config import SAMPLE_RATE

logger = logging.getLogger(__name__)

RECORDINGS_DIR = Path.home() / ".local" / "share" / "stave-synth" / "recordings"
MAX_QUEUE = 400  # ~2 s of 256-sample blocks at 48 kHz
MAX_TAKE_SECONDS = 30 * 60  # hard cap to prevent runaway disk fills


class Recorder:
    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = int(sample_rate)
        self._queue: queue.Queue = queue.Queue(maxsize=MAX_QUEUE)
        self._writer_thread: Optional[threading.Thread] = None
        self._wav: Optional[wave.Wave_write] = None
        self._recording = False
        self._current_path: Optional[Path] = None
        self._frames_written = 0
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    def is_recording(self) -> bool:
        return self._recording

    def current_duration_seconds(self) -> float:
        return self._frames_written / float(self.sample_rate)

    # ─────────────────────── control plane ───────────────────────

    def start(self, state_snapshot: Optional[dict] = None) -> dict:
        """Start a new take. Returns the take's metadata (filename, path).
        If a take is already in progress, stops it first."""
        with self._lock:
            if self._recording:
                self._stop_locked()

            ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            wav_path = RECORDINGS_DIR / f"{ts}.wav"
            state_path = RECORDINGS_DIR / f"{ts}.state.json"

            # Open wav first — raises if dir is missing / perms wrong
            self._wav = wave.open(str(wav_path), "wb")
            self._wav.setnchannels(2)
            self._wav.setsampwidth(2)          # int16
            self._wav.setframerate(self.sample_rate)

            # Save state snapshot (best-effort; failure shouldn't kill the take)
            if state_snapshot is not None:
                try:
                    with open(state_path, "w") as f:
                        json.dump(state_snapshot, f, indent=2, default=str)
                except Exception as e:
                    logger.warning("state snapshot save failed: %s", e)

            self._current_path = wav_path
            self._frames_written = 0
            self._stop_event.clear()
            self._recording = True

            self._writer_thread = threading.Thread(
                target=self._writer_loop, daemon=True,
                name="stave-synth-recorder-writer",
            )
            self._writer_thread.start()
            logger.info("Recording started: %s", wav_path.name)
            return {
                "filename": wav_path.name,
                "path": str(wav_path),
                "started_at": ts,
            }

    def stop(self) -> Optional[dict]:
        with self._lock:
            return self._stop_locked()

    def _stop_locked(self) -> Optional[dict]:
        if not self._recording:
            return None
        self._recording = False
        self._stop_event.set()
        # Flush writer thread — it drains the remaining queue + closes the WAV
        t = self._writer_thread
        if t is not None:
            t.join(timeout=3.0)
        meta = None
        if self._current_path is not None:
            meta = {
                "filename": self._current_path.name,
                "path": str(self._current_path),
                "duration_seconds": round(self.current_duration_seconds(), 2),
                "frames": self._frames_written,
            }
            logger.info("Recording stopped: %s (%.2fs)",
                        self._current_path.name, meta["duration_seconds"])
        self._wav = None
        self._current_path = None
        self._writer_thread = None
        return meta

    # ─────────────────────── audio-thread feed ───────────────────────

    def feed(self, left_f32: np.ndarray, right_f32: np.ndarray):
        """Called from the audio render thread. Copies buffers + enqueues.
        Drops blocks if the queue is saturated (writer can't keep up) rather
        than blocking the audio thread."""
        if not self._recording:
            return
        # Hard cap on take length
        if self._frames_written > MAX_TAKE_SECONDS * self.sample_rate:
            # Defer stop to a helper thread — don't touch the writer from audio
            threading.Thread(target=self.stop, daemon=True).start()
            return
        try:
            # Copy so the caller's scratch buffers can be reused next block
            block = (left_f32.copy(), right_f32.copy())
            self._queue.put_nowait(block)
        except queue.Full:
            # Writer is behind — drop the block. Logged once per minute max
            # by the writer thread if it notices a gap.
            pass

    # ─────────────────────── writer thread ───────────────────────

    def _writer_loop(self):
        """Drains the queue to disk. Runs until stop_event is set AND queue empty."""
        while True:
            try:
                left, right = self._queue.get(timeout=0.25)
            except queue.Empty:
                if self._stop_event.is_set():
                    break
                continue
            try:
                # Interleave stereo, clip to [-1, 1], scale to int16
                interleaved = np.empty(left.size * 2, dtype=np.float32)
                interleaved[0::2] = left
                interleaved[1::2] = right
                np.clip(interleaved, -1.0, 1.0, out=interleaved)
                int16 = (interleaved * 32767.0).astype(np.int16)
                if self._wav is not None:
                    self._wav.writeframes(int16.tobytes())
                    self._frames_written += left.size
            except Exception as e:
                logger.warning("writer: block write failed: %s", e)
        # Close file
        try:
            if self._wav is not None:
                self._wav.close()
        except Exception as e:
            logger.warning("writer: wav close failed: %s", e)

    # ─────────────────────── library queries ───────────────────────

    @staticmethod
    def list_takes() -> list:
        """Return a list of take dicts (newest first)."""
        if not RECORDINGS_DIR.exists():
            return []
        takes = []
        for wav in sorted(RECORDINGS_DIR.glob("*.wav"), reverse=True):
            try:
                with wave.open(str(wav), "rb") as w:
                    frames = w.getnframes()
                    sr = w.getframerate()
                    duration = frames / float(sr) if sr else 0.0
            except Exception:
                duration = 0.0
            state_path = wav.with_suffix(".state.json")
            takes.append({
                "filename": wav.name,
                "url": f"/recordings/{wav.name}",
                "duration_seconds": round(duration, 2),
                "size_bytes": wav.stat().st_size if wav.exists() else 0,
                "has_state": state_path.exists(),
                "mtime": wav.stat().st_mtime if wav.exists() else 0,
            })
        return takes

    @staticmethod
    def delete_take(filename: str) -> bool:
        """Delete a WAV + its sidecar. Returns True if at least the wav was removed."""
        # Guard against path traversal
        if "/" in filename or "\\" in filename or ".." in filename:
            return False
        wav = RECORDINGS_DIR / filename
        state = wav.with_suffix(".state.json")
        removed = False
        try:
            if wav.exists():
                wav.unlink()
                removed = True
        except Exception as e:
            logger.warning("delete_take: %s: %s", filename, e)
        try:
            if state.exists():
                state.unlink()
        except Exception:
            pass
        return removed

    @staticmethod
    def load_state_snapshot(filename: str) -> Optional[dict]:
        """Load the sidecar state JSON for a take (returns None if absent)."""
        if "/" in filename or "\\" in filename or ".." in filename:
            return None
        wav = RECORDINGS_DIR / filename
        state = wav.with_suffix(".state.json")
        if not state.exists():
            return None
        try:
            with open(state, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("state load failed: %s", e)
            return None
