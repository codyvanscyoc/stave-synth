"""Platform-independent audio/MIDI I/O.

The engine talks to an `AudioIO` instance rather than the JACK C bridge
directly. Platform gating lives in `create_audio_io()`:
  - Linux → LinuxJackIO (wraps the existing jack_bridge.so via ctypes)
  - Darwin → MacPortAudioIO (stub — Mac port work in progress)
"""
import sys

from .base import AudioIO


def create_audio_io() -> AudioIO:
    """Return the AudioIO implementation for the current platform."""
    if sys.platform.startswith("linux"):
        from .linux_jack import LinuxJackIO
        return LinuxJackIO()
    if sys.platform == "darwin":
        from .mac_portaudio import MacPortAudioIO
        return MacPortAudioIO()
    raise RuntimeError(f"No AudioIO implementation for platform: {sys.platform}")


__all__ = ["AudioIO", "create_audio_io"]
