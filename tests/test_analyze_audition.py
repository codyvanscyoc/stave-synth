"""Device-free analyzer tests using only explicitly created private WAVs."""

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

import numpy as np


PATH = Path(__file__).resolve().parents[1] / "tools/analyze_audition.py"
SPEC = importlib.util.spec_from_file_location("analyze_audition", PATH)
analyzer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyzer)


def chunk(name, data):
    return name + struct.pack("<I", len(data)) + data + (b"\0" if len(data) & 1 else b"")


def wav_bytes(values, *, encoding="float32", extensible=False, rate=8000):
    values = np.asarray(values)
    if values.ndim == 1:
        values = values.reshape(-1, 1)
    channels = values.shape[1]
    tag, bits = (3, 32) if encoding == "float32" else (1, int(encoding[3:]))
    align = channels * bits // 8
    fmt = struct.pack("<HHIIHH", 65534 if extensible else tag, channels,
                      rate, rate * align, align, bits)
    if extensible:
        fmt += struct.pack("<HHII", 22, bits, 4 if channels == 1 else 3, tag) + analyzer.GUID_SUFFIX
    if bits == 24:
        flat = values.astype(np.int32).reshape(-1)
        octets = np.column_stack((flat & 255, (flat >> 8) & 255, (flat >> 16) & 255))
        raw = octets.astype(np.uint8).tobytes()
    else:
        raw = values.astype("<f4" if tag == 3 else "<i2").tobytes()
    body = b"WAVE" + chunk(b"fmt ", fmt) + chunk(b"data", raw)
    return b"RIFF" + struct.pack("<I", len(body)) + body


class AuditionAnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stave-audition-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "explicit.wav"

    def write(self, values, **kwargs):
        self.path.write_bytes(wav_bytes(values, **kwargs))
        return self.path

    def analyze(self, values, **kwargs):
        self.write(values, **kwargs)
        return analyzer.analyze(self.path)

    def test_float_stats_are_exact_and_file_is_unchanged(self):
        values = np.array([[0, .25], [-.5, .5], [.5, .75], [1.25, 1]], dtype=np.float32)
        self.write(values)
        before = self.path.read_bytes()
        report = analyzer.analyze(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(report["sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(report["format"]["frames"], 4)
        self.assertEqual(report["format"]["encoding"], "float32")
        self.assertFalse(report["stage_qualified"])
        self.assertFalse(report["audio_modified"])
        for channel in range(2):
            stats = report["channels"][channel]
            self.assertEqual(stats["sample_peak"], float(np.abs(values[:, channel]).max()))
            self.assertAlmostEqual(stats["dc_mean"], float(values[:, channel].mean()))
            self.assertAlmostEqual(stats["rms"], float(np.sqrt(np.mean(values[:, channel].astype(float) ** 2))))
        self.assertEqual(report["channels"][0]["above_unity_samples"], 1)

    def test_pcm16_extremes_and_endpoint_count(self):
        report = self.analyze([-32768, -1, 0, 32767], encoding="pcm16")
        stats = report["channels"][0]
        self.assertEqual(stats["sample_peak"], 1.0)
        self.assertEqual(stats["full_scale_or_over_samples"], 2)
        self.assertEqual(stats["above_unity_samples"], 0)
        self.assertEqual(stats["exact_zero_samples"], 1)
        self.assertAlmostEqual(stats["dc_mean"], -2 / (4 * 32768))
        self.assertIsNone(report["stereo"])

    def test_pcm24_sign_extension(self):
        values = np.array([-8388608, -123456, -1, 0, 1, 123456, 8388607])
        report = self.analyze(values, encoding="pcm24")
        stats = report["channels"][0]
        expected = values / 8388608.0
        self.assertAlmostEqual(stats["dc_mean"], float(expected.mean()))
        self.assertAlmostEqual(stats["rms"], float(np.sqrt(np.mean(expected ** 2))))
        self.assertEqual(stats["full_scale_or_over_samples"], 2)

    def test_extensible_float_and_pcm24(self):
        for encoding in ("float32", "pcm24"):
            with self.subTest(encoding=encoding):
                report = self.analyze([[1, -1], [0, 0]], encoding=encoding, extensible=True)
                self.assertTrue(report["format"]["extensible"])
                self.assertEqual(report["format"]["encoding"], encoding)

    def test_nonfinite_samples_not_mistaken_for_silence_or_peaks(self):
        report = self.analyze([[float("nan"), 0], [float("inf"), .5], [-float("inf"), -.5]])
        self.assertEqual(report["nonfinite_samples"], 3)
        self.assertEqual(report["status"], "invalid_samples")
        self.assertIsNone(report["channels"][0]["sample_peak"])
        self.assertIsNone(report["channels"][0]["rms"])
        self.assertEqual(report["channels"][0]["full_scale_or_over_samples"], 0)
        self.assertEqual(report["activity"]["invalid"]["run_count"], 1)
        self.assertEqual(report["activity"]["silence"]["run_count"], 0)
        self.assertIsNone(report["stereo"])
        json.dumps(report, allow_nan=False)

    def test_polarity_inversion_reports_mono_cancellation(self):
        left = np.sin(np.arange(8000) * (2 * np.pi * 440 / 8000)) * .4
        report = self.analyze(np.column_stack((left, -left)))
        stereo = report["stereo"]
        self.assertTrue(stereo["mono_zero_with_nonzero_stereo"])
        self.assertEqual(stereo["mono_rms"], 0)
        self.assertIsNone(stereo["mono_relative_db"])
        self.assertAlmostEqual(stereo["correlation"], -1)
        self.assertTrue(any("BTL" in item for item in report["limitations"]))

    def test_identical_stereo_has_zero_db_mono_ratio(self):
        values = np.sin(np.arange(1000)) * .1
        report = self.analyze(np.column_stack((values, values)))
        self.assertAlmostEqual(report["stereo"]["mono_relative_db"], 0)
        self.assertAlmostEqual(report["stereo"]["correlation"], 1)

    def test_activity_timing_and_partial_final_window(self):
        values = np.concatenate((np.zeros(4000), np.full(8000, .25), np.zeros(4013)))
        report = self.analyze(values)
        activity = report["activity"]
        self.assertEqual(activity["first_sample_at_threshold"], 4000)
        self.assertEqual(activity["last_sample_at_threshold"], 11999)
        self.assertEqual(activity["active"]["runs"], [{"start_s": .5, "end_s": 1.5}])
        self.assertEqual(activity["silence"]["run_count"], 2)
        self.assertEqual(activity["silence"]["runs"][-1]["end_s"], len(values) / 8000)

    def test_all_silence_is_reported_without_quality_failure(self):
        report = self.analyze(np.zeros((800, 2)))
        self.assertEqual(report["status"], "analyzed")
        self.assertIsNone(report["activity"]["first_time_s"])
        self.assertIsNone(report["channels"][0]["rms_dbfs"])
        self.assertIsNone(report["stereo"]["correlation"])
        self.assertFalse(report["stereo"]["mono_zero_with_nonzero_stereo"])
        self.assertEqual(report["activity"]["silence"]["total_s"], .1)

    def test_streaming_keeps_cross_chunk_step_and_statistics(self):
        values = np.zeros((3211, 2))
        values[480:2000] = [.5, -.25]
        self.write(values)
        expected = analyzer.analyze(self.path)
        with mock.patch.object(analyzer, "READ_FRAMES", 512):
            actual = analyzer.analyze(self.path)
        self.assertEqual(actual, expected)
        self.assertEqual(actual["channels"][0]["max_adjacent_finite_step"], .5)

    def test_activity_run_output_is_bounded_not_silently_lost(self):
        report = self.analyze(np.tile(np.r_[np.zeros(160), np.ones(160) * .1], 90))
        for kind in ("silence", "active"):
            item = report["activity"][kind]
            self.assertEqual(item["run_count"], 90)
            self.assertEqual(len(item["runs"]), analyzer.MAX_RUNS_PER_KIND)
            self.assertTrue(item["runs_truncated"])
            self.assertAlmostEqual(item["total_s"], 1.8)

    def test_bad_headers_truncation_empty_and_unsupported_encoding_reject(self):
        valid = wav_bytes([0, 1])
        bads = [b"", valid[:-1], b"RF64" + valid[4:], b"RIFX" + valid[4:],
                wav_bytes([]), valid[:20] + struct.pack("<H", 7) + valid[22:],
                valid[:32] + struct.pack("<H", 2) + valid[34:]]
        for data in bads:
            with self.subTest(data=data[:24]):
                self.path.write_bytes(data)
                with self.assertRaises(ValueError):
                    analyzer.analyze(self.path)

    def test_duplicate_data_and_partial_frame_reject(self):
        valid = wav_bytes([0, 1])
        body = valid[8:] + chunk(b"data", b"\0\0\0\0")
        duplicate = b"RIFF" + struct.pack("<I", len(body)) + body
        body = valid[8:36] + chunk(b"data", b"\0\0\0")
        partial = b"RIFF" + struct.pack("<I", len(body)) + body
        for data in (duplicate, partial):
            self.path.write_bytes(data)
            with self.assertRaises(ValueError):
                analyzer.analyze(self.path)

    def test_extensible_invalid_width_guid_and_layout_reject(self):
        valid = wav_bytes([[0, 0]], extensible=True)
        for start, replacement in ((38, struct.pack("<H", 24)), (52, b"wrong-guid!!"),
                                   (40, struct.pack("<I", 12))):
            self.path.write_bytes(valid[:start] + replacement + valid[start + len(replacement):])
            with self.assertRaises(ValueError):
                analyzer.analyze(self.path)

    def test_size_and_numeric_option_bounds(self):
        self.write([0, 1])
        with mock.patch.object(analyzer, "MAX_FILE_BYTES", 10):
            with self.assertRaises(ValueError):
                analyzer.analyze(self.path)
        for kwargs in ({"window_ms": 0}, {"window_ms": float("inf")},
                       {"silence_dbfs": float("nan")}, {"silence_dbfs": 1}):
            with self.assertRaises(ValueError):
                analyzer.analyze(self.path, **kwargs)

    def test_file_change_during_analysis_rejects(self):
        self.write([0, 1])
        real_decode = analyzer._decode

        def mutate(raw, info):
            with self.path.open("ab") as stream:
                stream.write(b"changed")
            return real_decode(raw, info)

        with mock.patch.object(analyzer, "_decode", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "changed during"):
                analyzer.analyze(self.path)

    def test_cli_exit_codes_and_strict_json(self):
        for values, expected_code in (([0, .5], 0), ([float("nan")], 1)):
            self.write(values)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = analyzer.main([str(self.path)])
            self.assertEqual(code, expected_code)
            self.assertFalse(json.loads(output.getvalue())["stage_qualified"])
        self.path.write_bytes(b"broken")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(analyzer.main([str(self.path)]), 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "error")


if __name__ == "__main__":
    unittest.main()
