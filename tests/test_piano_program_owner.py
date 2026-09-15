"""Device-free program-switch ordering and lifecycle regressions."""
import ast
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_piano_event_ownership import make_player, ENV


class PianoProgramOwnerTests(unittest.TestCase):
    def setUp(self):
        self.presets = patch.dict(ENV['SOUNDFONT_PRESETS'], {
            'Rhodes': {'file': 'FluidR3_GM', 'program': 4, 'velocity_curve': 2.0,
                       'tremolo_hz': 5.5, 'tremolo_depth': .5}})
        self.presets.start()
        self.addCleanup(self.presets.stop)

    def player(self):
        p = make_player()
        p._sfid_by_file = {'FluidR3_GM': 7}
        p.sfid = 7
        p.current_soundfont = 'Fluid'
        p._loaded_file = 'FluidR3_GM'
        p._program_request = None
        p._tremolo_phase = 0.0
        p.set_render_owner_attached(True)
        return p

    def launch(self, p):
        errors = []
        def switch():
            try:
                p.set_soundfont('Rhodes')
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=switch, daemon=True)
        thread.start()
        deadline = time.monotonic() + .5
        while p._program_request is None and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertIsNotNone(p._program_request)
        return thread, errors

    def joined(self, thread):
        thread.join(.5)
        self.assertFalse(thread.is_alive())

    def test_fifo_program_metadata_velocity_and_samples_share_render_owner(self):
        p = self.player()
        p.note_on(60, .25)
        thread, errors = self.launch(p)
        p.note_on(64, .25)
        self.assertEqual(p.fs.calls, [])
        audio = p.render_block(512)
        self.joined(thread)
        self.assertFalse(errors)
        self.assertEqual([c[0] for c in p.fs.calls],
                         ['noteon', 'program_select', 'noteon', 'get_samples'])
        notes = [c for c in p.fs.calls if c[0] == 'noteon']
        self.assertEqual([c[-1] for c in notes], [31, 63])
        self.assertEqual({c[1] for c in p.fs.calls}, {threading.get_ident()})
        self.assertEqual((p.current_soundfont, p.velocity_curve, p.tremolo_depth), ('Rhodes', 2., .5))
        self.assertGreater(abs(audio).max(), 0)
        self.assertEqual(p.midi_render_status()['native_render_lock_misses'], 0)

    def test_disabled_piano_accepts_program_without_rendering_audio(self):
        p = self.player()
        p.enabled = False
        thread, errors = self.launch(p)
        self.assertEqual(abs(p.render_block(512)).max(), 0)
        self.joined(thread)
        self.assertFalse(errors)
        self.assertEqual([c[0] for c in p.fs.calls], ['program_select'])

    def test_unrouted_piano_pump_handles_program_without_sampling(self):
        p = self.player()
        p.enabled = False
        thread, errors = self.launch(p)
        p.pump_render_controls()
        self.joined(thread)
        self.assertFalse(errors)
        self.assertEqual(p.current_soundfont, 'Rhodes')
        self.assertNotIn('get_samples', [c[0] for c in p.fs.calls])

    def test_pump_defers_busy_native_owner_without_dropping_request(self):
        p = self.player()
        thread, errors = self.launch(p)
        owned, release = threading.Event(), threading.Event()
        def hold():
            with p._lock:
                owned.set()
                release.wait(.5)
        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(owned.wait(.5))
        try:
            p.pump_render_controls()
            self.assertEqual(p._program_request['state'], 'pending')
            self.assertFalse(p.fs.calls)
        finally:
            release.set()
            self.joined(holder)
        p.pump_render_controls()
        self.joined(thread)
        self.assertFalse(errors)

    def test_pending_timeout_cannot_execute_later(self):
        p = self.player()
        thread, errors = self.launch(p)
        thread.join(1.5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        p.pump_render_controls()
        self.assertEqual(p.current_soundfont, 'Fluid')
        self.assertFalse(p.fs.calls)
        self.assertEqual(p.midi_render_status()['pending'], 0)

    def test_claimed_native_call_is_not_reported_as_cancelled_on_timeout(self):
        p = self.player()
        entered, release = threading.Event(), threading.Event()
        native = p.fs.program_select
        def blocked(*args):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test release missing')
            return native(*args)
        p.fs.program_select = blocked
        caller, errors = self.launch(p)
        owner = threading.Thread(target=p.pump_render_controls)
        owner.start()
        self.assertTrue(entered.wait(.5))
        try:
            caller.join(1.1)
            self.assertTrue(caller.is_alive())
            self.assertEqual(p.midi_render_status()['program_state'], 'claimed')
            self.assertEqual(p.midi_render_status()['pending'], 1)
        finally:
            release.set()
            self.joined(owner)
            self.joined(caller)
        self.assertFalse(errors)
        self.assertEqual(p.current_soundfont, 'Rhodes')

    def test_panic_cancels_pending_program_and_never_resurrects_it(self):
        p = self.player()
        thread, errors = self.launch(p)
        p.all_notes_off()
        self.joined(thread)
        self.assertTrue(errors)
        p.pump_render_controls()
        self.assertNotIn('program_select', [c[0] for c in p.fs.calls])

    def test_overflow_resolves_program_waiter_and_keeps_recovery_visible(self):
        p = self.player()
        thread, errors = self.launch(p)
        for i in range(256):
            p.note_on(i % 128, .5)
        self.joined(thread)
        self.assertTrue(errors)
        self.assertTrue(p.midi_render_status()['recovery_pending'])
        p.render_block(512)
        self.assertNotIn('program_select', [c[0] for c in p.fs.calls])

    def test_detach_cancels_pending_program_before_direct_ownership_resumes(self):
        p = self.player()
        thread, errors = self.launch(p)
        p.set_render_owner_attached(False)
        self.joined(thread)
        self.assertTrue(errors)
        self.assertEqual(p.midi_render_status()['pending'], 0)
        p.set_soundfont('Rhodes')
        self.assertEqual(p.current_soundfont, 'Rhodes')

    def test_native_failure_keeps_old_metadata_and_resolves_waiter(self):
        p = self.player()
        p.fs.program_select = lambda *args: -1
        thread, errors = self.launch(p)
        p.pump_render_controls()
        self.joined(thread)
        self.assertTrue(errors)
        self.assertEqual((p.current_soundfont, p.velocity_curve), ('Fluid', 1.))
        self.assertGreater(p.midi_render_status()['native_errors'], 0)

    def test_failed_note_before_detached_marker_resolves_claimed_waiter(self):
        p = self.player()
        p.fs.noteon = lambda *args: -1
        p.note_on(60, .5)
        thread, errors = self.launch(p)
        p.pump_render_controls()
        self.joined(thread)
        self.assertTrue(errors)
        self.assertEqual(p.current_soundfont, 'Fluid')
        self.assertNotIn('program_select', [c[0] for c in p.fs.calls])

    def test_render_batch_limit_preserves_program_boundary_across_blocks(self):
        p = self.player()
        for i in range(65):
            p.note_on(i, .25)
        thread, errors = self.launch(p)
        p.render_block(512)
        self.assertEqual(p.current_soundfont, 'Fluid')
        self.assertEqual(p._program_request['state'], 'pending')
        p.render_block(512)
        self.joined(thread)
        self.assertFalse(errors)
        self.assertEqual(p.current_soundfont, 'Rhodes')

    def test_unexpected_batch_failure_does_not_strand_claimed_waiter(self):
        p = self.player()
        p.note_on(60, .5)
        p.velocity_curve = object()  # simulate an unexpected owner-side fault
        thread, errors = self.launch(p)
        with self.assertRaises(TypeError):
            p.pump_render_controls()
        self.joined(thread)
        self.assertTrue(errors)
        self.assertEqual(p.current_soundfont, 'Fluid')

    def test_missing_bank_and_concurrent_request_are_rejected_before_mutation(self):
        p = self.player()
        p._sfid_by_file.clear()
        with self.assertRaisesRegex(RuntimeError, 'preloading'):
            p.set_soundfont('Rhodes')
        self.assertFalse(p._midi_events)
        p._sfid_by_file['FluidR3_GM'] = 7
        thread, errors = self.launch(p)
        with self.assertRaisesRegex(RuntimeError, 'busy'):
            p.set_soundfont('Fluid')
        p.pump_render_controls()
        self.joined(thread)
        self.assertFalse(errors)

    def test_jack_keeps_control_owner_across_routing_and_detaches_after_join(self):
        path = Path(__file__).resolve().parents[1] / 'stave_synth/jack_engine.py'
        source = path.read_text()
        tree = ast.parse(source)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'JackEngine')
        methods = {n.name: ast.get_source_segment(source, n) for n in cls.body if isinstance(n, ast.FunctionDef)}
        self.assertIn('self._piano_control_player = piano_player', methods['__init__'])
        self.assertLess(methods['start'].index('attach(True)'), methods['start'].index('self._render_thread.start()'))
        self.assertIn('control_player.pump_render_controls()', methods['_render_loop'])
        self.assertLess(methods['stop'].index('if alive:'), methods['stop'].index('detach(False)'))


if __name__ == '__main__':
    unittest.main()
