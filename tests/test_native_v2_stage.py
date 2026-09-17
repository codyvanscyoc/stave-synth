"""Device-free native candidate contracts. Fake child, temporary state, loopback only."""
import http.client
import importlib.util
import json
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
        self.store.save(dict(piano=.7, piano_tone=.4))
        self.assertEqual(self.store.load(), dict(piano=.7, piano_tone=.4))
        before = self.store.path.read_bytes()
        for key in stage.storage.TRANSIENT:
            with self.assertRaises(ValueError): self.store.save({key: 0})
        for value in (True, float("nan"), float("inf"), -1, 3, "0.5"):
            with self.assertRaises(ValueError): self.store.save(dict(piano=value))
        self.assertEqual(self.store.path.read_bytes(), before)
        with mock.patch.object(stage.storage.atomic, "atomic_write_json", side_effect=OSError("full disk")):
            with self.assertRaises(OSError): self.store.save(dict(piano=.2))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_malformed_oversized_and_symlink_state_never_loaded(self):
        for data in (b"broken", b" "*16385, b'[]', b'{"schema":true,"controls":{}}',
                     b'{"schema":1,"controls":{"master":1}}'):
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
        self.hub.saved = dict(piano=.8, bed_level=.2)
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

    def test_startup_timeout_includes_missing_telemetry(self):
        child = mock.Mock(); child.poll.return_value = None
        self.hub.attach(child)
        self.hub.restore_started = time.monotonic()-11
        with self.assertRaisesRegex(ValueError, "timed out"): self.hub.reconcile()

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
