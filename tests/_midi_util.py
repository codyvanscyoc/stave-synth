"""Tiny Standard MIDI File builder (Type 0). No external deps."""
import struct
from pathlib import Path


def _vlq(n: int) -> bytes:
    """Variable-length quantity (MIDI SMF delta-time encoding)."""
    if n == 0:
        return b"\x00"
    out = []
    out.append(n & 0x7F)
    n >>= 7
    while n:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(out))


def chord_hold_mid(path: Path, notes=(48, 55, 60, 64, 67), hold_beats=32, ppq=96):
    """Write a .mid that plays `notes` together for hold_beats quarter-notes, then releases.

    At 120 BPM (default), 32 beats = 16 seconds of sustained chord.
    """
    events = bytearray()
    # All note-ons at t=0 (delta=0 for each)
    for i, n in enumerate(notes):
        events += _vlq(0) + bytes([0x90, n, 100])
    # Note-offs: first one at hold_beats*ppq, rest at delta 0
    events += _vlq(hold_beats * ppq) + bytes([0x80, notes[0], 0])
    for n in notes[1:]:
        events += _vlq(0) + bytes([0x80, n, 0])
    # End of track
    events += _vlq(0) + bytes([0xFF, 0x2F, 0x00])

    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, ppq)
    track = b"MTrk" + struct.pack(">I", len(events)) + bytes(events)
    path.write_bytes(header + track)


if __name__ == "__main__":
    import sys
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/chord.mid")
    chord_hold_mid(out)
    print(f"wrote {out}")
