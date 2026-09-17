"""Device-free refusal tests for the explicitly opt-in live MIDI probe."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "tools/probe_native_v2_audition.py"
spec = importlib.util.spec_from_file_location("native_v2_live_probe", PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class NativeLiveProbeTests(unittest.TestCase):
    def args(self, root, url="http://127.0.0.1:8082", allow=True):
        peer = root / "peer"; peer.touch()
        return [str(PATH), "--url", url, "--midi-probe", str(peer), "--output-dir", str(root / "evidence")] + (["--allow-muted-live-test"] if allow else [])

    def test_wrong_endpoint_and_missing_opt_in_refused_before_contact(self):
        for url, allow in (("http://127.0.0.1:8080", True), ("http://127.0.0.1:8082", False),
                           ("http://8.8.8.8:8082", True), ("http://example.invalid:8082", True)):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with mock.patch("sys.argv", self.args(root, url, allow)), mock.patch.object(probe.urllib.request, "build_opener") as network, mock.patch.object(probe.subprocess, "Popen") as launch:
                    with self.assertRaises(SystemExit): probe.main()
                    network.assert_not_called(); launch.assert_not_called()

    def test_existing_evidence_refused_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "evidence").mkdir()
            with mock.patch("sys.argv", self.args(root)), mock.patch.object(probe.urllib.request, "build_opener") as network:
                with self.assertRaises(FileExistsError): probe.main()
                network.assert_not_called()

    def test_requested_piano_tone_requires_candidate_support_before_midi(self):
        status = {"instance": "native-v2-audition", "stale": False, "exited": None,
                  "status": {"fault": 0, "frames": 512, "routed": True}, "values": {"master": 0}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); opener = mock.Mock()
            opener.open.return_value = io.BytesIO(json.dumps(status).encode())
            with mock.patch("sys.argv", self.args(root)+["--piano-tone-sweep"]), \
                 mock.patch.object(probe.urllib.request, "build_opener", return_value=opener), \
                 mock.patch.object(probe.subprocess, "Popen") as launch:
                self.assertEqual(probe.main(), 1); launch.assert_not_called()
            report = json.loads((root / "evidence/report.json").read_text())
            self.assertIn("not supported", report["error"])

    def test_unmuted_wrong_instance_and_faulted_owner_never_launch_peer(self):
        for variation in ("master", "instance", "fault", "stale", "frames"):
            status = {"instance": "native-v2-audition", "stale": False, "exited": None,
                      "status": {"fault": 0, "frames": 512, "routed": True}, "values": {"master": 0}}
            if variation == "master": status["values"]["master"] = .5
            elif variation == "instance": status["instance"] = "stage"
            elif variation == "stale": status["stale"] = True
            else: status["status"][variation] = 1
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); opener = mock.Mock()
                opener.open.return_value = io.BytesIO(json.dumps(status).encode())
                with mock.patch("sys.argv", self.args(root)), mock.patch.object(probe.urllib.request, "build_opener", return_value=opener), mock.patch.object(probe.subprocess, "Popen") as launch:
                    self.assertEqual(probe.main(), 1); launch.assert_not_called()
                self.assertEqual(json.loads((root / "evidence/report.json").read_text())["status"], "failed")
