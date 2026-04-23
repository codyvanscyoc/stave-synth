"""MacPortAudioIO — Core Audio via sounddevice, Core MIDI via python-rtmidi.

Mirrors the Linux producer/consumer model:
  - Producer: JackEngine's render thread calls write_stereo() with ctypes
    float32 pointers, which we copy into a 24-slot Python ring buffer
    (matching RING_SLOTS in jack_bridge.c).
  - Consumer: sounddevice's RT callback drains the ring into the output
    buffer; underruns emit silence and bump an underrun counter.

We don't drive render from the sounddevice callback on purpose — calling
Python DSP from the RT thread would hold the GIL and defeat the careful
off-RT design that the Pi side relies on. The callback stays lean: one
ring pop + numpy copy + master gain + peak track.

BTL mode is a no-op on Mac (Core Audio devices are all proper stereo).
"""
import logging
import time
from collections import deque

import numpy as np

from .base import AudioIO

logger = logging.getLogger(__name__)

# Match the Linux C bridge so the engine's `fill < 10` threshold and
# steady-state latency calculations carry over unchanged.
_RING_SLOTS = 24
_DEFAULT_SAMPLE_RATE = 48000
# Normal vs. Low Latency block sizes. Linux swaps ring depth at runtime;
# on Mac the equivalent lever is the PortAudio block size — smaller blocks
# mean shorter Core Audio round trip. The ring stays 24 slots either way.
_BLOCK_SIZE_NORMAL = 128
_BLOCK_SIZE_LOW_LATENCY = 48
_DEFAULT_BLOCK_SIZE = _BLOCK_SIZE_NORMAL
_CHANNELS = 2


class MacPortAudioIO(AudioIO):
    def __init__(self):
        # Imports are deferred so this module stays importable on non-Mac
        # platforms (the audio_io/__init__.py factory is the single gate).
        import sounddevice as _sd
        import rtmidi as _rtmidi
        self._sd = _sd
        self._rtmidi = _rtmidi

        # Ring of (n, 2) float32 interleaved blocks. deque append/popleft
        # are GIL-atomic; no extra lock needed for the producer/consumer
        # hand-off. We spin-sleep in write_stereo when the ring is full.
        self._ring: "deque[np.ndarray]" = deque()

        # Leftover frames when a callback consumes only part of a ring slot
        # (happens if Core Audio asks for a different frame count than our
        # ring blocks hold — e.g. 128 on a 256-frame ring).
        self._residual: np.ndarray | None = None

        self._master_volume = 1.0

        # Counters. The sounddevice callback mutates these from an RT thread;
        # we only ever read them from non-RT threads, and a torn read on an
        # int is fine for diagnostics.
        self._callback_count = 0
        self._xrun_count = 0        # PortAudio-reported output underflows
        self._underrun_count = 0    # our ring went empty mid-callback
        self._peak_output = 0.0

        self._started = False
        self._shutdown = False
        self._stream = None
        self._midi_in = None

        # Current + preferred sounddevice block size. set_low_latency_mode()
        # updates both; start() / switch_output_device() read _block_size when
        # opening a new stream so the preference sticks across hot-swaps.
        self._block_size = _DEFAULT_BLOCK_SIZE

        # Preferred output device (name as returned by sounddevice.query_devices).
        # Empty string means "use system default". Honored at start() and by
        # switch_output_device(). Set externally via set_preferred_device()
        # so the selection persists through a hot-swap.
        self._preferred_device = ""
        # Human-readable name of the device actually driving the stream right
        # now — populated after start() / swap, used by list_output_devices()
        # to render the "active" flag in the UI dropdown.
        self._active_device_name = ""

    def set_preferred_device(self, name: str) -> None:
        """Record the user's preferred output device name. Takes effect on
        the next start() call. Empty string = system default."""
        self._preferred_device = name or ""

    # ── lifecycle ──
    def start(self) -> int:
        try:
            self._stream = self._open_stream(self._preferred_device or None)
            self._active_device_name = self._resolve_device_name(self._stream.device)
            logger.info(
                "sounddevice output: sr=%d bs=%d latency=%.2fms device='%s'",
                int(self._stream.samplerate),
                int(self._stream.blocksize or _DEFAULT_BLOCK_SIZE),
                float(self._stream.latency) * 1000.0,
                self._active_device_name or "default",
            )
        except Exception as e:
            logger.error("sounddevice start failed (preferred='%s'): %s",
                         self._preferred_device, e)
            # If the preferred device can't be opened, fall back to default
            # before giving up entirely. Keeps audio working when e.g. a USB
            # interface that was plugged in last session isn't today.
            if self._preferred_device:
                try:
                    self._stream = self._open_stream(None)
                    self._active_device_name = self._resolve_device_name(self._stream.device)
                    logger.warning(
                        "Preferred device '%s' unavailable — fell back to '%s'",
                        self._preferred_device, self._active_device_name or "default",
                    )
                except Exception as e2:
                    logger.error("fallback to default also failed: %s", e2)
                    self._shutdown = True
                    return 1
            else:
                self._shutdown = True
                return 1

        try:
            self._open_midi_input()
        except Exception as e:
            logger.warning("MIDI init failed: %s (continuing without MIDI)", e)
            self._midi_in = None

        self._started = True
        return 0

    def _open_midi_input(self) -> None:
        mi = self._rtmidi.MidiIn()
        ports = mi.get_ports()
        logger.info("Core MIDI ports seen by rtmidi: %s", ports)
        # Ordered keyword match — known controller brands first, then generic
        # "midi interface" names (covers cheap 5-pin DIN → USB cables that
        # show up as H4MIDI, USB MIDI Interface, UM-ONE etc.). Falls back to
        # port 0 if nothing matches and falls through to a virtual port only
        # when rtmidi genuinely sees no hardware.
        preferred_idx = None
        _KEYWORDS = (
            "mpk", "keystation", "launchkey", "piano", "keyboard",
            "yamaha", "roland", "korg", "casio", "nektar",
            "h4midi", "um-one", "uno",  # common generic USB-DIN adapters
            "midi interface", "usb midi",
        )
        for idx, name in enumerate(ports):
            low = name.lower()
            if any(k in low for k in _KEYWORDS):
                preferred_idx = idx
                break
        if preferred_idx is None and ports:
            preferred_idx = 0

        # rtmidi's MidiIn defaults to filtering sysex/timing/active-sense
        # already, but be explicit so hot-plug doesn't surprise us later.
        mi.ignore_types(sysex=True, timing=True, active_sense=True)

        if preferred_idx is not None:
            logger.info("Core MIDI: opening '%s'", ports[preferred_idx])
            mi.open_port(preferred_idx)
        else:
            logger.info("Core MIDI: no hardware ports found; opening virtual port 'stave-synth'")
            mi.open_virtual_port("stave-synth")

        mi.set_callback(self._midi_cb)
        self._midi_in = mi
        # Bounded queue — if we can't drain events fast enough something else
        # is very wrong; dropping is preferable to OOM or blocking Core MIDI.
        import queue
        self._midi_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=1024)

    def stop(self) -> None:
        self._started = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                logger.warning("sounddevice stop/close error: %s", e)
            self._stream = None
        if self._midi_in is not None:
            try:
                self._midi_in.close_port()
                # rtmidi MidiIn needs explicit delete to release the Core MIDI client
                del self._midi_in
            except Exception as e:
                logger.warning("MIDI close error: %s", e)
            self._midi_in = None
        self._ring.clear()
        self._residual = None

    # ── Core Audio device selection (Mac-only extensions, not on AudioIO) ──
    def _open_stream(self, device):
        """Construct + start a sounddevice OutputStream on `device` (name/index
        or None for system default). Raises on failure."""
        stream = self._sd.OutputStream(
            samplerate=_DEFAULT_SAMPLE_RATE,
            blocksize=self._block_size,
            channels=_CHANNELS,
            dtype="float32",
            callback=self._audio_callback,
            latency="low",
            device=device,
        )
        stream.start()
        return stream

    def _resolve_device_name(self, device_id) -> str:
        """Ask PortAudio for the human-readable name of the device currently
        driving the stream. Returns "" if we can't resolve it."""
        try:
            if device_id is None:
                # Default: ask sounddevice what the default output is.
                default_out = self._sd.default.device
                if isinstance(default_out, (list, tuple)):
                    device_id = default_out[1]
                else:
                    device_id = default_out
            info = self._sd.query_devices(device_id, "output")
            return str(info.get("name", ""))
        except Exception as e:
            logger.debug("couldn't resolve device name (%s): %s", device_id, e)
            return ""

    def list_output_devices(self) -> list[dict]:
        """Enumerate Core Audio output devices. Returns one dict per device:
        {"name": str, "active": bool}. `active` marks the device currently
        driving our stream so the UI can highlight it in the dropdown."""
        out = []
        try:
            seen_names = set()
            for info in self._sd.query_devices():
                if int(info.get("max_output_channels", 0)) < 1:
                    continue
                name = str(info.get("name", "")).strip()
                if not name or name in seen_names:
                    # PortAudio occasionally reports duplicate names across
                    # host APIs (CoreAudio vs. AudioUnit). Dedupe by name so
                    # the dropdown shows each device once.
                    continue
                seen_names.add(name)
                out.append({"name": name, "active": name == self._active_device_name})
        except Exception as e:
            logger.warning("list_output_devices failed: %s", e)
        return out

    def switch_output_device(self, name: str) -> tuple[bool, str]:
        """Hot-swap the output stream to `name`. Empty/None = system default.
        Returns (success, error_message). On failure attempts to restore the
        previous stream so audio isn't left completely dead."""
        target = name or None
        old_active = self._active_device_name

        if not self._started or self._stream is None:
            # Not running yet — defer to next start().
            self._preferred_device = name or ""
            return True, ""

        # Pause the producer; write_stereo() exits its backpressure loop
        # when _started flips to False, so the render thread won't spin.
        self._started = False
        old_stream = self._stream
        try:
            old_stream.stop()
            old_stream.close()
        except Exception as e:
            logger.warning("stopping old stream for swap: %s", e)

        # sounddevice's callback can't fire once close() returns, so the ring
        # is now owned by no-one. Clear stale audio so the new device starts
        # on fresh timing.
        self._ring.clear()
        self._residual = None

        try:
            new_stream = self._open_stream(target)
        except Exception as e:
            logger.error("switch to '%s' failed: %s — reverting", name, e)
            # Best-effort: reopen whatever we had before (or default if that
            # name no longer resolves). If this also fails, the synth is
            # effectively muted and is_shutdown() will report it.
            try:
                fallback = self._open_stream(old_active or None)
                self._stream = fallback
                self._active_device_name = self._resolve_device_name(fallback.device)
                self._started = True
            except Exception as e2:
                logger.error("revert to '%s' also failed: %s", old_active, e2)
                self._stream = None
                self._shutdown = True
            return False, str(e)

        self._stream = new_stream
        self._preferred_device = name or ""
        self._active_device_name = self._resolve_device_name(new_stream.device)
        self._started = True
        logger.info(
            "sounddevice output swapped: '%s' → '%s' (latency=%.2fms)",
            old_active or "default", self._active_device_name or "default",
            float(new_stream.latency) * 1000.0,
        )
        return True, ""

    def get_active_device_name(self) -> str:
        """Name of the device currently playing audio; "" if unknown."""
        return self._active_device_name

    # ── device info ──
    def get_sample_rate(self) -> int:
        if self._stream is not None:
            return int(self._stream.samplerate)
        return _DEFAULT_SAMPLE_RATE

    def get_buffer_size(self) -> int:
        # Engine queries this once after start() and uses it as its render
        # block size; we report our current blocksize so ring slots = render
        # blocks 1:1 in the steady state. Value moves under the engine's feet
        # if low-latency mode is toggled — the engine re-queries on restart
        # but mid-run swaps are small enough that the existing residual path
        # in _audio_callback absorbs the mismatch.
        return int(self._block_size)

    # ── audio out ──
    def write_stereo(self, left_ptr, right_ptr, n: int) -> int:
        if not self._started or self._shutdown:
            return 0

        # Wrap the caller's ctypes float32 buffers as numpy views and
        # interleave into a fresh (n, 2) block. Must copy — the engine
        # overwrites its scratch on the next render pass.
        left = np.ctypeslib.as_array(left_ptr, shape=(n,))
        right = np.ctypeslib.as_array(right_ptr, shape=(n,))
        block = np.empty((n, 2), dtype=np.float32)
        block[:, 0] = left
        block[:, 1] = right

        # Backpressure: render thread is not on the RT path on Mac (Core
        # Audio has its own RT callback), so a short sleep-loop is fine.
        while len(self._ring) >= _RING_SLOTS:
            if not self._started or self._shutdown:
                return 0
            time.sleep(0.001)

        self._ring.append(block)
        return n

    def _audio_callback(self, outdata, frames, time_info, status) -> None:
        """Core Audio RT callback — keep this as lean as possible."""
        self._callback_count += 1
        if status and status.output_underflow:
            self._xrun_count += 1

        written = 0

        # Drain any residual carried over from the previous callback first.
        if self._residual is not None:
            take = min(frames, self._residual.shape[0])
            outdata[:take] = self._residual[:take]
            if take < self._residual.shape[0]:
                self._residual = self._residual[take:]
            else:
                self._residual = None
            written = take

        while written < frames:
            try:
                block = self._ring.popleft()
            except IndexError:
                # Ring empty — emit silence for the rest of the callback.
                self._underrun_count += 1
                outdata[written:] = 0.0
                break

            take = min(frames - written, block.shape[0])
            outdata[written:written + take] = block[:take]
            if take < block.shape[0]:
                # Partial consume — save remainder for next callback.
                self._residual = block[take:]
            written += take

        if self._master_volume != 1.0:
            outdata *= self._master_volume

        # Track peak for metering. Cheap float/abs over a small block.
        if frames:
            p = float(np.abs(outdata).max())
            if p > self._peak_output:
                self._peak_output = p

    # ── MIDI in ──
    def _midi_cb(self, event, _data) -> None:
        """Core MIDI input thread → bounded queue. Called from rtmidi's
        internal thread, NOT the audio callback thread."""
        message, _timestamp = event
        try:
            self._midi_queue.put_nowait(bytes(message))
        except Exception:
            # Queue full or closed — drop. The audio path never depends on
            # MIDI delivery, so silent drop is safer than blocking rtmidi.
            pass

    def read_midi(self, buf_ptr) -> int:
        q = getattr(self, "_midi_queue", None)
        if q is None:
            return 0
        try:
            msg = q.get_nowait()
        except Exception:
            return 0
        # Channel-voice messages are 2-3 bytes; we promise the Linux surface
        # of [status, d1, d2, reserved] in a 4-byte buffer. Zero-pad the rest.
        n = min(len(msg), 3)
        for i in range(n):
            buf_ptr[i] = msg[i]
        # Caller keys on `n >= 3`; pad so out-of-range reads are well-defined.
        for i in range(n, 4):
            buf_ptr[i] = 0
        return n

    def get_midi_event_count(self) -> int:
        q = getattr(self, "_midi_queue", None)
        return q.qsize() if q is not None else 0

    # ── master gain + BTL ──
    def set_master_volume(self, amp: float) -> None:
        self._master_volume = float(amp)

    def set_btl_mode(self, enabled: int) -> None:
        if int(enabled):
            logger.warning(
                "BTL mode requested on Mac — no-op. Core Audio outputs are "
                "proper stereo; BTL_MODE is a Linux-only bridge-tied-load hack."
            )

    def get_btl_mode(self) -> int:
        return 0

    # ── health ──
    def is_shutdown(self) -> int:
        if self._shutdown:
            return 1
        if self._started and self._stream is not None and not self._stream.active:
            return 1
        return 0

    def get_ring_fill(self) -> int:
        # Slot count, matching jack_bridge.c's ring_readable_relaxed().
        return len(self._ring)

    def get_peak_output(self) -> float:
        p = self._peak_output
        self._peak_output = 0.0
        return p

    def get_xrun_count(self) -> int:
        return self._xrun_count

    def get_underrun_count(self) -> int:
        return self._underrun_count

    def get_callback_count(self) -> int:
        return self._callback_count

    # ── latency mode ──
    def set_low_latency_mode(self, enabled: bool) -> int:
        """Reopen the sounddevice stream with a smaller block size. On Mac
        the Core Audio round trip dominates latency, so shrinking the PortAudio
        block is the equivalent of Linux's ring-depth swap. If called before
        start() we just remember the choice; start() opens at the right size.

        Ring depth on Mac stays 24 slots, so the render threshold doesn't move
        — we return 0 to tell the engine to keep its current value."""
        target_bs = _BLOCK_SIZE_LOW_LATENCY if enabled else _BLOCK_SIZE_NORMAL
        if target_bs == self._block_size and self._started:
            # Already there. No-op keeps the toggle idempotent on startup when
            # main.py applies saved state and then the user flips the UI.
            return 0

        self._block_size = target_bs

        if not self._started or self._stream is None:
            # Not running yet — start() will pick up _block_size.
            return 0

        # Mute the producer the same way switch_output_device does, so the
        # render thread exits its backpressure spin while we're tearing down.
        self._started = False
        old_stream = self._stream
        try:
            old_stream.stop()
            old_stream.close()
        except Exception as e:
            logger.warning("stopping stream for latency swap: %s", e)

        # ~20 ms quiet window mirrors the Linux bridge transition mute. Gives
        # Core Audio a beat to release the old unit before we allocate a new
        # one at a different block size.
        time.sleep(0.020)

        self._ring.clear()
        self._residual = None

        try:
            new_stream = self._open_stream(self._preferred_device or None)
        except Exception as e:
            logger.error(
                "low-latency swap to blocksize=%d failed: %s — reverting",
                target_bs, e,
            )
            # Fall back to the previous block size so audio comes back.
            self._block_size = _BLOCK_SIZE_NORMAL if enabled else _BLOCK_SIZE_LOW_LATENCY
            try:
                new_stream = self._open_stream(self._preferred_device or None)
            except Exception as e2:
                logger.error("revert after failed latency swap also failed: %s", e2)
                self._stream = None
                self._shutdown = True
                return 0

        self._stream = new_stream
        self._active_device_name = self._resolve_device_name(new_stream.device)
        self._started = True
        logger.info(
            "Low Latency Mode %s — blocksize=%d latency=%.2fms",
            "ON" if enabled else "OFF",
            int(new_stream.blocksize or self._block_size),
            float(new_stream.latency) * 1000.0,
        )
        return 0
