"""Real original SamplePlayer oracle, extracted without runtime/device imports.

The native probe is standalone: no Faust/FluidSynth/JACK, WAV or network. These
tests qualify a component, NOT integration/recording/UI or Pi4 performance.
"""
import ast
import logging
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np
from scipy.signal import lfilter

ROOT = Path(__file__).resolve().parents[1]


def original_player():
    path = ROOT / "stave_synth/synth_engine.py"
    tree = ast.parse(path.read_text())
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name in {"BiquadLowpass", "SamplePlayer"}]
    def process_filter(flt, samples):
        out, flt.zi = lfilter(flt.b, flt.a, samples, zi=flt.zi)
        return out
    namespace = {"np": np, "SAMPLE_RATE": 48000, "TWO_PI": 2*np.pi,
                 "_biquad_run": process_filter, "logger": logging.getLogger(__name__)}
    exec(compile(ast.Module(body=classes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["SamplePlayer"]


class NativeSampledBedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="stave-bed-offline-")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.probe = Path(cls.tmp.name) / "probe"
        subprocess.run(["c++", "-std=c++17", "-O2", "-ffp-contract=off", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=undefined", "-fno-sanitize-recover=all", "-I", str(ROOT / "native_v2/include"),
                        str(ROOT / "native_v2/tests/sampled_bed_probe.cpp"), "-o", str(cls.probe)],
                       check=True, capture_output=True, text=True, timeout=60)

    def test_original_loop_envelopes_key_changes_and_rise_match(self):
        player_type = original_player()
        # Predeclared max absolute1e-10, not a tolerance selected after failure.
        # Includes many loops/block for4/37-frame recordings;500ms overlap for
        # the long fixture, release during rise, retrigger and explicit STOP.
        for frames in (256, 512):
            for length in (4, 37, 100000):
                for rise in (False, True):
                    with self.subTest(frames=frames, length=length, rise=rise):
                        result = subprocess.run([str(self.probe), str(frames), str(length), str(int(rise))],
                                                check=True, capture_output=True, timeout=30)
                        actual = np.frombuffer(result.stdout, dtype=np.float64).reshape(850, frames, 2)
                        self.assertIn(b"PASS:", result.stderr)
                        left = (np.arange(length) % 97 - 48) / 512.
                        right = (np.arange(length) % 71 - 35) / 512.
                        voices = [player_type(), player_type()]
                        for voice, l, r in ((voices[0], left, right), (voices[1], right, left)):
                            voice.samples_l, voice.samples_r = l, r
                            voice.length = length
                            voice.xfade_len = max(1, min(24000, length // 4))
                            voice.loaded = True
                        maximum = 0
                        for block in range(850):
                            event = {0: (0, 1.2 if rise else 0, 3000),
                                     160: (1, 2 if rise else 0, 4800),
                                     350: (0, .8 if rise else 0, 3000),
                                     450: (0, 0, 3000), 675: (1, 0, 3000)}.get(block)
                            if event:
                                slot, seconds, hz = event
                                for i, voice in enumerate(voices):
                                    if i != slot and voice.active: voice.release()
                                voices[slot].trigger(seconds, hz)
                            if block in (280, 550):
                                for voice in voices: voice.release()
                            if block == 650:
                                for voice in voices: voice.hard_stop()
                            l, r = np.zeros(frames), np.zeros(frames)
                            for voice in voices: voice.process(frames, l, r)
                            expected = np.column_stack((l, r))
                            self.assertTrue(np.isfinite(actual[block]).all())
                            maximum = max(maximum, float(np.max(np.abs(expected - actual[block]))))
                        self.assertLessEqual(maximum, 1e-10)


if __name__ == "__main__":
    unittest.main()
