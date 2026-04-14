"""MIDI handler: transpose management, note tracking, and MIDI state."""

import logging

logger = logging.getLogger(__name__)


class MidiHandler:
    """Tracks MIDI state: active notes, transpose, and provides utilities."""

    def __init__(self):
        self.transpose = 0  # Semitones (-12 to +12)
        self.active_notes: set[int] = set()  # Currently held notes (post-transpose)

    def set_transpose(self, semitones: int) -> int:
        """Set transpose value, clamped to -12..+12. Returns new value."""
        self.transpose = max(-12, min(12, semitones))
        logger.info("Transpose set to: %+d", self.transpose)
        return self.transpose

    def increment_transpose(self, delta: int) -> int:
        """Change transpose by delta semitones. Returns new value."""
        return self.set_transpose(self.transpose + delta)

    def on_note_on(self, note: int, velocity: float):
        """Track a note-on event."""
        self.active_notes.add(note)

    def on_note_off(self, note: int):
        """Track a note-off event."""
        self.active_notes.discard(note)

    def all_notes_off(self):
        """Clear all active notes."""
        self.active_notes.clear()

    def get_active_count(self) -> int:
        return len(self.active_notes)
