"""UI-only restart decisions with fake listeners; never binds a port."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from stave_synth.ui_recovery import UIRecovery


class UIRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.monitor = UIRecovery(clock=lambda: self.now)
        self.old = Mock()
        self.old.health_status.return_value = {"healthy": False}
        self.old.stop.return_value = True
        self.owner = SimpleNamespace(ws_server=self.old, _stopping=False)
        self.new = Mock()
        self.new.health_status.return_value = {"healthy": True}
        self.new.stop.return_value = True
        self.factory = Mock(return_value=self.new)

    def tick(self, audio=True):
        self.monitor.tick(self.owner, self.factory, audio_healthy=audio)

    def test_requires_repeated_bad_health_then_replaces_only_ui(self):
        self.tick()
        self.factory.assert_not_called()
        self.tick()
        self.old.stop.assert_called_once()
        self.new.start.assert_called_once()
        self.assertIs(self.owner.ws_server, self.new)

    def test_healthy_ui_and_unhealthy_audio_never_trigger_ui_restart(self):
        self.old.health_status.return_value = {"healthy": True}
        for _ in range(5):
            self.tick()
        self.factory.assert_not_called()
        self.old.health_status.return_value = {"healthy": False}
        for _ in range(5):
            self.tick(audio=False)
        self.factory.assert_not_called()

    def test_stuck_old_owner_blocks_duplicate_workers_permanently(self):
        self.old.stop.return_value = False
        for _ in range(8):
            self.now += 100
            self.tick()
        self.assertTrue(self.monitor.blocked)
        self.old.stop.assert_called_once()
        self.factory.assert_not_called()

    def test_stop_during_old_teardown_cannot_start_replacement(self):
        def stop():
            self.owner._stopping = True
            return True
        self.old.stop.side_effect = stop
        self.tick()
        self.tick()
        self.factory.assert_not_called()

    def test_failed_rebind_has_backoff_and_bounded_attempt_count(self):
        self.new.start.side_effect = OSError("bind failed")
        self.new.health_status.return_value = {"healthy": False}
        self.tick()
        self.tick()
        self.assertEqual(self.monitor.attempts, 1)
        for _ in range(20):
            self.tick()
        self.assertEqual(self.monitor.attempts, 1)
        for _ in range(8):
            self.now += 100
            self.tick()
        self.assertEqual(self.monitor.attempts, 3)
        self.assertTrue(self.monitor.blocked)

    def test_failed_replacement_retains_nonquiescent_owner_for_shutdown(self):
        self.new.start.side_effect = OSError("startup incomplete")
        self.new.stop.return_value = False
        self.tick()
        self.tick()
        self.assertTrue(self.monitor.blocked)
        self.assertIs(self.owner.ws_server, self.new)


if __name__ == "__main__":
    unittest.main()
