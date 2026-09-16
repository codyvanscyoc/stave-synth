"""Device-free scope guards for the player-approved Yamaha-only output policy."""
import json
import unittest
from pathlib import Path


PROFILE = Path(__file__).resolve().parents[1] / "config/pi4/51-stave-yamaha-mx-latency.conf"


class YamahaLatencyProfileTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(PROFILE.read_text())

    def test_rule_matches_only_the_observed_yamaha_output(self):
        self.assertEqual(set(self.config), {"monitor.alsa.rules"})
        rules = self.config["monitor.alsa.rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["matches"], [{
            "node.name": "alsa_output.usb-Yamaha_Corporation_Yamaha_MX_Series-00.analog-stereo",
            "media.class": "Audio/Sink",
            "alsa.components": "USB0499:1711",
        }])

    def test_only_period_size_is_changed(self):
        self.assertEqual(self.config["monitor.alsa.rules"][0]["actions"], {
            "update-props": {"api.alsa.period-size": 128},
        })

    def test_no_runtime_id_or_global_graph_setting(self):
        rule = self.config["monitor.alsa.rules"][0]
        self.assertEqual(set(rule), {"matches", "actions"})
        self.assertNotIn("object.id", rule["matches"][0])
        self.assertNotIn("alsa.card", rule["matches"][0])
        self.assertNotIn("context.properties", self.config)


if __name__ == "__main__":
    unittest.main()
