import math
import unittest

from stave_synth.control_mapping import macro_commands


class MacroMappingTests(unittest.TestCase):
    def test_preserves_raw_html_range_conversion(self):
        macro = {"assignments": [
            {"section": "piano", "param": "reverb_dry_wet", "min": 0, "max": 80},
            {"section": "piano", "param": "filter_highcut_hz", "min": 0, "max": 1000},
            {"section": "synth_pad", "param": "sympathetic_level", "min": 0, "max": 1000},
        ]}
        commands = macro_commands(macro, 0.5)
        self.assertEqual(commands[0]["value"], 0.4)
        self.assertEqual(commands[1]["value"], 2000)
        self.assertTrue(math.isclose(commands[2]["value"], 0.01875))

    def test_bipolar_bool_fader_and_rejections(self):
        macro = {"bipolar": True, "assignments": [
            {"kind": "fader", "fader_id": 2, "fader_alt": 0,
             "min": 0, "center": 0.4, "max": 1},
            {"section": "master", "param": "pitch_bend_enabled",
             "is_bool": True, "min": 0, "max": 1},
            {"section": "synth_pad", "param": "reverb_type", "min": 0, "max": 1},
            {"section": "bogus", "param": "x", "min": 0, "max": 1},
        ]}
        commands = macro_commands(macro, 0.25)
        self.assertEqual(commands, [
            {"type": "fader", "id": 2, "alt": 0, "value": 0.2},
            {"type": "setting", "section": "master",
             "param": "pitch_bend_enabled", "value": False},
        ])
        self.assertEqual(macro_commands(macro, float("nan")), [])

    def test_linked_slider_mirroring_uses_twin_conversion(self):
        macro = {"assignments": [
            {"section": "synth_pad", "param": "osc1_indep_cutoff",
             "min": 0, "max": 1000},
            {"section": "synth_pad", "param": "lfo_depth",
             "min": 0, "max": 75},
        ]}
        commands = macro_commands(
            macro, 1.0,
            link_state={"osc_levels_linked": True, "lfo_link": True})
        self.assertEqual(commands, [
            {"type": "setting", "section": "synth_pad",
             "param": "osc1_indep_cutoff", "value": 20000},
            {"type": "setting", "section": "synth_pad",
             "param": "osc2_indep_cutoff", "value": 20000},
            {"type": "setting", "section": "synth_pad",
             "param": "lfo_depth", "value": 0.75},
            {"type": "setting", "section": "synth_pad",
             "param": "lfo2_depth", "value": 0.75},
        ])


if __name__ == "__main__":
    unittest.main()
