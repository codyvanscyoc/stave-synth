"""Offline text contract for the reversible stage-candidate systemd drop-in."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_PATH = ROOT / "systemd" / "pi4-stage-candidate.conf"
BASE_UNIT_PATH = ROOT / "systemd" / "stave-synth.service"


class StageCandidateUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = CANDIDATE_PATH.read_text(encoding="utf-8")
        cls.lines = [line.strip() for line in cls.text.splitlines()]

    def test_replaces_only_start_command_and_preflight(self):
        exec_start = [line for line in self.lines if line.startswith("ExecStart=")]
        self.assertEqual(exec_start, [
            "ExecStart=",
            "ExecStart=/usr/bin/pw-jack %h/stave-synth/venv/bin/python "
            "-m stave_synth.main --no-gui",
        ])
        exec_start_pre = [line for line in self.lines if line.startswith("ExecStartPre=")]
        self.assertEqual(len(exec_start_pre), 2)
        self.assertEqual(exec_start_pre[0], "ExecStartPre=")
        self.assertIn("pw-cli i 0", exec_start_pre[1])
        self.assertIn("sleep 0.3", exec_start_pre[1])
        self.assertIn("/usr/bin/timeout 5 ", exec_start_pre[1])
        self.assertNotIn("exit 0", exec_start_pre[1])
        self.assertNotIn("amixer", self.text)
        self.assertIn("WorkingDirectory=%h/stave-synth-pi4-stage", self.lines)

    def test_pins_stage_and_removes_isolated_overrides(self):
        self.assertIn("Environment=STAVE_INSTANCE=stage", self.lines)
        self.assertIn(
            "UnsetEnvironment=STAVE_INSTANCE_ROOT STAVE_HTTP_PORT STAVE_WEBSOCKET_PORT",
            self.lines,
        )
        self.assertNotIn("STAVE_INSTANCE=qa", self.text)

    def test_contains_complete_strict_pi4_native_profile(self):
        flags = (
            "REVERB", "PING_PONG", "OSC_BANK", "SYMPATHETIC", "MASTER_FX",
            "BUS_COMP", "ORGAN", "PAD_BUS", "PIANO_CHAIN", "MERGED",
        )
        for flag in flags:
            with self.subTest(flag=flag):
                self.assertEqual(
                    self.lines.count(f"Environment=STAVE_FAUST_{flag}=1"), 1
                )
        self.assertIn("Environment=STAVE_LOW_RAM=1", self.lines)
        self.assertIn("Environment=STAVE_REQUIRE_NATIVE=1", self.lines)

    def test_preserves_base_notify_watchdog_and_service_policy(self):
        # This drop-in changes candidate source/profile only. Lifecycle policy
        # remains reviewable in the tracked base unit instead of being quietly
        # forked in a one-night deployment override.
        for directive in ("Type=", "WatchdogSec=", "Restart=", "TimeoutStopSec="):
            self.assertFalse(any(line.startswith(directive) for line in self.lines))
        base = BASE_UNIT_PATH.read_text(encoding="utf-8")
        self.assertIn("Type=notify", base)
        self.assertIn("WatchdogSec=30", base)
        self.assertIn("Restart=on-failure", base)

    def test_documents_exact_reversible_install_boundary(self):
        self.assertIn("DO NOT install this blindly", self.text)
        self.assertIn("90-pi4-stage-candidate.conf", self.text)
        self.assertIn("removing only that exact installed file", self.text)
        self.assertNotIn("install.sh", "\n".join(
            line for line in self.lines if not line.startswith("#")
        ))


if __name__ == "__main__":
    unittest.main()
