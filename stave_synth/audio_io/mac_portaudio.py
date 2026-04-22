"""MacPortAudioIO — Core Audio via sounddevice/PortAudio.

Stub. Real implementation lands in a later commit of the Mac port work.
The goal: sounddevice.Stream with a Python callback replaces the entire
C bridge + ring buffer on Mac (PortAudio handles the realtime scheduling
itself; no jackd, no a2jmidid).
"""
from .base import AudioIO


class MacPortAudioIO(AudioIO):
    def __init__(self):
        raise NotImplementedError(
            "MacPortAudioIO is not yet implemented. "
            "Mac port is in progress — see audio_io/base.py for the target surface."
        )

    def start(self) -> int: raise NotImplementedError
    def stop(self) -> None: raise NotImplementedError
    def get_sample_rate(self) -> int: raise NotImplementedError
    def get_buffer_size(self) -> int: raise NotImplementedError
    def write_stereo(self, left_ptr, right_ptr, n: int) -> int: raise NotImplementedError
    def read_midi(self, buf_ptr) -> int: raise NotImplementedError
    def get_midi_event_count(self) -> int: raise NotImplementedError
    def set_master_volume(self, amp: float) -> None: raise NotImplementedError
    def set_btl_mode(self, enabled: int) -> None: raise NotImplementedError
    def get_btl_mode(self) -> int: raise NotImplementedError
    def is_shutdown(self) -> int: raise NotImplementedError
    def get_ring_fill(self) -> int: raise NotImplementedError
    def get_peak_output(self) -> float: raise NotImplementedError
    def get_xrun_count(self) -> int: raise NotImplementedError
    def get_underrun_count(self) -> int: raise NotImplementedError
    def get_callback_count(self) -> int: raise NotImplementedError
