"""Only isolated control/HTTP contracts: fake child, ephemeral loopback listener.

Never starts JACK, loads SF2, imports runtime or contacts the Pi.
"""
import http.client
import importlib.util
import json
from pathlib import Path
import subprocess
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("native_v2_audition", ROOT / "tools/native_v2_audition.py")
audition = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audition)


class AuditionControlTests(unittest.TestCase):
    def controller(self):
        child = mock.Mock()
        child.poll.return_value = None
        c = audition.Controller(child)
        c.receive({"type": "status", "instance": "native-v2-audition", "fault": 0, "applied": 0, "routed": True, "blocks": 1})
        return c

    def test_untrusted_controls_rejected_before_queue(self):
        c = self.controller()
        for data in (None, [], {}, {"key": "piano", "value": True}, {"key": [], "value": 0},
                     {"key": "master", "value": float("nan")}, {"key": "master", "value": float("inf")},
                     {"key": "master", "value": 10**400}, {"key": "wave1", "value": 1.5},
                     {"key": "master", "value": .5, "extra": 1}, {"key": "drone", "value": 1}):
            with self.subTest(data=str(data)[:80]), self.assertRaises(ValueError):
                c.submit(data)
        self.assertEqual(c.sequence, 0)
        self.assertTrue(c.outgoing.empty())

    def test_pending_is_not_applied_and_rejection_not_replayed(self):
        c = self.controller()
        self.assertEqual(c.submit({"key": "master", "value": .5}), 1)
        self.assertEqual(c.snapshot()["values"]["master"], 0)
        c.receive({"type": "accepted", "id": 1, "ok": True})
        self.assertEqual(c.snapshot()["pending"], 1)
        c.receive({"type": "status", "instance": "native-v2-audition", "fault": 0, "applied": 1, "routed": True})
        self.assertEqual(c.snapshot()["values"]["master"], .5)
        c.submit({"key": "master", "value": .8})
        c.receive({"type": "accepted", "id": 2, "ok": False})
        c.receive({"type": "status", "instance": "native-v2-audition", "fault": 0, "applied": 3})
        self.assertEqual(c.snapshot()["values"]["master"], .5)
        self.assertEqual(c.snapshot()["pending"], 0)

    def test_stale_failed_and_exited_owners_refuse_controls(self):
        for mode in ("stale", "fault", "exited", "stalled", "unrouted"):
            c = self.controller()
            if mode == "stale": c.last_status = time.monotonic() - 4
            if mode == "fault": c.status["fault"] = 2
            if mode == "exited": c.child.poll.return_value = 1
            if mode == "stalled": c.last_progress = time.monotonic() - 4
            if mode == "unrouted": c.status["routed"] = False
            with self.assertRaises(ValueError): c.submit({"key": "piano", "value": .4})

    def test_bounded_backlog(self):
        c = self.controller()
        for _ in range(64): c.submit({"key": "osc1", "value": .2})
        with self.assertRaises(ValueError): c.submit({"key": "osc1", "value": .3})
        self.assertEqual(len(c.pending), 64)
        self.assertEqual(c.outgoing.qsize(), 64)

    def test_same_origin_and_host_guards(self):
        c = self.controller()
        server = audition.HTTPServer(("127.0.0.1", 0), audition.BaseHTTPRequestHandler)
        authority = f"127.0.0.1:{server.server_port}"
        server.RequestHandlerClass = audition.handler_for(c, authority)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            for origin, host, expected in (("http://"+authority, authority, 202), ("https://external.invalid", authority, 403),
                                           ("http://external.invalid", "external.invalid", 403)):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                connection.request("POST", "/control", json.dumps({"key": "master", "value": .2}),
                                   {"Host": host, "Origin": origin, "Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                response.read(); connection.close()
        finally:
            server.shutdown(); thread.join(timeout=2); server.server_close()
        self.assertEqual(c.sequence, 1)

    def test_frontend_syntax_and_explicit_limitations(self):
        html = (ROOT / "native_v2/audition.html").read_text()
        javascript = html.split("<script>", 1)[1].split("</script>", 1)[0]
        subprocess.run(["node", "--check", "-"], input=javascript, text=True, capture_output=True, check=True)
        self.assertIn("not the normal Stave", html)
        self.assertIn("block-quantized", html)
        self.assertIn("pending.clear()", html)

    def test_native_control_names_match_and_no_service_mutation(self):
        header = (ROOT / "native_v2/include/stave/audition_session.hpp").read_text()
        for name in audition.CONTROLS:
            self.assertIn('"'+name+'"', header)
        source = (ROOT / "tools/native_v2_audition.py").read_text()
        for forbidden in ("systemctl", "pw-metadata", "current_state.json", "shell=True"):
            self.assertNotIn(forbidden, source)
