"""Real WAV -> private bank -> C++ loader/playback, device-free fixtures only."""
import importlib.util
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from test_pad_preparation import wav_bytes, audio_namespace
from test_native_sampled_bed import original_player

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("native_bed_assets", ROOT / "native_v2/bed_assets.py")
assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assets)


class NativeBedAssetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="stave-bank-guards-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.probe = Path(cls.temporary.name) / "probe"
        subprocess.run(["c++", "-std=c++17", "-O2", "-ffp-contract=off", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=undefined", "-fno-sanitize-recover=all", "-I", str(ROOT / "native_v2/include"),
                        str(ROOT / "native_v2/tests/bed_assets_probe.cpp"), "-o", str(cls.probe)],
                       capture_output=True, text=True, check=True, timeout=60)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="stave-bank-fixture-")
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.library = self.directory / "library"
        self.library.mkdir()
        self.bundle = self.directory / "bank"

    def native(self, path=None, slot=None):
        return subprocess.run([str(self.probe), str(path or self.bundle), *([] if slot is None else [str(slot)])],
                              capture_output=True, timeout=15)

    def test_decode_exact_original_formats_and_real_resampling(self):
        original = audio_namespace()["SamplePlayer"]
        for bits, tag in ((8, 1), (16, 1), (24, 1), (32, 1), (32, 3), (64, 3)):
            for channels in (1, 2):
                for rate in (48000, 44100):
                    with self.subTest(bits=bits, tag=tag, channels=channels, rate=rate):
                        data = np.tile([0, 127, 128, 255] if bits == 8 else [-1, 0, 1, 2], 100)
                        path = self.library / "pad_C.wav"
                        path.write_bytes(wav_bytes(data, bits=bits, tag=tag, channels=channels, rate=rate, extensible=True))
                        a = original.prepare(path)
                        l, r = assets.decode_wave(path)
                        np.testing.assert_array_equal(l, a.samples_l)
                        np.testing.assert_array_equal(r, a.samples_r)
                        self.assertFalse(l.flags.writeable)

    def test_real_loop_playback_and_key_mask_match_original(self):
        path = self.library / "pad_G.wav"
        values = (np.arange(10000) % 71) * 137 - 4000
        path.write_bytes(wav_bytes(values, bits=16, channels=2, rate=44100))
        original_bytes = path.read_bytes()
        report = assets.prepare_bank(self.library, self.bundle)
        original = original_player().prepare(path)
        self.assertEqual(report["slots"][0]["slot"], 7)
        self.assertEqual(self.native().stdout.decode().strip(), f"128 {original.length*16}")
        result = self.native(slot=7)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = np.frombuffer(result.stdout, dtype=np.float64).reshape(8, 512, 2)
        original.trigger()
        for block in range(8):
            l, r = np.zeros(512), np.zeros(512)
            original.process(512, l, r)
            self.assertLessEqual(float(np.max(np.abs(actual[block] - np.column_stack((l, r))))), 1e-10)
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(self.bundle.stat().st_mode & 0o777, 0o600)

    def test_empty_library_and_exclusive_destination(self):
        assets.prepare_bank(self.library, self.bundle)
        self.assertEqual(self.native().stdout, b"0 0\n")
        with self.assertRaises(FileExistsError):
            assets.prepare_bank(self.library, self.bundle)

    def test_bad_present_asset_aborts_bank_and_sources_are_untouched(self):
        path = self.library / "pad_C.wav"
        path.write_bytes(wav_bytes([0]*16))
        bad = self.library / "pad_G.wav"
        bad.write_bytes(b"broken recording")
        with self.assertRaises(ValueError):
            assets.prepare_bank(self.library, self.bundle)
        self.assertNotEqual(self.native().returncode, 0)
        self.assertEqual(bad.read_bytes(), b"broken recording")
        self.assertEqual(self.bundle.read_bytes()[:8], b"STVPEND1")

    def test_source_symlink_nonfinite_and_preallocation_budgets_refused(self):
        real = self.directory / "real.wav"
        real.write_bytes(wav_bytes([0]*32))
        path = self.library / "pad_C.wav"
        path.symlink_to(real)
        with self.assertRaises(OSError):
            assets.decode_wave(path)
        with self.assertRaises(ValueError):
            assets.decode_wave(real, available=1)
        real.write_bytes(wav_bytes([0, 0, 0, float("nan")], tag=3, bits=32, channels=1))
        with self.assertRaises(ValueError):
            assets.decode_wave(real)

    def test_bank_aggregate_budget_checked_before_next_decode(self):
        for filename in ("pad_C.wav", "pad_G.wav"):
            (self.library / filename).write_bytes(wav_bytes([0]*16))
        with patch.object(assets, "BANK_BYTES", 128), self.assertRaises(ValueError):
            assets.prepare_bank(self.library, self.bundle)
        self.assertNotEqual(self.native().returncode, 0)

    def test_native_rejects_malformed_and_symlink_bundles(self):
        h = struct.pack("<8sII", b"STVBANK1", 48000, 1)
        s = struct.pack("<IIQ", 0, 0, 4)
        pcm = struct.pack("<8d", *([0]*8))
        for data in (b"", h, h+s, h+s+pcm+b"x", h+struct.pack("<IIQ", 12, 0, 4)+pcm,
                     h+struct.pack("<IIQ", 0, 1, 4)+pcm, h+struct.pack("<IIQ", 0, 0, 2**63),
                     h+s+struct.pack("<8d", *([float("inf")]+[0]*7)),
                     struct.pack("<8sII", b"STVBANK1", 44100, 1)+s+pcm,
                     struct.pack("<8sII", b"STVBANK1", 48000, 2)+s+pcm+s+pcm):
            self.bundle.write_bytes(data)
            self.assertNotEqual(self.native().returncode, 0)
        self.bundle.write_bytes(h+s+pcm)
        self.assertEqual(self.native().returncode, 0)
        link = self.directory / "link"
        link.symlink_to(self.bundle)
        self.assertNotEqual(self.native(link).returncode, 0)


if __name__ == "__main__":
    unittest.main()
