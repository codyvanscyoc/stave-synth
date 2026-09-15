"""STOP control regressions without app, audio-device, or native imports.

Native checks execute the real wrapper with a fake Faust library. Fallback
checks execute the real organ classes with identity filter stubs, isolating
Leslie target/ramp behavior. These are not target-native acoustic evidence.
"""

import ast
import math
from pathlib import Path
import re
import threading
import unittest

import numpy as np

from tests.test_native_controls import make_organ


ROOT = Path(__file__).resolve().parents[1]


class IdentityFilter:
    def __init__(self, *_args):
        pass

    def set_params(self, *_args):
        pass

    def process(self, samples):
        return samples


def make_fallback(*, legacy_target=False):
    """Extract real classes, excluding dependency/device module imports."""
    path = ROOT / "stave_synth/organ_engine.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    selected = [node for node in tree.body
                if isinstance(node, (ast.Assign, ast.FunctionDef, ast.ClassDef))]
    if legacy_target:
        replacements = 0
        for node in ast.walk(ast.Module(body=selected, type_ignores=[])):
            if (isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "target_hz"
                            for target in node.targets)):
                node.value = ast.parse(
                    'LESLIE_FAST_HZ if self.leslie_speed == "fast" else LESLIE_SLOW_HZ',
                    mode="eval").body
                replacements += 1
        if replacements != 1:
            raise AssertionError("legacy comparison must replace exactly one target selection")
    namespace = {"np": np, "threading": threading, "SAMPLE_RATE": 48000,
                 "lfilter": lambda _b, _a, samples: samples}
    for name in ("OnePole6dBLowpass", "OnePole6dBHighpass", "BiquadLowpass",
                 "BiquadHighpass", "BiquadPeakingEQ"):
        namespace[name] = IdentityFilter
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)
    organ = namespace["OrganEngine"]()
    organ.enabled = True
    organ.click_enabled = False
    return organ


class OrganLeslieStopTests(unittest.TestCase):
    def test_native_stop_slow_fast_targets_preserve_depth_voice_and_output(self):
        organ, library = make_organ()
        organ.note_on(60, 0.75)
        voice = organ.voices[60]
        slot = voice.slot

        def compute(*_args):
            organ._out_l.fill(0.25)
            organ._out_r.fill(-0.125)

        library.computeStaveOrgan = compute
        before_phase = organ._phase_zones[slot][0]
        before_volume = organ.volume
        for speed, target in (("stop", 0.0), ("slow", 0.8), ("fast", 6.5), ("stop", 0.0)):
            with self.subTest(speed=speed):
                organ.update_params({"leslie_speed": speed})
                output = organ.render_block(512)
                self.assertEqual(organ.leslie_speed, speed)
                self.assertEqual(organ._zones["leslie_target_hz"][0], target)
                self.assertEqual(organ._zones["leslie_depth"][0], 0.3)
                self.assertEqual(organ.volume, before_volume)
                self.assertEqual(organ._phase_zones[slot][0], before_phase)
                self.assertIs(organ.voices[60], voice)
                self.assertFalse(voice.releasing)
                self.assertEqual(organ._gate_zones[slot][0], 0.75)
                np.testing.assert_array_equal(output[0], 0.25)
                np.testing.assert_array_equal(output[1], -0.125)
                self.assertEqual(library.clear_count, 0)

    def test_fallback_stop_coasts_existing_rotors_without_resetting_or_muting(self):
        organ = make_fallback()
        organ.note_on(60, 0.75)
        voice = organ.voices[60]
        organ._horn_current_hz = 6.5
        organ._drum_current_hz = 6.5 * 0.85
        organ._horn_phase = 1.1
        organ._drum_phase = 2.2
        organ.update_params({"leslie_speed": "stop"})
        self.assertEqual(organ._horn_current_hz, 6.5)  # Control itself never snaps the rotor.
        self.assertEqual(organ._horn_phase, 1.1)
        output = organ.render_block(512)
        self.assertAlmostEqual(organ._horn_current_hz, 6.5 * math.exp(-512 / (48000 * 0.8)), places=14)
        self.assertAlmostEqual(organ._drum_current_hz, 6.5 * 0.85 * math.exp(-512 / (48000 * 3.0)), places=14)
        self.assertNotEqual(organ._horn_phase, 1.1)
        self.assertNotEqual(organ._drum_phase, 2.2)
        for _ in range(32):
            previous_horn, previous_drum = organ._horn_current_hz, organ._drum_current_hz
            organ.render_block(512)
            self.assertLess(organ._horn_current_hz, previous_horn)
            self.assertLess(organ._drum_current_hz, previous_drum)
            self.assertGreaterEqual(organ._horn_current_hz, 0.0)
            self.assertGreaterEqual(organ._drum_current_hz, 0.0)
        self.assertEqual(organ.leslie_depth, 0.3)
        self.assertEqual(organ.volume, 0.5)
        self.assertIs(organ.voices[60], voice)
        self.assertFalse(voice.releasing)
        self.assertTrue(np.isfinite(output).all())
        self.assertGreater(float(np.max(np.abs(output))), 0.0)

    def test_fallback_stop_then_slow_then_fast_uses_existing_ramp_constants(self):
        organ = make_fallback()
        for speed, target in (("stop", 0.0), ("slow", 0.8), ("fast", 6.5)):
            before_horn, before_drum = organ._horn_current_hz, organ._drum_current_hz
            organ.update_params({"leslie_speed": speed})
            organ.render_block(512)
            horn_alpha = 1.0 - np.exp(-512 / (48000 * 0.8))
            drum_alpha = 1.0 - np.exp(-512 / (48000 * 3.0))
            self.assertEqual(organ._horn_current_hz, before_horn + horn_alpha * (target - before_horn))
            self.assertEqual(organ._drum_current_hz, before_drum + drum_alpha * (target * 0.85 - before_drum))

    def test_slow_fast_fallback_samples_equal_legacy_target_selection(self):
        current, legacy = make_fallback(), make_fallback(legacy_target=True)
        for organ in (current, legacy):
            organ.update_params({"volume": 0.7, "leslie_depth": 0.6, "preset": "gospel"})
            for note in (48, 60, 64):
                organ.note_on(note, 0.6)
        for speed in ("slow", "fast", "slow", "fast"):
            for organ in (current, legacy):
                organ.update_params({"leslie_speed": speed})
            for frames in (64, 512, 1024):
                with self.subTest(speed=speed, frames=frames):
                    np.testing.assert_array_equal(current.render_block(frames), legacy.render_block(frames))
                    self.assertEqual(current._horn_phase, legacy._horn_phase)
                    self.assertEqual(current._drum_phase, legacy._drum_phase)

    def test_bulk_scene_params_and_program_changes_preserve_stop_for_both_paths(self):
        for organ in (make_fallback(), make_organ()[0]):
            with self.subTest(engine=type(organ).__name__):
                params = {"enabled": True, "volume": 0.67, "preset": "jazz",
                          "drawbars": [8, 0, 8, 0, 0, 0, 0, 0, 0],
                          "leslie_speed": "stop", "leslie_depth": 0.48,
                          "drive": 0.1, "attack_ms": 2, "release_ms": 25,
                          "filter_highcut_hz": 7200, "filter_lowcut_hz": 45,
                          "tone_tilt": 0.5, "width": 0.6}
                organ.update_params(params)
                organ.update_params(params)  # Repeated scene ticks remain valid.
                self.assertEqual(organ.leslie_speed, "stop")
                self.assertEqual(organ.leslie_depth, 0.48)
                self.assertEqual(organ.preset, "jazz")
                organ.update_params({"preset": "full"})
                self.assertEqual(organ.leslie_speed, "stop")
                organ.update_params({**params, "leslie_speed": "slow"})
                self.assertEqual(organ.leslie_speed, "slow")
                organ.update_params({**params, "leslie_speed": "fast"})
                self.assertEqual(organ.leslie_speed, "fast")

    def test_unexpected_speed_values_leave_current_speed_unchanged(self):
        for organ in (make_fallback(), make_organ()[0]):
            for valid in ("stop", "slow", "fast"):
                organ.update_params({"leslie_speed": valid})
                for invalid in ("STOP", "halt", "", None, True, 0, [], {}):
                    with self.subTest(engine=type(organ).__name__, valid=valid, invalid=invalid):
                        organ.update_params({"leslie_speed": invalid})
                        self.assertEqual(organ.leslie_speed, valid)

    def test_native_declared_zone_range_accepts_zero_and_preserves_default(self):
        source = (ROOT / "faust/organ.dsp").read_text(encoding="utf-8")
        match = re.search(r'leslie_target_hz\s*=\s*hslider\("leslie_target_hz",\s*'
                          r'([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)\)', source)
        self.assertIsNotNone(match)
        self.assertEqual(tuple(map(float, match.groups())), (0.8, 0.0, 10.0, 0.001))
        self.assertIn("horn_hz = leslie_target_hz", source)
        self.assertIn("drum_hz = leslie_target_hz * DRUM_SPEED_RATIO", source)
        self.assertIn("HORN_RAMP_SEC    = 0.8;", source)
        self.assertIn("DRUM_RAMP_SEC    = 3.0;", source)


if __name__ == "__main__":
    unittest.main()
