"""AudioIO interface — thin abstraction over the platform audio/MIDI transport.

Method names mirror the Linux C bridge 1:1 so the first Linux→interface
cutover is a mechanical rename (`self._bridge.bridge_X` → `self._audio.X`).
Pythonification (numpy arrays in, bytes out) is a follow-up refactor.
"""
from abc import ABC, abstractmethod


class AudioIO(ABC):
    """Audio + MIDI transport surface used by the render + MIDI threads."""

    # ── lifecycle ──
    @abstractmethod
    def start(self) -> int:
        """Start the audio/MIDI subsystem. Returns 0 on success, nonzero err."""

    @abstractmethod
    def stop(self) -> None:
        """Stop and release resources. Idempotent."""

    # ── device info ──
    @abstractmethod
    def get_sample_rate(self) -> int: ...

    @abstractmethod
    def get_buffer_size(self) -> int: ...

    # ── audio out ──
    @abstractmethod
    def write_stereo(self, left_ptr, right_ptr, n: int) -> int:
        """Push `n` frames of stereo audio to the output ring.

        `left_ptr` / `right_ptr` are ctypes float32 pointers on Linux.
        Mac impl will accept numpy arrays and do its own conversion.
        Return value = frames written (may be less than n under backpressure).
        """

    # ── MIDI in ──
    @abstractmethod
    def read_midi(self, buf_ptr) -> int:
        """Drain pending MIDI events into `buf_ptr` (ctypes uint8 array).

        Returns event count. Each event is 4 bytes: [status, d1, d2, reserved].
        """

    @abstractmethod
    def get_midi_event_count(self) -> int: ...

    # ── master gain + BTL flag ──
    @abstractmethod
    def set_master_volume(self, amp: float) -> None: ...

    @abstractmethod
    def set_btl_mode(self, enabled: int) -> None:
        """BTL (bridge-tied-load) adapter hack: invert R for mono-sum output.
        Mac impl should treat this as a no-op — Core Audio devices are all
        proper stereo."""

    @abstractmethod
    def get_btl_mode(self) -> int: ...

    # ── health ──
    @abstractmethod
    def is_shutdown(self) -> int:
        """Return nonzero if the underlying server has died. On Linux this
        catches JACK/PipeWire collapse so systemd can restart us."""

    @abstractmethod
    def get_ring_fill(self) -> int:
        """Frames currently queued in the output ring. Used by the render
        loop to decide whether to sleep vs. push another block."""

    @abstractmethod
    def get_peak_output(self) -> float: ...

    @abstractmethod
    def get_xrun_count(self) -> int: ...

    @abstractmethod
    def get_underrun_count(self) -> int: ...

    @abstractmethod
    def get_callback_count(self) -> int: ...
