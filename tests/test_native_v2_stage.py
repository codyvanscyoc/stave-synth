"""Device-free native candidate contracts. Fake child, temporary state, loopback only."""
import configparser
import http.client
import importlib.util
import json
import os
import shlex
import socket
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("native_stage_test", ROOT / "tools/native_v2_stage.py")
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)
ROUTES = dict(midi_source="Keyboard:midi", audio_left="Interface:left", audio_right="Interface:right")
INVENTORY = """Keyboard:midi
    properties: output,physical,terminal,
    8 bit raw midi
Interface:left
    properties: input,physical,terminal,
    32 bit float mono audio
Interface:right
    properties: input,physical,terminal,
    32 bit float mono audio
"""


def write_take(path, frames=512, sample=0):
    with wave.open(str(path), "wb") as writer:
        writer.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        writer.writeframes(sample.to_bytes(2, "little") * frames)


class NativeStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = stage.storage.ControlStore(Path(self.temp.name)/"native_controls.json", stage.audition.validate_control)
        self.hub = stage.CandidateHub(self.store, ROUTES)
        self.hub.inventory(stage.parse_ports(INVENTORY))

    def attached(self):
        child = mock.Mock(); child.poll.return_value = None
        control = self.hub.attach(child)
        control.receive(dict(type="status", instance="native-v2-stage", blocks=1, applied=0, routed=True, fault=0))
        return control

    def test_snapshot_roundtrip_and_action_exclusion(self):
        self.assertEqual(self.store.load(), {})
        self.store.save(dict(piano=.7, piano_tone=.4, master=.65))
        self.assertEqual(self.store.load(), dict(piano=.7, piano_tone=.4, master=.65))
        before = self.store.path.read_bytes()
        for key in stage.storage.TRANSIENT:
            with self.assertRaises(ValueError): self.store.save({key: 0})
        for value in (True, float("nan"), float("inf"), -1, 3, "0.5"):
            with self.assertRaises(ValueError): self.store.save(dict(piano=value))
        self.assertEqual(self.store.path.read_bytes(), before)
        with mock.patch.object(stage.storage.atomic, "atomic_write_json", side_effect=OSError("full disk")):
            with self.assertRaises(OSError): self.store.save(dict(piano=.2))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_five_native_preset_slots_save_prepare_and_queue_master_last(self):
        self.hub.dispatch('/control', dict(key='piano', value=.72), self.hub.epoch)
        self.hub.dispatch('/control', dict(key='master', value=.64), self.hub.epoch)
        result = self.hub.dispatch('/preset-save', dict(slot=2, name='Sunday Piano'), self.hub.epoch)
        self.assertEqual(result['presets'][2]['name'], 'Sunday Piano')
        self.hub.dispatch('/control', dict(key='piano', value=.2), self.hub.epoch)
        self.hub.dispatch('/preset-load', dict(slot=2), self.hub.epoch)
        self.assertEqual(self.hub.snapshot()['values']['piano'], .72)
        control = self.attached(); self.hub.reconcile()
        while not control.outgoing.empty():
            control.outgoing.get_nowait()
        control.receive(dict(type='status', instance='native-v2-stage', blocks=2,
                             applied=control.sequence, routed=True, fault=0))
        self.hub.reconcile()
        self.hub.dispatch('/control', dict(key='piano', value=.3), self.hub.epoch)
        control.receive(dict(type='status', instance='native-v2-stage', blocks=3,
                             applied=control.sequence, routed=True, fault=0))
        self.hub.dispatch('/preset-load', dict(slot=2), self.hub.epoch)
        self.assertEqual(list(control.pending.values())[-1], ('master', .64))
        with self.assertRaises(ValueError):
            self.hub.dispatch('/preset-save', dict(slot=5, name='Bad'), self.hub.epoch)

    def test_live_preset_recalls_independent_levels_filters_and_unedited_defaults(self):
        self.hub.presets.save_slot(0, 'Piano', dict(osc1=.8, osc2=.2, attack1=50, attack2=700,
            envelope_link=1, cutoff_min=10000, cutoff_max=18000, cutoff=15555,
            reverb=1, reverb_lowcut=25, reverb_highcut=50))
        control = self.attached()
        self.hub.reconcile()
        for key,value in [('volume_link', 1), ('envelope_link', 1), ('cutoff_max', 5000), ('wet', .1)]:
            control.submit(dict(key=key, value=value))
        self.hub.dispatch('/preset-load', {'slot': 0}, self.hub.epoch)
        control.receive(dict(type='status', instance='native-v2-stage', blocks=2, applied=control.sequence, routed=True, fault=0))
        for key,value in dict(osc1=.8, osc2=.2, attack1=50, attack2=700, envelope_link=1, cutoff=15555,
                              reverb_lowcut=25, reverb_highcut=50, wet=.75).items():
            self.assertEqual(control.values[key], value)

    def test_overfull_preset_is_rejected_before_any_part_is_queued(self):
        self.hub.presets.save_slot(0, 'Bright', {'piano_tone': .98})
        control = self.attached()
        self.hub.reconcile()
        for _ in range(60):
            control.submit({'key': 'piano', 'value': .5})
        before = control.pending.copy()
        with self.assertRaisesRegex(ValueError, 'backlog'):
            self.hub.dispatch('/preset-load', {'slot': 0}, self.hub.epoch)
        self.assertEqual(control.pending, before)

    def test_midi_map_is_unique_persistent_and_restored_before_controls(self):
        control = self.attached(); self.hub.reconcile()
        # Acknowledge each outbound mapping exactly as the native reader would.
        def acknowledge(seq):
            control.receive(dict(type='mapped', id=seq, ok=True))
        control.wait_mapping = acknowledge
        result = self.hub.dispatch('/midi-map', dict(key='osc1', cc=21), self.hub.epoch)
        self.assertEqual(result['midi_maps'], {'osc1': 21})
        control.receive(dict(type='mapped', id=result['queued'], ok=True))
        result = self.hub.dispatch('/midi-map', dict(key='piano', cc=21), self.hub.epoch)
        self.assertEqual(result['midi_maps'], {'piano': 21}, 'one hardware CC owns one target')
        path = Path(self.temp.name) / 'native_midi_map.json'
        self.assertEqual(json.loads(path.read_text())['mappings'], {'piano': 21})
        for data in (dict(key='master', cc=64), dict(key='record_start', cc=22), dict(key='master', cc=True)):
            with self.assertRaises(ValueError):
                self.hub.dispatch('/midi-map', data, self.hub.epoch)

        self.hub = stage.CandidateHub(self.store, ROUTES); self.hub.inventory(stage.parse_ports(INVENTORY))
        restored = self.attached(); self.hub.reconcile()
        self.assertEqual(restored.snapshot()['map_pending'], 1)
        self.assertEqual(restored.outgoing.get_nowait(), ('map', 1, 21, 'piano'))
        self.assertEqual(restored.pending, {}, 'sound restore waits for MIDI-map acknowledgment')
        restored.receive(dict(type='mapped', id=1, ok=True)); self.hub.reconcile()
        self.assertFalse(self.hub.restoring)
        self.assertEqual(self.hub.snapshot()['runtime']['midi_maps'], {'piano': 21})

    def test_assignment_clear_and_durable_rollback(self):
        control = self.attached()
        self.hub.reconcile()
        def acknowledge(seq):
            control.receive(dict(type='mapped', id=seq, ok=True))
        control.wait_mapping = acknowledge
        self.hub.dispatch('/midi-map', dict(key='osc1', cc=21), self.hub.epoch)
        with mock.patch.object(self.hub.midi_map, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.hub.dispatch('/midi-map', dict(key='master', cc=21), self.hub.epoch)
        self.assertEqual(self.hub.midi_map.mappings, {'osc1': 21})
        commands = list(control.outgoing.queue)
        self.assertEqual(commands[-1][-2:], (21, 'osc1'))
        cleared = self.hub.dispatch('/midi-unmap', dict(key='osc1'), self.hub.epoch)
        self.assertEqual(cleared['midi_maps'], {})
        self.assertEqual(self.hub.midi_map.load(), {})

    def test_learn_exclusive_renewal_baseline_and_expired_session(self):
        control = self.attached()
        self.hub.reconcile()
        first, second = "first-panel-token", "second-panel-token"
        def learn(who, active=True, **extra):
            return self.hub.dispatch('/midi-learn', dict(active=active, owner=who, **extra), self.hub.epoch)
        def ack(seq):
            control.receive(dict(type='mapped', id=seq, ok=True, midi_cc_serial=19))
            return 19
        control.wait_mapping = ack
        with self.assertRaises(ValueError):
            learn(first, renew=True)
        self.assertEqual(learn(first)['serial'], 19)
        self.assertEqual(learn(first, renew=True)['serial'], 19)
        for enable in (True, False):
            with self.assertRaisesRegex(ValueError, 'Another panel'):
                learn(second, enable)
        with self.assertRaisesRegex(ValueError, 'ownership'):
            self.hub.dispatch('/midi-map', dict(key='master', cc=22, owner=second), self.hub.epoch)
        self.hub.dispatch('/midi-map', dict(key='master', cc=22, owner=first), self.hub.epoch)
        self.hub.learn_until = 0
        with self.assertRaisesRegex(ValueError, 'expired'):
            learn(first, renew=True)
        with self.assertRaisesRegex(ValueError, 'ownership'):
            self.hub.dispatch('/midi-map', dict(key='piano', cc=22, owner=first), self.hub.epoch)
        learn(second)
        learn(second, False)
        self.assertIsNone(self.hub.learn_owner)
        self.assertEqual(self.hub.midi_map.mappings, {'master': 22})

    def test_dirty_flags_and_keyboard_setup_are_independent_of_slot(self):
        status = lambda: self.hub.snapshot()['runtime']
        self.assertFalse(status()['startup_dirty'])
        self.hub.dispatch('/control', dict(key='piano', value=.81), self.hub.epoch)
        self.assertTrue(status()['startup_dirty'])
        self.hub.dispatch('/preset-save', {'slot': 3, 'name': 'Warm piano'}, self.hub.epoch)
        self.assertEqual(status()['active_preset'], 3)
        self.assertFalse(status()['preset_dirty'])
        self.hub.dispatch('/control', dict(key='midi_channel', value=3), self.hub.epoch)
        self.assertFalse(status()['preset_dirty'])
        self.hub.dispatch('/control', dict(key='piano', value=.2), self.hub.epoch)
        self.assertTrue(status()['preset_dirty'])
        self.hub.dispatch('/preset-load', {'slot': 3}, self.hub.epoch)
        self.assertEqual(self.hub.prepared['midi_channel'], 3)
        self.assertFalse(status()['preset_dirty'])
        self.hub.dispatch('/save', {}, self.hub.epoch)
        self.assertFalse(status()['startup_dirty'])

    def test_acknowledged_output_mute_survives_child_replacement(self):
        first = self.attached()
        self.hub.reconcile()
        self.hub.dispatch('/control', dict(key='output_mute', value=1), self.hub.epoch)
        self.hub.detached('replacing')
        self.assertTrue(self.hub.output_muted)
        new = self.attached()
        self.hub.reconcile()
        self.assertEqual(next(iter(new.pending.values())), ('output_mute', 1))
        self.assertNotIn('output_mute', self.store.load())

    def test_malformed_oversized_and_symlink_state_never_loaded(self):
        for data in (b"broken", b" "*16385, b'[]', b'{"schema":true,"controls":{}}',
                     b'{"schema":1,"controls":{"master":2}}'):
            self.store.path.write_bytes(data)
            hub = stage.CandidateHub(self.store, ROUTES)
            self.assertIsNotNone(hub.state_warning)
            self.assertEqual(self.store.path.read_bytes(), data)
        self.store.path.unlink()
        target = Path(self.temp.name)/"target"; target.write_text("keep")
        self.store.path.symlink_to(target)
        with self.assertRaises(OSError): self.store.load()
        with self.assertRaises(ValueError): self.store.save({})
        self.assertEqual(target.read_text(), "keep")

    def test_restore_waits_for_ack_and_omits_empty_bed_knobs(self):
        self.hub.prepared = dict(piano=.8, bed_level=.2)
        c = self.attached(); epoch = self.hub.epoch
        self.hub.reconcile()
        self.assertTrue(self.hub.restoring)
        self.assertEqual(list(c.pending.values()), [("piano", .8)])
        with self.assertRaises(ValueError): self.hub.dispatch("/control", dict(key="master", value=1), epoch)
        c.receive(dict(type="status", instance="native-v2-stage", blocks=2, applied=1, routed=True, fault=0))
        self.hub.reconcile()
        self.assertFalse(self.hub.restoring)
        self.assertEqual(c.values["master"], 0)
        self.hub.dispatch("/save", {}, epoch)
        self.assertEqual(self.store.load()["bed_level"], .2)
        self.assertFalse(stage.storage.TRANSIENT & self.store.load().keys())

    def test_restart_works_without_child_and_old_actions_are_refused(self):
        epoch = self.hub.epoch
        self.hub.dispatch("/restart-audio", {}, epoch)
        self.assertNotEqual(epoch, self.hub.epoch)
        with self.assertRaises(ValueError): self.hub.dispatch("/restart-audio", {}, epoch)
        self.assertTrue(self.hub.take_restart())
        self.hub.dispatch("/restart-audio", {}, self.hub.epoch)
        self.assertTrue(self.hub.take_restart())  # newer request survives consumption
        self.assertFalse(self.hub.take_restart())

    def test_prepare_without_devices_save_reload_then_restore_muted(self):
        self.hub.inventory({})
        epoch = self.hub.epoch
        self.assertTrue(self.hub.snapshot()['runtime']['preparation'])
        for key, value in [('piano', .81), ('cutoff', 734), ('attack', 670), ('bed_level', .25)]:
            result = self.hub.dispatch('/control', dict(key=key, value=value), epoch)
            self.assertTrue(result['prepared'])
            self.assertEqual(self.hub.snapshot()['values'][key], value)
        self.assertFalse(self.store.path.exists(), 'edits do not implicitly overwrite saved sound')
        for key in stage.storage.TRANSIENT:
            with self.assertRaises(ValueError):
                self.hub.dispatch('/control', dict(key=key, value=1), epoch)
        with self.assertRaises(ValueError):
            self.hub.dispatch('/control', dict(key='piano', value=float('nan')), epoch)
        self.hub.dispatch('/save', {}, epoch)
        self.hub = stage.CandidateHub(self.store, ROUTES)
        self.assertEqual(self.hub.snapshot()['values']['piano'], .81)
        self.assertEqual(self.hub.snapshot()['values']['master'], 0)
        old_epoch = self.hub.epoch
        control = self.attached()
        with self.assertRaises(ValueError):
            self.hub.dispatch('/control', dict(key='piano', value=.2), old_epoch)
        self.hub.reconcile()
        self.assertEqual(dict(control.pending.values()), dict(piano=.81, cutoff=734, attack1=670, attack2=670))
        sequence = control.sequence
        control.receive(dict(type='status', instance='native-v2-stage', blocks=2, applied=sequence, routed=True, fault=0))
        self.hub.reconcile()
        self.assertFalse(self.hub.restoring)
        self.assertEqual(self.hub.snapshot()['values']['piano'], .81)
        self.assertEqual(self.hub.snapshot()['values']['bed_level'], .25)
        self.assertEqual(control.values['master'], 0)

    def test_legacy_envelope_migration_preserves_explicit_values(self):
        self.store.path.write_text(json.dumps(dict(schema=1, controls=dict(attack=670, release=950, attack2=120))))
        loaded = self.store.load()
        self.assertEqual(loaded, dict(attack1=670, attack2=120, release1=950, release2=950))
        self.assertNotIn('attack', self.store.save(loaded))
        self.assertEqual(self.store.load(), loaded)

    def test_link_is_one_acknowledged_transaction_and_restores_last(self):
        epoch = self.hub.epoch
        for key, value in [('attack1', 120), ('attack2', 770), ('envelope_link', 1)]:
            self.hub.dispatch('/control', dict(key=key, value=value), epoch)
        values = self.hub.snapshot()['values']
        self.assertEqual((values['attack1'], values['attack2']), (120, 770))
        self.hub.dispatch('/save', {}, epoch)
        self.hub = stage.CandidateHub(self.store, ROUTES)
        c = self.attached(); self.hub.reconcile()
        self.assertEqual(list(c.pending.values())[-1], ('envelope_link', 1))
        c.receive(dict(type='status', instance='native-v2-stage', blocks=2, applied=c.sequence, routed=True, fault=0))
        self.hub.reconcile()
        self.assertEqual((c.values['attack1'], c.values['attack2']), (120, 770))
        seq = c.submit(dict(key='decay2', value=2345))
        self.assertEqual(len(c.pending), 1)
        self.assertEqual((c.values['decay1'], c.values['decay2']), (1500, 1500))
        c.receive(dict(type='status', instance='native-v2-stage', blocks=3, applied=seq, routed=True, fault=0))
        self.assertEqual((c.values['decay1'], c.values['decay2']), (2345, 2345))
        self.hub.detached('No keyboard')
        self.hub.dispatch('/control', dict(key='sustain1', value=42), self.hub.epoch)
        values = self.hub.snapshot()['values']
        self.assertEqual((values['sustain1'], values['sustain2']), (42, 42))
        before = values.copy()
        with self.assertRaises(ValueError):
            self.hub.dispatch('/control', dict(key='sustain2', value=101), self.hub.epoch)
        self.assertEqual(self.hub.snapshot()['values'], before)
        self.hub.dispatch('/control', dict(key='envelope_link', value=0), self.hub.epoch)
        self.hub.dispatch('/control', dict(key='release2', value=1350), self.hub.epoch)
        self.assertEqual(self.hub.snapshot()['values']['release1'], 500)

    def test_volume_link_preserves_balance_through_edges_and_restore(self):
        epoch = self.hub.epoch
        for key, value in [('osc1', .7), ('osc2', .4), ('volume_link', 1)]:
            self.hub.dispatch('/control', dict(key=key, value=value), epoch)
        self.hub.dispatch('/control', dict(key='osc1', value=.9), epoch)
        self.assertAlmostEqual(self.hub.prepared['osc1'], .9)
        self.assertAlmostEqual(self.hub.prepared['osc2'], .6)
        self.hub.dispatch('/control', dict(key='osc1', value=0), epoch)
        self.assertEqual((self.hub.prepared['osc1'], self.hub.prepared['osc2']), (0, 0))
        self.hub.dispatch('/control', dict(key='osc1', value=.7), epoch)
        self.assertEqual((self.hub.prepared['osc1'], self.hub.prepared['osc2']), (.7, .4))
        self.hub.dispatch('/save', {}, epoch)

        self.hub = stage.CandidateHub(self.store, ROUTES)
        control = self.attached(); self.hub.reconcile()
        self.assertEqual(list(control.pending.values())[-1], ('volume_link', 1))
        control.receive(dict(type='status', instance='native-v2-stage', blocks=2,
                             applied=control.sequence, routed=True, fault=0))
        self.hub.reconcile()
        sequence = control.submit(dict(key='osc2', value=.2))
        control.receive(dict(type='status', instance='native-v2-stage', blocks=3,
                             applied=sequence, routed=True, fault=0))
        self.assertAlmostEqual(control.values['osc1'], .5)
        self.assertAlmostEqual(control.values['osc2'], .2)

    def test_global_tone_is_bounded_and_saved_without_devices(self):
        for key, value in [('master_low', -2.3), ('master_mid', 1.4), ('master_high', 0), ('master_lowcut', 1), ('master_lowcut_hz', 43)]:
            self.hub.dispatch('/control', dict(key=key, value=value), self.hub.epoch)
        self.hub.dispatch('/save', {}, self.hub.epoch)
        self.assertEqual(self.store.load()['master_low'], -2.3)
        for key, value in [('master_low', 7), ('master_lowcut', .5), ('master_lowcut_hz', 201)]:
            with self.assertRaises(ValueError):
                self.hub.dispatch('/control', dict(key=key, value=value), self.hub.epoch)

    def test_musical_effect_controls_prepare_and_reverb_type_updates_preset(self):
        values = [('piano_room_size', .72), ('piano_room_damp', .81), ('delay_time', 417),
                  ('delay_lowcut', 95), ('delay_highcut', 9200), ('reverb', 1)]
        for key, value in values:
            self.hub.dispatch('/control', dict(key=key, value=value), self.hub.epoch)
        current = self.hub.snapshot()['values']
        self.assertEqual(tuple(current[k] for k in ('reverb_decay', 'reverb_predelay', 'reverb_lowcut', 'reverb_highcut', 'reverb_damp')),
                         (9, 45, 120, 8500, .35))
        self.hub.dispatch('/control', dict(key='reverb_decay', value=7.4), self.hub.epoch)
        self.assertEqual(self.hub.snapshot()['values']['reverb_decay'], 7.4)
        self.hub.dispatch('/save', {}, self.hub.epoch)
        loaded = self.store.load()
        self.assertEqual(loaded['delay_time'], 417)
        self.assertEqual(loaded['reverb_decay'], 7.4)
        for key, value in [('piano_room_damp', 1), ('delay_time', 0), ('delay_highcut', 499),
                           ('reverb_predelay', 151), ('reverb_lowcut', 19)]:
            with self.assertRaises(ValueError):
                self.hub.dispatch('/control', dict(key=key, value=value), self.hub.epoch)
        self.hub.dispatch('/control', dict(key='delay_lowcut', value=900), self.hub.epoch)
        self.hub.dispatch('/control', dict(key='reverb_lowcut', value=8000), self.hub.epoch)
        for key, value in [('delay_highcut', 800), ('reverb_highcut', 7000)]:
            with self.assertRaisesRegex(ValueError, 'cut'):
                self.hub.dispatch('/control', dict(key=key, value=value), self.hub.epoch)

    def test_existing_low_cost_engine_controls_prepare_save_and_restore(self):
        values = [('piano_lowcut', 48), ('piano_velocity', 1.7),
                  ('osc1_reverb', .74), ('osc2_reverb', .39), ('piano_delay', .31),
                  ('filter_slope', 1), ('piano_filter', 1), ('bpm', 93),
                  ('delay_time', 417), ('delay_division', 5)]
        for key, value in values:
            self.hub.dispatch('/control', dict(key=key, value=value), self.hub.epoch)
        self.assertEqual(self.hub.snapshot()['values']['delay_division'], 5)
        self.hub.dispatch('/save', {}, self.hub.epoch)
        loaded = self.store.load()
        for key, value in values:
            self.assertEqual(loaded[key], value)
        items = list(stage.audition.restore_items(loaded))
        restored = dict(items)
        self.assertEqual(restored['delay_time'], 417)
        self.assertLess(items.index(('delay_time', 417)), items.index(('delay_division', 5)))
        for key, value in [('piano_lowcut', 501), ('piano_velocity', .49),
                           ('filter_slope', .5), ('bpm', 39), ('delay_division', 9)]:
            with self.assertRaises(ValueError):
                self.hub.dispatch('/control', dict(key=key, value=value), self.hub.epoch)

    def test_piano_voice_and_both_lfos_round_trip_and_legacy_preset_resets_motion(self):
        patch = dict(piano_voice=2, lfo1_rate=.29, lfo1_depth=.72, lfo1_shape=6, lfo1_target=0,
                     lfo1_division=3, lfo1_key_sync=1, lfo2_rate=7.81, lfo2_depth=.85,
                     lfo2_shape=2, lfo2_target=3, lfo2_division=8, lfo2_key_sync=1, wheel_depth=.64)
        self.hub.presets.save_slot(0, 'Old piano', dict(piano=.46))
        for key, value in patch.items():
            self.hub.dispatch('/control', dict(key=key, value=value), self.hub.epoch)
        self.hub.dispatch('/preset-save', dict(slot=1, name='Moving suitcase'), self.hub.epoch)
        self.hub.dispatch('/save', {}, self.hub.epoch)
        for saved in (self.store.load(), self.hub.presets.controls_for(1)):
            self.assertEqual({k: saved[k] for k in patch}, patch)
        owner = self.attached()
        self.hub.reconcile()
        def acknowledge(block):
            while not owner.outgoing.empty():
                owner.outgoing.get_nowait()
            owner.receive(dict(type='status', instance='native-v2-stage', blocks=block,
                               applied=owner.sequence, routed=True, fault=0))
            self.hub.reconcile()
        acknowledge(2)
        self.assertEqual({k: owner.values[k] for k in patch}, patch)
        self.hub.dispatch('/preset-load', dict(slot=0), self.hub.epoch)
        acknowledge(3)
        self.assertEqual(owner.values['piano'], .46)
        self.assertEqual({k: owner.values[k] for k in patch},
                         {k: stage.audition.CONTROLS[k][3] for k in patch})
        self.hub.dispatch('/preset-load', dict(slot=1), self.hub.epoch)
        acknowledge(4)
        self.assertEqual({k: owner.values[k] for k in patch}, patch)

    def test_missing_optional_soundfont_voice_restores_grand_and_refuses_partial_preset(self):
        self.store.save(dict(piano_voice=2, master=.59))
        self.hub = stage.CandidateHub(self.store, ROUTES)
        self.hub.presets.save_slot(1, 'Legend', dict(osc1=.7, piano_voice=3))
        owner = self.attached()
        def status(block):
            owner.receive(dict(type='status', instance='native-v2-stage', blocks=block, bed_mask=0,
                               piano_voice_mask=1, applied=owner.sequence, routed=True, fault=0))
            self.hub.reconcile()
        status(2)
        self.assertIn(('piano_voice', 0), owner.pending.values())
        self.assertIn(('master', .59), owner.pending.values())
        status(3)
        self.assertFalse(self.hub.restoring)
        self.assertIn('Grand', self.hub.message)
        self.assertEqual(self.store.load()['piano_voice'], 2)
        before = owner.sequence
        for path, payload in (('/preset-load', dict(slot=1)), ('/control', dict(key='piano_voice', value=1))):
            with self.assertRaisesRegex(ValueError, 'SoundFont'):
                self.hub.dispatch(path, payload, self.hub.epoch)
        self.assertEqual(owner.sequence, before)
        self.assertEqual(owner.pending, {})

    def test_full_muted_startup_with_all_pad_settings_fits_one_control_batch(self):
        full = {k: spec[3] for k, spec in stage.audition.CONTROLS.items()
                if k not in stage.storage.TRANSIENT}
        full.update(master=.65, piano_voice=3, lfo1_depth=1, lfo2_depth=1, wheel_depth=1)
        saved = self.store.save(full)
        self.hub = stage.CandidateHub(self.store, ROUTES)
        self.hub.output_muted = True
        owner = self.attached()
        owner.receive(dict(type='status', instance='native-v2-stage', blocks=2, bed_mask=4095,
                           applied=0, routed=True, fault=0))
        self.hub.reconcile()
        batch = list(owner.pending.values())
        self.assertEqual(batch[0], ('output_mute', 1))
        self.assertEqual(batch[-1], ('master', .65))
        self.assertTrue(set(saved.items()).issubset(batch))
        owner.receive(dict(type='status', instance='native-v2-stage', blocks=3, bed_mask=4095,
                           applied=owner.sequence, routed=True, fault=0))
        self.hub.reconcile()
        self.assertFalse(self.hub.restoring)
        self.assertEqual({k: owner.values[k] for k in saved}, saved)

    def test_device_loss_keeps_acknowledged_unsaved_tone_not_actions_or_pending(self):
        control = self.attached()
        self.hub.reconcile()
        control.submit(dict(key='piano', value=.82))
        control.submit(dict(key='master', value=.9))
        control.receive(dict(type='status', instance='native-v2-stage', blocks=2, applied=2, routed=True, fault=0))
        control.submit(dict(key='cutoff', value=200))  # never acknowledged
        self.hub.detached('Keyboard disconnected')
        self.assertEqual(self.hub.snapshot()['values']['piano'], .82)
        self.assertEqual(self.hub.snapshot()['values']['cutoff'], 8000)
        self.assertEqual(self.hub.snapshot()['values']['master'], .9)
        self.assertFalse(self.store.path.exists())
        self.hub.dispatch('/control', dict(key='piano', value=.63), self.hub.epoch)
        control = self.attached()
        self.hub.reconcile()
        self.assertEqual(dict(control.pending.values())['piano'], .63)
        self.assertEqual(list(control.pending.values())[-1], ('master', .9))
        self.assertFalse(stage.storage.TRANSIENT & dict(control.pending.values()).keys())
        # A failed restore must keep the prepared sound, not reset it to defaults.
        self.hub.detached('Startup failed')
        self.assertEqual(self.hub.snapshot()['values']['piano'], .63)

    def test_prepare_save_failure_preserves_previous_file_and_unsaved_draft(self):
        self.hub.dispatch('/control', dict(key='piano', value=.4), self.hub.epoch)
        self.hub.dispatch('/save', {}, self.hub.epoch)
        self.hub.dispatch('/control', dict(key='piano', value=.9), self.hub.epoch)
        with mock.patch.object(stage.storage.atomic, 'atomic_write_json', side_effect=OSError('full disk')):
            with self.assertRaises(OSError): self.hub.dispatch('/save', {}, self.hub.epoch)
        self.assertEqual(self.store.load()['piano'], .4)
        self.assertEqual(self.hub.saved['piano'], .4)
        self.assertEqual(self.hub.snapshot()['values']['piano'], .9)

    def test_empty_bed_shaping_is_prepared_and_survives_save(self):
        control = self.attached()
        self.hub.reconcile()
        self.hub.dispatch('/control', dict(key='bed_rise', value=7), self.hub.epoch)
        self.assertEqual(control.sequence, 0)
        self.assertEqual(self.hub.snapshot()['values']['bed_rise'], 7)
        self.hub.dispatch('/save', {}, self.hub.epoch)
        self.assertEqual(self.store.load()['bed_rise'], 7)
        with self.assertRaises(ValueError):
            self.hub.dispatch('/control', dict(key='bed_key', value=0), self.hub.epoch)

    def test_completed_take_installs_one_slot_with_undo_and_muted_restart(self):
        root = Path(self.temp.name); recordings = root/'recordings'; pads = root/'pads'
        recordings.mkdir(); pads.mkdir()
        self.hub = stage.CandidateHub(self.store, ROUTES, recordings, pads)
        take = recordings/'take-test.wav'; write_take(take, sample=1)
        old = pads/'pad_C.wav'; write_take(old); original = old.read_bytes()
        child = mock.Mock(); child.poll.return_value = None
        control = self.hub.attach(child, take)
        control.receive(dict(type='status', instance='native-v2-stage', blocks=1, applied=0,
                             routed=True, fault=0, writer_state=2, capture_end=5, writer_frames=512))
        self.hub.reconcile(); epoch = self.hub.epoch
        result = self.hub.dispatch('/record-assign', {'slot': 0}, epoch)
        self.assertEqual(old.read_bytes(), take.read_bytes())
        self.addCleanup(self.hub.next_bank.cleanup)
        self.assertEqual(self.hub.snapshot()['runtime']['pad_bank']['used_bytes'], 512 * 16)
        undo = list(pads.glob('undo-pad_C-*.wav'))
        self.assertEqual(len(undo), 1); self.assertEqual(undo[0].read_bytes(), original)
        self.assertTrue(self.hub.restart); self.assertTrue(self.hub.restoring)
        self.assertIn('restarting muted', result['message'])
        self.assertNotEqual(epoch, self.hub.epoch)

    def test_fifth_40_second_take_leaves_playing_bank_and_owner_untouched(self):
        root = Path(self.temp.name); pads = root / "pads"; pads.mkdir()
        take = root / "take.wav"; write_take(take, 40 * 48000)
        for name in stage.assets.FILENAMES[:4]:
            os.link(take, pads / name)
        self.hub = stage.CandidateHub(self.store, ROUTES, pad_library=pads)
        prior = self.hub.prepare_current_bank(); self.addCleanup(prior.cleanup)
        control = self.attached(); self.hub.recording_take = take
        control.receive(dict(type="status", instance="native-v2-stage", blocks=2, applied=0, routed=True,
                             fault=0, writer_state=2, capture_end=5))
        self.hub.reconcile(); epoch = self.hub.epoch
        with self.assertRaisesRegex(ValueError, "budget"):
            self.hub.dispatch("/record-assign", {"slot": 4}, epoch)
        self.assertIs(self.hub.active, control)
        self.assertEqual(self.hub.epoch, epoch)
        self.assertFalse(self.hub.take_restart())
        self.assertEqual(sorted(p.name for p in pads.iterdir()), sorted(stage.assets.FILENAMES[:4]))
        status = self.hub.snapshot()["runtime"]["pad_bank"]
        self.assertEqual(status["used_bytes"], 4 * 40 * 48000 * 16)
        self.assertAlmostEqual(status["remaining_seconds"], 14.7626667, places=5)
        self.assertEqual(status["slot_seconds"][:5], [40, 40, 40, 40, 0])

    def test_invalid_existing_bank_allows_padless_piano_and_reports_bad_key(self):
        pads = Path(self.temp.name) / "pads"; pads.mkdir()
        (pads / "pad_D.wav").write_bytes(b"invalid")
        self.hub = stage.CandidateHub(self.store, ROUTES, pad_library=pads)
        self.assertIsNone(self.hub.prepare_current_bank())
        self.assertIn("pad_D.wav", self.hub.snapshot()["runtime"]["pad_bank"]["warning"])
        self.assertEqual((pads / "pad_D.wav").read_bytes(), b"invalid")

    def test_supervisor_launches_audio_without_invalid_beds(self):
        root = Path(self.temp.name)
        pads = root / "pads"
        pads.mkdir()
        (pads / "pad_D.wav").write_bytes(b"invalid")
        binary, font = root / "engine", root / "keys.sf2"
        binary.touch()
        font.touch()
        args = ["stage", "--listen", "127.0.0.1", "--port", "8089", "--allow-stage-candidate",
                "--binary", str(binary), "--soundfont", str(font), "--state-dir", str(root / "state"),
                "--pad-library", str(pads), "--midi-source", ROUTES["midi_source"],
                "--audio-left", ROUTES["audio_left"], "--audio-right", ROUTES["audio_right"]]
        with mock.patch("sys.argv", args), mock.patch.object(stage, "BoundedHTTPServer"), \
                mock.patch.object(stage.lifecycle.Listeners, "poll", side_effect=lambda: time.sleep(.001)), \
                mock.patch.object(stage.subprocess, "run", return_value=mock.Mock(stdout=INVENTORY, returncode=0)), \
                mock.patch.object(stage.subprocess, "Popen", side_effect=RuntimeError("fixture launch")) as launch:
            with self.assertRaisesRegex(RuntimeError, "fixture launch"):
                stage.main()
        command = launch.call_args.args[0]
        self.assertEqual(command[1:3], [str(binary.resolve()), str(font.resolve())])
        self.assertNotIn("--bed-bank", command)

    def test_assignment_checks_epoch_again_before_slot_commit(self):
        pads = Path(self.temp.name) / "pads"; pads.mkdir()
        take = Path(self.temp.name) / "take.wav"; write_take(take)
        self.hub = stage.CandidateHub(self.store, ROUTES, pad_library=pads)
        control = self.attached(); self.hub.recording_take = take
        control.receive(dict(type="status", instance="native-v2-stage", blocks=2, applied=0, routed=True,
                             fault=0, writer_state=2, capture_end=2))
        self.hub.reconcile(); epoch = self.hub.epoch
        real_prepare = stage.assets.prepare_bank
        def concurrent_changes(*args, **kwargs):
            with self.assertRaisesRegex(ValueError, "Another pad assignment"):
                self.hub.dispatch("/record-assign", {"slot": 0}, epoch)
            self.hub.dispatch("/restart-audio", {}, epoch)
            return real_prepare(*args, **kwargs)
        with mock.patch.object(stage.assets, "prepare_bank", side_effect=concurrent_changes):
            with self.assertRaisesRegex(ValueError, "session changed"):
                self.hub.dispatch("/record-assign", {"slot": 0}, epoch)
        self.assertEqual(list(pads.iterdir()), [])
        self.assertFalse(self.hub.assigning)

    def test_take_assignment_rejects_incomplete_and_symlink_target(self):
        root = Path(self.temp.name); recordings = root/'recordings'; pads = root/'pads'
        recordings.mkdir(); pads.mkdir()
        self.hub = stage.CandidateHub(self.store, ROUTES, recordings, pads)
        take = recordings/'take-test.wav'; take.write_bytes(b'R'*58)
        child = mock.Mock(); child.poll.return_value = None
        control = self.hub.attach(child, take)
        control.receive(dict(type='status', instance='native-v2-stage', blocks=1, applied=0,
                             routed=True, fault=0, writer_state=1, capture_end=1))
        self.hub.reconcile()
        with self.assertRaisesRegex(ValueError, 'wait for Ready'):
            self.hub.dispatch('/record-assign', {'slot': 0}, self.hub.epoch)
        control.receive(dict(type='status', instance='native-v2-stage', blocks=2, applied=0,
                             routed=True, fault=0, writer_state=2, capture_end=2))
        outside = root/'outside.wav'; outside.write_bytes(b'O'*58)
        (pads/'pad_C.wav').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            self.hub.dispatch('/record-assign', {'slot': 0}, self.hub.epoch)
        self.assertEqual(outside.read_bytes(), b'O'*58)

    def test_startup_timeout_includes_missing_telemetry(self):
        child = mock.Mock(); child.poll.return_value = None
        self.hub.attach(child)
        self.hub.startup_started = time.monotonic()-61
        with self.assertRaisesRegex(ValueError, "timed out"): self.hub.reconcile()

    def test_slow_soundfont_startup_has_a_separate_restore_acknowledgment_budget(self):
        self.hub.prepared = {'piano': .4}
        child = mock.Mock(); child.poll.return_value = None
        control = self.hub.attach(child)
        self.hub.startup_started = time.monotonic()-55
        self.hub.reconcile()
        self.assertIsNone(self.hub.restore_started)
        control.receive(dict(type='status', instance='native-v2-stage', blocks=1, applied=0, routed=True, fault=0))
        self.hub.reconcile()
        self.assertTrue(self.hub.restoring)
        self.hub.startup_started = time.monotonic()-61
        control.receive(dict(type='status', instance='native-v2-stage', blocks=2, applied=control.sequence, routed=True, fault=0))
        self.hub.reconcile()
        self.assertFalse(self.hub.restoring)
        self.assertEqual(control.values['piano'], .4)

    def test_saved_control_acknowledgment_still_times_out_after_ten_seconds(self):
        self.hub.prepared = {'piano': .4}
        self.attached()
        self.hub.reconcile()
        self.hub.restore_started = time.monotonic()-11
        with self.assertRaisesRegex(ValueError, "acknowledgment timed out"):
            self.hub.reconcile()

    def test_running_audio_progress_is_supervised_after_restore(self):
        for mode in ('stale', 'exited', 'fault', 'route'):
            control = self.attached(); self.hub.reconcile()
            self.assertFalse(self.hub.restoring)
            self.hub.reconcile()
            if mode == 'stale': control.last_progress = 0
            if mode == 'exited': control.child.poll.return_value = 1
            if mode == 'fault': control.status['fault'] = 1
            if mode == 'route': control.status['routed'] = False
            with self.assertRaisesRegex(ValueError, 'restarting muted'): self.hub.reconcile()

    def test_auto_network_is_explicit_private_and_survives_no_wifi(self):
        def row(name, address, up=True):
            return dict(ifname=name, flags=['UP'] if up else [], addr_info=[dict(family='inet', scope='global', local=address)])
        rows = [row('wlan0', '192.168.1.203'), row('eth0', '10.42.0.1'), row('vpn0', '10.1.1.1'),
                row('wlan0', '8.8.8.8'), row('wlan0', '169.254.1.1'), row('eth0', '172.20.0.1', False)]
        self.assertEqual(stage.lifecycle.private_addresses(json.dumps(rows), ['wlan0', 'eth0']),
                         {'127.0.0.1', '192.168.1.203', '10.42.0.1'})
        self.assertEqual(stage.lifecycle.private_addresses('[]', ['wlan0']), {'127.0.0.1'})
        for text in ('{}', 'broken', ' '*65537):
            with self.assertRaises(ValueError): stage.lifecycle.private_addresses(text, ['wlan0'])

    def test_listener_rebind_retries_conflict_without_owning_audio(self):
        first, second = mock.Mock(), mock.Mock()
        factory = mock.Mock(side_effect=[first, OSError('occupied'), second])
        listeners = stage.lifecycle.Listeners(factory)
        listeners.reconcile({'127.0.0.1'})
        listeners.reconcile({'127.0.0.1', '192.168.1.1'})
        self.assertIn('192.168.1.1', listeners.errors)
        listeners.reconcile({'127.0.0.1', '192.168.1.1'})
        self.assertEqual(len(listeners.servers), 2)
        self.assertFalse(listeners.errors)
        listeners.reconcile({'127.0.0.1'})
        second.server_close.assert_called_once()
        first.server_close.assert_not_called()
        listeners.close(); first.server_close.assert_called_once()

    def test_notify_uses_real_local_datagram_without_audio(self):
        # Short /tmp path also fits macOS sockaddr_un's smaller path limit.
        with tempfile.TemporaryDirectory(dir='/tmp') as short:
            path = short + '/notify'
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
                receiver.bind(path); receiver.settimeout(1)
                with mock.patch.dict(os.environ, {'NOTIFY_SOCKET': path}):
                    stage.lifecycle.notify('READY=1\nWATCHDOG=1')
                self.assertEqual(receiver.recv(100), b'READY=1\nWATCHDOG=1')
        with mock.patch.dict(os.environ, {'NOTIFY_SOCKET': path}):
            stage.lifecycle.notify('STOPPING=1')  # missing manager is bounded

    def test_boot_unit_has_watchdog_no_legacy_auto_start_and_fixed_audio(self):
        # Verify both the historical Pi4 unit and the unit the public installer copies.
        units = [(ROOT / "systemd" / name).read_text() for name in
                 ("stave-native.service", "stave-pi4-share.service")]
        self.assertIn("systemd/stave-pi4-share.service", (ROOT / "install-pi4.sh").read_text())
        for required in ('Type=notify', 'WatchdogSec=30', 'WatchdogSignal=SIGKILL', 'LimitCORE=0', 'KillMode=control-group', 'Restart=always',
                         'WantedBy=default.target', '--listen auto', '--port 8082', '--state-dir %h/.local/share/stave-native'):
            for unit in units:
                self.assertIn(required, unit)
        for unit in units:
            self.assertNotIn('ExecStopPost=', unit)
            self.assertNotIn('256', unit)
        self.assertIn("--hostname %l.local", units[1])
        self.assertIn("WorkingDirectory=%h/.local/opt/stave-pi4/current", units[1])
        self.assertNotIn("--midi-source", units[1])

    def test_distributed_unit_command_reads_fresh_layout_and_announces_ready(self):
        root = Path(self.temp.name)
        parsed = configparser.ConfigParser(interpolation=None)
        parsed.read(ROOT / "systemd/stave-pi4-share.service")
        service = parsed["Service"]
        workdir = Path(service["WorkingDirectory"].replace("%h", str(root)))
        workdir.mkdir(parents=True)
        for rel in ("tools/native_v2_stage.py", "bin/audition-jack"):
            path = workdir / rel
            path.parent.mkdir(exist_ok=True)
            path.touch()
        font = root / "FluidR3_GM.sf2"
        font.touch()
        argv = shlex.split(service["ExecStart"].replace("%h", str(root)).replace("%l", "test-instrument"))
        self.assertEqual(argv[:3], ["/usr/bin/python3", "-B", "tools/native_v2_stage.py"])
        self.assertIn("test-instrument.local", argv)
        self.assertIn("/usr/share/sounds/sf2/FluidR3_GM.sf2", argv)
        argv[argv.index("--soundfont") + 1] = str(font)
        def ready(message):
            if message.startswith("READY=1"):
                raise RuntimeError("fixture observed READY")
        previous = Path.cwd()
        try:
            os.chdir(workdir)
            with mock.patch("sys.argv", argv[2:]), mock.patch.object(stage, "BoundedHTTPServer"), \
                    mock.patch.object(stage.lifecycle, "notify", side_effect=ready), \
                    mock.patch.object(stage.subprocess, "Popen") as child:
                with self.assertRaisesRegex(RuntimeError, "fixture observed READY"):
                    stage.main()
                child.assert_not_called()
        finally:
            os.chdir(previous)
        state = root / ".local/share/stave-native"
        self.assertTrue((state / "recordings").is_dir())
        self.assertTrue((state / "pad_samples").is_dir())
        self.assertFalse((state / "native_routes.json").exists())

    def test_multiple_listeners_share_global_http_worker_budget(self):
        slots = threading.BoundedSemaphore(4)
        with stage.BoundedHTTPServer(('127.0.0.1', 0), stage.audition.BaseHTTPRequestHandler, slots=slots) as first, \
             stage.BoundedHTTPServer(('127.0.0.1', 0), stage.audition.BaseHTTPRequestHandler, slots=slots) as second:
            for _ in range(4): self.assertTrue(first.slots.acquire(False))
            self.assertFalse(second.slots.acquire(False))

    def test_routes_require_exact_available_type_direction_and_persist(self):
        self.assertEqual(stage.validate_routes(ROUTES, self.hub.ports), ROUTES)
        for routes in ({}, {**ROUTES, "audio_right": ROUTES["audio_left"]},
                       {**ROUTES, "midi_source": "StaveSynth:midi"},
                       {**ROUTES, "audio_left": "missing"}, {**ROUTES, "midi_source": ROUTES["audio_left"]}):
            with self.assertRaises(ValueError): self.hub.dispatch("/routes", routes, self.hub.epoch)
        epoch = self.hub.epoch
        self.hub.dispatch("/routes", ROUTES, epoch)
        self.assertFalse(self.hub.take_restart())
        self.assertEqual(self.hub.epoch, epoch)
        self.hub.inventory({})
        self.assertEqual(self.hub.snapshot()["runtime"]["missing_routes"], ROUTES)
        with self.assertRaisesRegex(ValueError, "Keyboard:midi"):
            stage.validate_routes(ROUTES, {})
        with self.assertRaises(ValueError): stage.parse_ports("x"*262145)

    def test_fresh_install_requires_explicit_routes_and_keeps_them_after_restart(self):
        hub = stage.CandidateHub(self.store, None)
        hub.inventory(stage.parse_ports(INVENTORY))
        status = hub.snapshot()
        self.assertTrue(status["runtime"]["preparation"])
        self.assertEqual(status["values"]["master"], 0)
        self.assertEqual(hub.routes, {})
        self.assertIn(ROUTES["midi_source"], status["runtime"]["devices"]["midi"])
        self.assertFalse(hub.take_restart())
        hub.dispatch("/routes", ROUTES, hub.epoch)
        self.assertTrue(hub.take_restart())
        self.assertEqual(stage.CandidateHub(self.store, None).routes, ROUTES)
        self.store.path.write_text("damaged")
        recovered = stage.CandidateHub(self.store, None)
        self.assertEqual(recovered.routes, ROUTES)
        self.assertTrue(recovered.state_warning)
        self.assertEqual(recovered.snapshot()["values"]["master"], 0)

    @staticmethod
    def report_master(control, blocks, master):
        keys = list(stage.audition.CONTROLS)
        index = keys.index("master")
        values = [stage.audition.CONTROLS[key][3] for key in keys]
        values[index] = master
        raw, serial = [-1] * len(keys), [0] * len(keys)
        raw[index], serial[index] = round(master * 127), blocks
        control.receive(dict(type="status", instance="native-v2-stage", blocks=blocks, applied=0,
                             routed=True, fault=0, control_values=values, linked_offset=0,
                             midi_mapped_raw=raw, midi_mapped_serial=serial))

    def test_live_saves_capture_fader_move_between_periodic_snapshots(self):
        control = self.attached()
        self.hub.reconcile()
        for index, (path, data) in enumerate((("/save", {}), ("/preset-save", {"slot": 2, "name": "After fade"}))):
            blocks = 2 + index * 2
            self.report_master(control, blocks, .2)
            epoch = self.hub.epoch
            waiting, result, errors = threading.Event(), [], []
            wait_for_snapshot = control.wait_for_save_snapshot
            def wait(baseline):
                self.assertEqual(baseline, blocks)
                waiting.set()
                wait_for_snapshot(baseline)
            def save():
                try:
                    result.append(self.hub.dispatch(path, data, epoch))
                except Exception as error:
                    errors.append(error)
            with mock.patch.object(control, "wait_for_save_snapshot", side_effect=wait):
                worker = threading.Thread(target=save)
                worker.start()
                try:
                    self.assertTrue(waiting.wait(1))
                    self.assertTrue(self.hub.lock.acquire(timeout=.2), "Save blocked concurrent stage requests")
                    self.hub.lock.release()
                    self.assertTrue(worker.is_alive())
                    # The physical move is already applied; its next telemetry arrives after Save.
                    self.report_master(control, blocks + 1, .87)
                finally:
                    worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(result)
            stored = self.store.load() if path == "/save" else self.hub.presets.controls_for(2)
            self.assertEqual(stored["master"], .87)

    def test_live_save_aborts_if_child_replaced_during_snapshot_wait(self):
        control = self.attached()
        self.hub.reconcile()
        self.report_master(control, 2, .2)
        def replace(_baseline):
            self.hub.detached("Device changed")
            self.report_master(control, 3, .87)
        with mock.patch.object(control, "wait_for_save_snapshot", side_effect=replace):
            with self.assertRaisesRegex(ValueError, "session changed"):
                self.hub.dispatch("/preset-save", {"slot": 0, "name": "Old session"}, self.hub.epoch)
        self.assertIsNone(self.hub.presets.summary()[0])

    def test_snapshot_wait_rejects_timeout_stalled_report_pending_or_lost_child(self):
        control = self.attached()
        self.report_master(control, 2, .2)
        self.report_master(control, 2, .87)
        with self.assertRaisesRegex(ValueError, "in time"):
            control.wait_for_save_snapshot(2, timeout=.001)
        control.submit({"key": "master", "value": .6})
        with self.assertRaisesRegex(ValueError, "acknowledged"):
            control.wait_for_save_snapshot(2, timeout=.001)
        control.pending.clear()
        control.child.poll.return_value = 1
        with self.assertRaisesRegex(ValueError, "acknowledged"):
            control.wait_for_save_snapshot(2, timeout=.001)
        self.assertFalse(self.store.path.exists())

    def test_save_refuses_pending_stale_unrouted_or_exited(self):
        c = self.attached(); self.hub.reconcile()
        for mode in ("pending", "stale", "unrouted", "exited"):
            c = self.attached(); self.hub.reconcile()
            if mode == "pending": c.submit(dict(key="piano", value=.9))
            if mode == "stale": c.last_status = 0
            if mode == "unrouted": c.status["routed"] = False
            if mode == "exited": c.child.poll.return_value = 1
            with self.assertRaises(ValueError): self.hub.dispatch("/save", {}, self.hub.epoch)
        self.assertFalse(self.store.path.exists())

    def test_hostname_same_origin_and_session_guards_on_real_loopback(self):
        for name in ("*.local", "bad.local:80", "public.example", "-bad.local", "a..local"):
            with self.assertRaises(ValueError): stage.audition.authority_set("127.0.0.1", 8082, [name])
        server = stage.BoundedHTTPServer(("127.0.0.1", 0), stage.audition.BaseHTTPRequestHandler)
        port = server.server_port
        numeric, named = f"127.0.0.1:{port}", f"stavepi4.local:{port}"
        server.RequestHandlerClass = stage.audition.handler_for(self.hub, stage.audition.authority_set("127.0.0.1", port, ["stavepi4.local"]))
        thread = threading.Thread(target=server.serve_forever); thread.start()
        try:
            for host, origin, epoch, expected in ((named, named, self.hub.epoch, 202),
                    (named, numeric, self.hub.epoch, 403), ("evil.local", "evil.local", self.hub.epoch, 403),
                    (numeric, numeric, "stale", 400)):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                connection.request("POST", "/restart-audio", "{}", {"Host": host, "Origin": "http://"+origin,
                    "Content-Type": "application/json", "X-Stave-Epoch": epoch})
                response = connection.getresponse(); self.assertEqual(response.status, expected)
                response.read(); connection.close()
        finally:
            server.shutdown(); thread.join(2); server.server_close()

    def test_thread_slots_are_bounded_and_released_on_spawn_failure(self):
        with stage.BoundedHTTPServer(("127.0.0.1", 0), stage.audition.BaseHTTPRequestHandler) as server:
            with mock.patch.object(stage.socketserver.ThreadingMixIn, "process_request", side_effect=RuntimeError):
                with self.assertRaises(RuntimeError): server.process_request(mock.Mock(), ("127.0.0.1", 1))
            self.assertTrue(all(server.slots.acquire(False) for _ in range(4)))
            request = mock.Mock()
            with mock.patch.object(server, "shutdown_request") as close:
                server.process_request(request, ("127.0.0.1", 1)); close.assert_called_once_with(request)


if __name__ == "__main__":
    unittest.main()
