"""Device-free native candidate contracts. Fake child, temporary state, loopback only."""
import http.client
import importlib.util
import json
import os
import socket
from pathlib import Path
import tempfile
import threading
import time
import unittest
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

    def test_midi_map_is_unique_persistent_and_restored_before_controls(self):
        control = self.attached(); self.hub.reconcile()
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
        take = recordings/'take-test.wav'; take.write_bytes(b'R'*58)
        old = pads/'pad_C.wav'; old.write_bytes(b'O'*58)
        child = mock.Mock(); child.poll.return_value = None
        control = self.hub.attach(child, take)
        control.receive(dict(type='status', instance='native-v2-stage', blocks=1, applied=0,
                             routed=True, fault=0, writer_state=2, capture_end=2, writer_frames=512))
        self.hub.reconcile(); epoch = self.hub.epoch
        result = self.hub.dispatch('/record-assign', {'slot': 0}, epoch)
        self.assertEqual(old.read_bytes(), b'R'*58)
        undo = list(pads.glob('undo-pad_C-*.wav'))
        self.assertEqual(len(undo), 1); self.assertEqual(undo[0].read_bytes(), b'O'*58)
        self.assertTrue(self.hub.restart); self.assertTrue(self.hub.restoring)
        self.assertIn('restarting muted', result['message'])
        self.assertNotEqual(epoch, self.hub.epoch)

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
        self.hub.restore_started = time.monotonic()-11
        with self.assertRaisesRegex(ValueError, "timed out"): self.hub.reconcile()

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
        unit = (ROOT/'systemd/stave-native.service').read_text()
        for required in ('Type=notify', 'WatchdogSec=30', 'WatchdogSignal=SIGKILL', 'LimitCORE=0', 'KillMode=control-group', 'Restart=always',
                         'WantedBy=default.target', '--listen auto', '--port 8082', '--state-dir %h/.local/share/stave-native'):
            self.assertIn(required, unit)
        self.assertNotIn('ExecStopPost=', unit)
        self.assertNotIn('256', unit)

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
        self.hub.dispatch("/routes", ROUTES, self.hub.epoch)
        self.assertTrue(self.hub.take_restart())
        self.assertEqual(stage.CandidateHub(self.store, ROUTES).routes, ROUTES)
        with self.assertRaises(ValueError): stage.parse_ports("x"*262145)

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
