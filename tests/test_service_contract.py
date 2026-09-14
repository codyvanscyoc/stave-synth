"""Read-only checks for the stage unit; never call systemctl or start a service."""

import unittest
from pathlib import Path


UNIT = Path(__file__).resolve().parents[1] / "systemd" / "stave-synth.service"


class StageServiceContractTests(unittest.TestCase):
    def test_stage_identity_cannot_inherit_isolated_manager_overrides(self):
        lines = UNIT.read_text().splitlines()
        self.assertIn("Environment=STAVE_INSTANCE=stage", lines)
        unset = set()
        for line in lines:
            if line.startswith("UnsetEnvironment="):
                unset.update(line.split("=", 1)[1].split())
        self.assertTrue({"STAVE_INSTANCE_ROOT", "STAVE_HTTP_PORT", "STAVE_WEBSOCKET_PORT"} <= unset)

    def test_preflight_is_bounded_and_does_not_force_success_or_global_gain(self):
        commands = [line for line in UNIT.read_text().splitlines() if line.startswith("ExecStartPre=")]
        self.assertEqual(len(commands), 1)
        self.assertIn("/usr/bin/timeout 5 ", commands[0])
        self.assertIn("until pw-cli i 0", commands[0])
        self.assertNotIn("exit 0", commands[0])
        self.assertNotIn("amixer", commands[0])


if __name__ == "__main__":
    unittest.main()
