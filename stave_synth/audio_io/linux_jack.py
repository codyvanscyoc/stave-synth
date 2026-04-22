"""LinuxJackIO — wraps jack_bridge.so via ctypes.

Exactly mirrors the direct-ctypes surface that `JackEngine` used before
the AudioIO extraction. Behavior is unchanged; this is pure encapsulation.
"""
import ctypes
import logging
import os

from .base import AudioIO

logger = logging.getLogger(__name__)

# jack_bridge.so lives one level up from this subpackage, next to
# jack_bridge.c and the other engine modules.
_BRIDGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "jack_bridge.so",
)


class LinuxJackIO(AudioIO):
    """Ctypes wrapper around jack_bridge.so.

    python-jack-client's CFFI buffer writing is broken on aarch64/Pi 5,
    hence the hand-rolled C bridge. This class holds the CDLL, configures
    argtypes/restypes once, and exposes each bridge function as a method.
    """

    def __init__(self):
        if not os.path.exists(_BRIDGE_PATH):
            raise RuntimeError(
                f"jack_bridge.so not found at {_BRIDGE_PATH}. "
                "Run: gcc -shared -fPIC -O2 -o jack_bridge.so jack_bridge.c "
                "-ljack -lpthread"
            )
        self._lib = ctypes.CDLL(_BRIDGE_PATH)
        self._configure_types()

    def _configure_types(self):
        b = self._lib
        b.bridge_start.restype = ctypes.c_int
        b.bridge_stop.restype = None
        b.bridge_write_audio.argtypes = [ctypes.POINTER(ctypes.c_float), ctypes.c_int]
        b.bridge_write_audio.restype = ctypes.c_int
        b.bridge_write_stereo.argtypes = [
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
        ]
        b.bridge_write_stereo.restype = ctypes.c_int
        b.bridge_set_master_volume.argtypes = [ctypes.c_float]
        b.bridge_set_master_volume.restype = None
        b.bridge_read_midi.argtypes = [ctypes.POINTER(ctypes.c_uint8)]
        b.bridge_read_midi.restype = ctypes.c_int
        b.bridge_get_sample_rate.restype = ctypes.c_int
        b.bridge_get_buffer_size.restype = ctypes.c_int
        b.bridge_get_callback_count.restype = ctypes.c_int
        b.bridge_get_peak_output.restype = ctypes.c_float
        b.bridge_get_xrun_count.restype = ctypes.c_int
        b.bridge_get_underrun_count.restype = ctypes.c_int
        b.bridge_get_midi_event_count.restype = ctypes.c_int
        b.bridge_get_ring_fill.restype = ctypes.c_int
        b.bridge_set_btl_mode.argtypes = [ctypes.c_int]
        b.bridge_set_btl_mode.restype = None
        b.bridge_get_btl_mode.restype = ctypes.c_int
        b.bridge_is_shutdown.restype = ctypes.c_int

    # ── lifecycle ──
    def start(self) -> int:
        return self._lib.bridge_start()

    def stop(self) -> None:
        self._lib.bridge_stop()

    # ── device info ──
    def get_sample_rate(self) -> int:
        return self._lib.bridge_get_sample_rate()

    def get_buffer_size(self) -> int:
        return self._lib.bridge_get_buffer_size()

    # ── audio out ──
    def write_stereo(self, left_ptr, right_ptr, n: int) -> int:
        return self._lib.bridge_write_stereo(left_ptr, right_ptr, n)

    # ── MIDI in ──
    def read_midi(self, buf_ptr) -> int:
        return self._lib.bridge_read_midi(buf_ptr)

    def get_midi_event_count(self) -> int:
        return self._lib.bridge_get_midi_event_count()

    # ── master gain + BTL ──
    def set_master_volume(self, amp: float) -> None:
        self._lib.bridge_set_master_volume(ctypes.c_float(amp))

    def set_btl_mode(self, enabled: int) -> None:
        self._lib.bridge_set_btl_mode(int(enabled))

    def get_btl_mode(self) -> int:
        return self._lib.bridge_get_btl_mode()

    # ── health ──
    def is_shutdown(self) -> int:
        return self._lib.bridge_is_shutdown()

    def get_ring_fill(self) -> int:
        return self._lib.bridge_get_ring_fill()

    def get_peak_output(self) -> float:
        return self._lib.bridge_get_peak_output()

    def get_xrun_count(self) -> int:
        return self._lib.bridge_get_xrun_count()

    def get_underrun_count(self) -> int:
        return self._lib.bridge_get_underrun_count()

    def get_callback_count(self) -> int:
        return self._lib.bridge_get_callback_count()
