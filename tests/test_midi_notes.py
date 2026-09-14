"""Offline regression tests for the production MIDI dispatch methods.

AST extraction executes the actual method bodies without importing JACK,
NumPy, Faust, application configuration, or any service/device integration.
Run: python3 -m unittest discover -s tests -p 'test_midi_notes.py' -v
"""

import ast
import ctypes
import logging
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace


def _production_methods():
    path = Path(__file__).resolve().parents[1] / "stave_synth" / "jack_engine.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    engine = next(node for node in tree.body
                  if isinstance(node, ast.ClassDef) and node.name == "JackEngine")
    methods = [node for node in engine.body
               if isinstance(node, ast.FunctionDef)
               and node.name in ("_midi_loop_body", "panic")]
    assert len(methods) == 2
    namespace = {
        "ctypes": ctypes,
        "time": SimpleNamespace(sleep=lambda _: None),
        "_MIDI_POLL_S": 0.002,
        "SAMPLE_RATE": 48000,
        "SynthEngine": SimpleNamespace(split_weight=lambda *args: 1.0),
        "logger": logging.getLogger(__name__),
    }
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


_METHODS = _production_methods()


class _Bridge:
    def __init__(self, engine):
        self.engine = engine
        self.events = deque()
        self.clear_count = 0

    def bridge_read_midi(self, buffer):
        while self.events:
            event = self.events.popleft()
            if callable(event):
                event()
                continue
            for index, byte in enumerate(event):
                buffer[index] = byte
            return len(event)
        self.engine.running = False
        return 0

    def bridge_clear_ring(self):
        self.clear_count += 1
        return 0

    def bridge_get_midi_drop_count(self):
        return 0

    def bridge_get_midi_recovery_count(self):
        return 0


class _Synth:
    def __init__(self):
        self.events = []
        self.weights = (1.0, 1.0, 1.0)

    def note_on(self, note, velocity, *weights):
        self.events.append(("note_on", note, velocity))

    def note_off(self, note):
        self.events.append(("note_off", note, 0))

    def all_notes_off(self):
        self.events.append(("all_notes_off", 0, 0))

    def panic(self):
        self.events.append(("panic", 0, 0))

    def compute_split_weights(self, note):
        return self.weights


class _Engine:
    _midi_loop_body = _METHODS["_midi_loop_body"]
    panic = _METHODS["panic"]

    def __init__(self):
        self.running = False
        self._bridge = _Bridge(self)
        self._midi_events_seen = self._midi_notes_triggered = 0
        self.midi_clock_enabled = False
        self.min_velocity = 1
        self.transpose = self.piano_octave = 0
        self.split_enabled = False
        self.instrument_split_low = 0
        self.instrument_split_high = 127
        self.instrument_split_xfade = 0
        self.synth = _Synth()
        self._note_map = {}
        self._physically_held = set()
        self._sustained_notes = set()
        self._sostenuto_held = set()
        self._pad_notes_active = set()
        self._piano_notes_active = set()
        self._sustain_on = self._sostenuto_on = False
        self.bus_comp_retrigger = False
        self.piano_events = []
        self.ui_events = []
        self.piano_callback = lambda *event: self.piano_events.append(event)
        self.midi_callback = lambda *event: self.ui_events.append(event)
        self.limiter_resets = 0
        self._limiter = SimpleNamespace(reset=self._reset_limiter)

    def _reset_limiter(self):
        self.limiter_resets += 1

    def feed(self, *events):
        self._bridge.events.extend(events)
        self.running = True
        self._midi_loop_body()


def on(note=60, velocity=100):
    return (0x90, note, velocity)


def off(note=60):
    return (0x80, note, 0)


def pedal(number, high):
    return (0xB0, number, 127 if high else 0)


def note_offs(events):
    return [event[1] for event in events if event[0] == "note_off"]


class MidiNoteOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.engine = _Engine()

    def assertReleased(self):
        engine = self.engine
        self.assertEqual(engine._note_map, {})
        self.assertEqual(engine._pad_notes_active, set())
        self.assertEqual(engine._piano_notes_active, set())

    def test_piano_octave_only_sustained_retrigger_retires_old_pitch(self):
        engine = self.engine
        engine.feed(pedal(64, True), on(), off())
        engine.piano_octave = 1
        engine.feed(on())
        self.assertEqual(note_offs(engine.piano_events), [60])
        self.assertEqual(note_offs(engine.synth.events), [])
        self.assertEqual(engine._note_map, {60: (60, 72)})
        self.assertEqual(engine._pad_notes_active, {60})
        self.assertEqual(engine._piano_notes_active, {72})
        self.assertEqual(engine._sustained_notes, set())
        self.assertEqual(engine.piano_events[-2][0:2], ("note_off", 60))
        self.assertEqual(engine.piano_events[-1][0:2], ("note_on", 72))
        engine.feed(off(), pedal(64, False))
        self.assertEqual(note_offs(engine.piano_events), [60, 72])
        self.assertEqual(note_offs(engine.synth.events), [60])
        self.assertReleased()

    def test_transpose_retrigger_retires_both_old_destinations(self):
        engine = self.engine
        engine.feed(on())
        engine.transpose = 2
        engine.feed(on())
        self.assertEqual(note_offs(engine.synth.events), [60])
        self.assertEqual(note_offs(engine.piano_events), [60])
        self.assertEqual(engine._pad_notes_active, {62})
        self.assertEqual(engine._piano_notes_active, {62})
        engine.feed(off())
        self.assertEqual(note_offs(engine.synth.events), [60, 62])
        self.assertReleased()

    def test_same_pitch_retrigger_preserves_existing_no_off_policy(self):
        engine = self.engine
        engine.feed(on(), on(velocity=80))
        self.assertEqual(note_offs(engine.synth.events), [])
        self.assertEqual(note_offs(engine.piano_events), [])
        engine.feed(off())
        self.assertEqual(note_offs(engine.piano_events), [60])
        self.assertReleased()

    def test_sustain_up_does_not_release_sostenuto_note(self):
        engine = self.engine
        engine.feed(on(), pedal(66, True), pedal(64, True), off(), pedal(64, False))
        self.assertEqual(note_offs(engine.piano_events), [])
        self.assertEqual(engine._note_map, {60: (60, 60)})
        self.assertEqual(engine._sustained_notes, set())
        engine.feed(pedal(66, False))
        self.assertEqual(note_offs(engine.piano_events), [60])
        self.assertReleased()

    def test_sostenuto_up_does_not_release_sustain_note(self):
        engine = self.engine
        engine.feed(on(), pedal(66, True), pedal(64, True), off(), pedal(66, False))
        self.assertEqual(note_offs(engine.piano_events), [])
        self.assertEqual(engine._sustained_notes, {60})
        self.assertEqual(engine._sostenuto_held, set())
        engine.feed(pedal(64, False))
        self.assertEqual(note_offs(engine.piano_events), [60])
        self.assertReleased()

    def test_sustain_takes_over_already_released_sostenuto_note(self):
        engine = self.engine
        engine.feed(on(), pedal(66, True), off())
        self.assertEqual(engine._sustained_notes, set())
        engine.feed(pedal(64, True), pedal(66, False))
        self.assertEqual(note_offs(engine.piano_events), [])
        self.assertEqual(engine._sustained_notes, {60})
        engine.feed(pedal(64, False))
        self.assertEqual(note_offs(engine.piano_events), [60])
        self.assertReleased()

    def test_repeated_high_sostenuto_does_not_capture_later_notes(self):
        engine = self.engine
        engine.feed(on(), pedal(66, True), off(), on(62), pedal(66, True), off(62))
        self.assertEqual(engine._sostenuto_held, {60})
        self.assertEqual(note_offs(engine.piano_events), [62])
        self.assertEqual(engine._note_map, {60: (60, 60)})
        engine.feed(pedal(66, False))
        self.assertEqual(note_offs(engine.piano_events), [62, 60])
        self.assertReleased()

    def test_physical_key_outlives_both_pedals_in_either_release_order(self):
        for first, second in ((64, 66), (66, 64)):
            with self.subTest(first=first):
                self.engine = engine = _Engine()
                engine.feed(on(), pedal(66, True), pedal(64, True),
                            pedal(first, False), pedal(second, False))
                self.assertEqual(note_offs(engine.piano_events), [])
                self.assertEqual(engine._physically_held, {60})
                engine.feed(off())
                self.assertEqual(note_offs(engine.piano_events), [60])
                self.assertReleased()

    def test_panic_resets_both_pedals_and_next_note_releases_normally(self):
        engine = self.engine
        engine.feed(on(), pedal(66, True), pedal(64, True), off())
        engine.panic()
        self.assertFalse(engine._sustain_on)
        self.assertFalse(engine._sostenuto_on)
        self.assertEqual(engine._physically_held, set())
        self.assertEqual(engine._sustained_notes, set())
        self.assertEqual(engine._sostenuto_held, set())
        self.assertEqual(engine.limiter_resets, 1)
        self.assertEqual(engine._bridge.clear_count, 1)
        self.assertReleased()
        engine.feed(on(), off())
        self.assertEqual(note_offs(engine.piano_events), [60])
        self.assertReleased()

    def test_overflow_recovery_cc123_clears_both_pedals_and_notes(self):
        engine = self.engine
        engine.feed(on(), pedal(66, True), pedal(64, True), off())
        engine.feed((0xB0, 123, 0))
        self.assertFalse(engine._sustain_on)
        self.assertFalse(engine._sostenuto_on)
        self.assertEqual(engine._physically_held, set())
        self.assertEqual(engine._sustained_notes, set())
        self.assertEqual(engine._sostenuto_held, set())
        self.assertEqual(engine._note_map, {})
        self.assertIn(("all_notes_off", 0, 0), engine.synth.events)
        self.assertIn(("all_notes_off", 0, 0), engine.piano_events)
        self.assertIn(("all_notes_off", 0, 0), engine.ui_events)

    def test_repeated_pedal_up_does_not_duplicate_note_offs(self):
        engine = self.engine
        engine.feed(on(), pedal(66, True), pedal(64, True), off(),
                    pedal(66, False), pedal(66, False),
                    pedal(64, False), pedal(64, False))
        self.assertEqual(note_offs(engine.piano_events), [60])
        self.assertEqual(note_offs(engine.synth.events), [60])
        self.assertReleased()

    def test_velocity_zero_note_on_still_releases(self):
        engine = self.engine
        engine.feed(on(), on(velocity=0))
        self.assertEqual(note_offs(engine.piano_events), [60])
        self.assertReleased()

    def test_minimum_velocity_policy_unchanged(self):
        engine = self.engine
        engine.min_velocity = 20
        engine.feed(on(velocity=10), off())
        self.assertEqual(engine.piano_events, [])
        self.assertEqual(engine.synth.events, [])
        self.assertEqual(engine._midi_notes_triggered, 0)
        self.assertReleased()

    def test_silent_pad_split_does_not_add_phantom_active_pitch(self):
        engine = self.engine
        engine.split_enabled = True
        engine.synth.weights = (0.0, 0.0, 0.0)
        engine.feed(on())
        self.assertEqual(engine.synth.events, [])
        self.assertEqual(engine._pad_notes_active, set())
        self.assertEqual(engine._piano_notes_active, {60})


if __name__ == "__main__":
    unittest.main()
