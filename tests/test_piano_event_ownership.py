"""Offline AST-extracted ownership regressions; no native module imports."""

import ast
import logging
import math
import threading
import unittest
from collections import deque
from pathlib import Path
from types import MethodType

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "stave_synth/fluidsynth_player.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
BODY = []
for node in TREE.body:
    if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and target.id in {"_MIDI_EVENT_QUEUE_CAPACITY",
                              "_MIDI_EVENT_RENDER_BATCH_MAX"}
            for target in node.targets):
        BODY.append(node)
    elif isinstance(node, ast.ClassDef) and node.name == "FluidSynthPlayer":
        BODY.append(node)
ENV = {
    "logging": logging,
    "logger": logging.getLogger(__name__),
    "math": math,
    "threading": threading,
    "deque": deque,
    "np": np,
    "Path": Path,
    "SAMPLE_RATE": 48000,
    "LOW_RAM_MODE": False,
    "USE_FAUST_PIANO_CHAIN": False,
    "SOUNDFONT_PRESETS": {
        "Salamander": {"file": "Salamander", "program": 0},
        "Fluid": {"file": "FluidR3_GM", "program": 0,
                  "tremolo_hz": 0.0, "tremolo_depth": 0.0,
                  "velocity_curve": 1.0},
    },
    "PIANO_VOICINGS": {},
    "SOUNDFONT_DIR": ROOT / "soundfonts",
}
exec(compile(ast.Module(body=BODY, type_ignores=[]), str(SOURCE), "exec"), ENV)
FluidSynthPlayer = ENV["FluidSynthPlayer"]
MIDI_EVENT_QUEUE_CAPACITY = ENV["_MIDI_EVENT_QUEUE_CAPACITY"]
MIDI_EVENT_RENDER_BATCH_MAX = ENV["_MIDI_EVENT_RENDER_BATCH_MAX"]


class FakeFluidSynth:
    def __init__(self):
        self.calls = []

    def noteon(self, channel, note, velocity):
        self.calls.append(("noteon", threading.get_ident(), channel, note, velocity))

    def noteoff(self, channel, note):
        self.calls.append(("noteoff", threading.get_ident(), channel, note))

    def pitch_bend(self, channel, value):
        self.calls.append(("pitch_bend", threading.get_ident(), channel, value))

    def cc(self, channel, controller, value):
        self.calls.append(("cc", threading.get_ident(), channel, controller, value))

    def get_samples(self, count):
        self.calls.append(("get_samples", threading.get_ident(), count))
        raw = np.empty(count * 2, dtype=np.int16)
        raw[0::2] = 1200
        raw[1::2] = -900
        return raw

    def program_select(self, channel, sfid, bank, program):
        self.calls.append(("program_select", threading.get_ident(), channel,
                           sfid, bank, program))
        return 0


def make_player(active_notes=1):
    player = FluidSynthPlayer.__new__(FluidSynthPlayer)
    player.sample_rate = 48000
    player.fs = FakeFluidSynth()
    player.enabled = True
    player._closing = False
    player._lock = threading.RLock()
    player._midi_event_lock = threading.Lock()
    player._midi_events = deque()
    player._midi_recovery_pending = False
    player._midi_recovery_failed_latched = False
    player._midi_events_enqueued = 0
    player._midi_events_applied = 0
    player._midi_event_overflows = 0
    player._midi_event_recoveries = 0
    player._midi_events_discarded = 0
    player._midi_queue_lock_deferrals = 0
    player._midi_status_lock_deferrals = 0
    player._native_render_lock_misses = 0
    player._midi_native_errors = 0
    player._midi_recovery_failures = 0
    player._midi_noteoff_unmatched = 0
    player._note_on_count = 0
    player._render_count = 0
    player._active_notes = active_notes
    player._silent_blocks = 0
    player._vel_tracker = 0.7
    player.volume = 1.0
    player._volume_cur = 1.0
    player.velocity_curve = 1.0
    player._faust_chain = object()
    player._render_chain_faust = MethodType(
        lambda self, raw, count: (
            raw[0::2].astype(np.float64) / 32768.0,
            raw[1::2].astype(np.float64) / 32768.0,
        ),
        player,
    )
    player.vel_bright_enabled = False
    player.vel_bright_amount = 0.0
    player.tremolo_depth = 0.0
    player.tremolo_hz = 0.0
    player._piano_room = None
    return player


class PianoEventOwnershipTests(unittest.TestCase):
    def test_chord_native_calls_are_render_owned_and_ordered_before_samples(self):
        player = make_player(active_notes=0)
        producer_ident = threading.get_ident()
        chord = (48, 55, 60, 62, 64, 67)
        for note in chord:
            player.note_on(note, 72 / 127.0)

        self.assertEqual(player.fs.calls, [])
        rendered = []

        def render():
            rendered.append(player.render_block(512))

        worker = threading.Thread(target=render)
        worker.start()
        worker.join(timeout=2.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual([call[0] for call in player.fs.calls],
                         ["noteon"] * len(chord) + ["get_samples"])
        native_thread_ids = {call[1] for call in player.fs.calls}
        self.assertEqual(len(native_thread_ids), 1)
        self.assertNotIn(producer_ident, native_thread_ids)
        self.assertGreater(float(np.abs(rendered[0]).max()), 0.0)
        self.assertEqual(player.midi_render_status()["applied"], len(chord))

    def test_stalled_event_lock_defers_midi_but_never_substitutes_silence(self):
        player = make_player()
        player.note_on(60, 0.5)
        self.assertTrue(player._midi_event_lock.acquire(blocking=False))
        try:
            rendered = player.render_block(512)
        finally:
            player._midi_event_lock.release()
        self.assertGreater(float(np.abs(rendered).max()), 0.0)
        self.assertEqual([call[0] for call in player.fs.calls], ["get_samples"])
        status = player.midi_render_status()
        self.assertEqual(status["queued"], 1)
        self.assertEqual(status["queue_lock_deferrals"], 1)

        player.render_block(512)
        self.assertEqual([call[0] for call in player.fs.calls],
                         ["get_samples", "noteon", "get_samples"])

    def test_render_work_is_bounded_and_remainder_keeps_fifo_order(self):
        player = make_player(active_notes=0)
        total = MIDI_EVENT_RENDER_BATCH_MAX + 3
        for note in range(total):
            player._queue_midi_event("note_on", note, 64, 0.5)

        player.render_block(512)
        first_notes = [call[3] for call in player.fs.calls
                       if call[0] == "noteon"]
        self.assertEqual(first_notes,
                         list(range(MIDI_EVENT_RENDER_BATCH_MAX)))
        self.assertEqual(player.midi_render_status()["queued"], 3)

        player.render_block(512)
        all_notes = [call[3] for call in player.fs.calls if call[0] == "noteon"]
        self.assertEqual(all_notes, list(range(total)))

    def test_overflow_discards_ambiguous_batch_and_releases_everything(self):
        player = make_player()
        capacity = MIDI_EVENT_QUEUE_CAPACITY
        for index in range(capacity):
            self.assertTrue(player._queue_midi_event(
                "note_on", index % 128, 64, 0.5
            ))
        self.assertFalse(player._queue_midi_event("note_off", 60, 0, 0.0))
        self.assertFalse(player._queue_midi_event("note_on", 61, 64, 0.5))

        player.render_block(512)
        kinds = [call[0] for call in player.fs.calls]
        self.assertEqual(kinds[:3], ["cc", "cc", "cc"])
        self.assertEqual(kinds.count("noteoff"), 0)
        self.assertNotIn("noteon", kinds)
        self.assertEqual(kinds[-2:], ["pitch_bend", "get_samples"])
        status = player.midi_render_status()
        self.assertEqual(status["overflows"], 1)
        self.assertEqual(status["recoveries"], 1)
        self.assertEqual(status["discarded"], capacity + 2)
        self.assertEqual(status["pending"], 0)

    def test_synchronous_all_notes_off_cancels_stale_queued_batch(self):
        player = make_player()
        player._piano_room = None
        player._comp_envelope = 0.5
        player.note_on(60, 0.8)
        player.note_off(60)
        player.all_notes_off()
        player.render_block(512)
        kinds = [call[0] for call in player.fs.calls]
        self.assertNotIn("noteon", kinds)
        self.assertEqual(kinds.count("noteoff"), 0)
        self.assertEqual([call[2:] for call in player.fs.calls if call[0] == "cc"],
                         [(0, 64, 0), (0, 66, 0), (0, 123, 0)])
        self.assertEqual(player.midi_render_status()["discarded"], 2)

    def test_native_release_is_four_ordered_calls_without_hard_sound_off(self):
        player = make_player()
        self.assertTrue(player._release_all_native_locked())
        calls = [(c[0], *c[2:]) for c in player.fs.calls]
        self.assertEqual(calls, [('cc', 0, 64, 0), ('cc', 0, 66, 0),
                                 ('cc', 0, 123, 0), ('pitch_bend', 0, 0)])

    def test_release_failure_still_attempts_remaining_calls(self):
        for bad_controller in (64, 66, 123):
            with self.subTest(controller=bad_controller):
                player = make_player()
                original = player.fs.cc
                def cc(channel, controller, value):
                    original(channel, controller, value)
                    return -1 if controller == bad_controller else 0
                player.fs.cc = cc
                self.assertFalse(player._release_all_native_locked())
                self.assertEqual(len(player.fs.calls), 4)
                self.assertEqual(player.fs.calls[-1][0], 'pitch_bend')
                self.assertEqual(player._midi_native_errors, 1)

    def test_release_exception_still_resets_pitch_and_latches_failure(self):
        player = make_player(active_notes=3)
        original = player.fs.cc
        def cc(channel, controller, value):
            original(channel, controller, value)
            if controller == 123:
                raise RuntimeError('injected channel release exception')
        player.fs.cc = cc
        with self.assertRaisesRegex(RuntimeError, 'all-notes-off was incomplete'):
            player.all_notes_off()
        self.assertEqual(player.fs.calls[-1][0], 'pitch_bend')
        self.assertTrue(player._midi_recovery_failed_latched)
        self.assertEqual(player._active_notes, 3)

    def test_incomplete_synchronous_release_raises_and_latches(self):
        player = make_player(active_notes=2)
        player._comp_envelope = 0.5
        player.fs.cc = lambda channel, controller, value: -1

        with self.assertRaisesRegex(RuntimeError, "all-notes-off was incomplete"):
            player.all_notes_off()
        self.assertEqual(player._active_notes, 2)
        status = player.midi_render_status()
        self.assertTrue(status["recovery_pending"])
        self.assertEqual(status["recovery_failures"], 1)

    def test_disable_race_cannot_enqueue_a_stale_note_on(self):
        player = make_player()
        player.enabled = False
        self.assertFalse(player._queue_midi_event("note_on", 60, 90, 0.7))
        self.assertEqual(player.midi_render_status()["queued"], 0)

    def test_program_switch_applies_earlier_notes_before_new_program(self):
        player = make_player(active_notes=0)
        player.current_soundfont = "Salamander"
        player._sfid_by_file = {"FluidR3_GM": 7}
        player.tremolo_hz = 0.0
        player.tremolo_depth = 0.0
        player.velocity_curve = 1.0
        player._tremolo_phase = 0.0
        player.note_on(60, 0.5)

        player.set_soundfont("Fluid")

        self.assertEqual([call[0] for call in player.fs.calls],
                         ["noteon", "program_select"])
        self.assertEqual(player.current_soundfont, "Fluid")

    def test_unmatched_noteoff_status_is_not_a_recovery_failure(self):
        player = make_player()
        player.fs.noteoff = lambda channel, note: -1
        player.note_off(60)
        rendered = player.render_block(512)
        self.assertGreater(float(np.abs(rendered).max()), 0.0)
        status = player.midi_render_status()
        self.assertEqual(status["noteoff_unmatched"], 1)
        self.assertEqual(status["native_errors"], 0)
        self.assertEqual(status["recoveries"], 0)

    def test_pending_note_wakes_player_after_idle_threshold(self):
        player = make_player(active_notes=0)
        player._silent_blocks = 500
        player.note_on(60, 0.5)
        rendered = player.render_block(512)
        self.assertGreater(float(np.abs(rendered).max()), 0.0)
        self.assertEqual([call[0] for call in player.fs.calls],
                         ["noteon", "get_samples"])

    def test_note_and_pitch_commands_keep_fifo_order(self):
        player = make_player(active_notes=0)
        player.note_on(60, 0.5)
        player.midi_callback("pitch_bend", 9000, 0.0)
        player.note_off(60)
        player.render_block(512)
        self.assertEqual([call[0] for call in player.fs.calls],
                         ["noteon", "pitch_bend", "noteoff", "get_samples"])

    def test_panic_waits_for_detached_render_batch_then_clears_state(self):
        player = make_player(active_notes=0)
        native_entered = threading.Event()
        release_native = threading.Event()
        original_noteon = player.fs.noteon

        def blocking_noteon(channel, note, velocity):
            native_entered.set()
            release_native.wait(2.0)
            return original_noteon(channel, note, velocity)

        player.fs.noteon = blocking_noteon
        player.note_on(60, 0.5)
        render = threading.Thread(target=lambda: player.render_block(512))
        render.start()
        self.assertTrue(native_entered.wait(1.0))
        panic_result = []
        panic = threading.Thread(target=lambda: panic_result.append(
            player.all_notes_off()
        ))
        panic.start()
        self.assertTrue(panic.is_alive(), "panic bypassed native render owner")
        release_native.set()
        render.join(timeout=2.0)
        panic.join(timeout=2.0)
        self.assertFalse(render.is_alive())
        self.assertFalse(panic.is_alive())
        self.assertEqual(panic_result, [True])
        self.assertEqual(player._active_notes, 0)
        self.assertEqual(player.midi_render_status()["queued"], 0)

    def test_failed_overflow_recovery_latches_silence_and_retries(self):
        player = make_player()
        capacity = MIDI_EVENT_QUEUE_CAPACITY
        for index in range(capacity):
            player._queue_midi_event("note_on", index % 128, 64, 0.5)
        player._queue_midi_event("note_on", 1, 64, 0.5)
        player.fs.cc = lambda channel, controller, value: -1

        failed = player.render_block(512)
        self.assertTrue(np.array_equal(failed, np.zeros((2, 512))))
        status = player.midi_render_status()
        self.assertTrue(status["recovery_pending"])
        self.assertEqual(status["recovery_failures"], 1)
        self.assertGreater(status["native_errors"], 0)

        player.fs.cc = lambda channel, controller, value: 0
        recovered = player.render_block(512)
        self.assertGreater(float(np.abs(recovered).max()), 0.0)
        status = player.midi_render_status()
        self.assertFalse(status["recovery_pending"])
        self.assertEqual(status["recoveries"], 1)

    def test_failed_recovery_never_waits_on_event_lock_or_exposes_audio(self):
        player = make_player()
        player._midi_recovery_pending = True
        player.fs.cc = lambda channel, controller, value: -1

        first = player.render_block(512)
        self.assertTrue(np.array_equal(first, np.zeros((2, 512))))
        self.assertTrue(player._midi_recovery_failed_latched)

        self.assertTrue(player._midi_event_lock.acquire(blocking=False))
        completed = threading.Event()
        rendered = []

        def render_while_event_owner_stalled():
            rendered.append(player.render_block(512))
            completed.set()

        worker = threading.Thread(target=render_while_event_owner_stalled)
        worker.start()
        try:
            self.assertTrue(completed.wait(1.0), "render waited on event lock")
        finally:
            player._midi_event_lock.release()
            worker.join(timeout=2.0)
        self.assertTrue(np.array_equal(rendered[0], np.zeros((2, 512))))
        self.assertFalse(any(call[0] == "get_samples" for call in player.fs.calls))

    def test_native_transition_contention_is_counted_not_hidden(self):
        player = make_player()
        locked = threading.Event()
        release = threading.Event()

        def hold_native():
            with player._lock:
                locked.set()
                release.wait(2.0)

        owner = threading.Thread(target=hold_native)
        owner.start()
        self.assertTrue(locked.wait(1.0))
        try:
            rendered = player.render_block(512)
        finally:
            release.set()
            owner.join(timeout=2.0)
        self.assertTrue(np.array_equal(rendered, np.zeros((2, 512))))
        self.assertEqual(player.midi_render_status()["native_render_lock_misses"], 1)

    def test_no_event_path_preserves_exact_fake_source_output(self):
        first = make_player()
        second = make_player()
        self.assertTrue(np.array_equal(first.render_block(512),
                                       second.render_block(512)))


if __name__ == "__main__":
    unittest.main()
