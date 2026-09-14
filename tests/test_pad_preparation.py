"""Offline bounded WAV/preparation and slot-local replacement regressions.

Actual SamplePlayer/pad-bank source is extracted without importing the synth,
Faust, SciPy, home config or devices. Native-rate decoding uses real fixture
files; resampling tests mock only SciPy's boundary, not its DSP quality.
"""

import ast
import logging
import os
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

import numpy as np


ROOT = Path(__file__).resolve().parents[1]


class FilterSpy:
    def __init__(self, *_args):
        self.state = 0

    def reset(self):
        self.state = 0

    def set_params(self, *_args):
        pass

    def process(self, data):
        return data


class TrackingLock:
    def __init__(self):
        self.lock = threading.RLock()
        self.depth = 0

    def __enter__(self):
        self.lock.acquire()
        self.depth += 1
        return self

    def __exit__(self, *_args):
        self.depth -= 1
        self.lock.release()


def audio_namespace():
    source = ROOT / "stave_synth/synth_engine.py"
    tree = ast.parse(source.read_text())
    sample = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SamplePlayer")
    engine = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SynthEngine")
    methods = {"prepare_pad_sample", "install_pad_sample", "discard_prepared_pad", "clear_pad_sample",
               "pad_sample_status", "load_pad_samples", "trigger_pad_sample", "release_pad_samples"}
    constants = {"_PAD_NOTE_FILENAMES", "MAX_PAD_BANK_BYTES", "_PAD_LIBRARY_LOCK"}
    engine.body = [node for node in engine.body if (
        isinstance(node, (ast.FunctionDef, ast.ClassDef))
        and (node.name in methods or node.name == "_PreparedPadSlot")) or (
        isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in constants
                                            for target in node.targets))]
    namespace = {"__name__": "isolated_pad_preparation", "np": np, "SAMPLE_RATE": 48000,
                 "threading": threading, "BiquadLowpass": FilterSpy,
                 "logger": logging.getLogger("isolated_pad_preparation")}
    exec(compile(ast.Module(body=[sample, engine], type_ignores=[]), str(source), "exec"), namespace)
    return namespace


def wav_bytes(values, *, bits=16, tag=1, rate=48000, channels=2, extensible=False):
    values = np.asarray(values).reshape(-1)
    if bits == 24 and tag == 1:
        payload = b"".join(int(value).to_bytes(3, "little", signed=True) for value in values)
    else:
        dtype = f"<f{bits // 8}" if tag == 3 else np.uint8 if bits == 8 else f"<i{bits // 8}"
        payload = values.astype(dtype).tobytes()
    align = channels * (bits // 8)
    fmt = struct.pack("<HHIIHH", 0xFFFE if extensible else tag, channels, rate, rate * align, align, bits)
    if extensible:
        fmt += struct.pack("<HHI", 22, bits, 3 if channels == 2 else 4)
        fmt += struct.pack("<I", tag) + b"\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
    body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(payload)) + payload + (b"\x00" if len(payload) % 2 else b"")
    return b"RIFF" + struct.pack("<I", len(body)) + body


class PadPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stave-pad-prepare-")
        self.directory = Path(self.temporary.name)
        self.namespace = audio_namespace()
        self.Sample = self.namespace["SamplePlayer"]
        self.Engine = self.namespace["SynthEngine"]
        self.engine = self.Engine()
        self.engine.sample_rate = 48000
        self.engine._render_lock = TrackingLock()
        self.engine._pad_samples = {}

    def tearDown(self):
        self.temporary.cleanup()

    def fixture(self, name="sample.wav", values=None, **format_options):
        if values is None:
            values = np.tile(np.array([-32768, 32767, -1234, 1234]), 8)
        path = self.directory / name
        path.write_bytes(wav_bytes(values, **format_options))
        return path

    def player(self, path=None):
        return self.Sample.prepare(path or self.fixture())

    def add(self, note, *, active=False, path=None):
        player = self.player(path)
        if active:
            player.trigger()
            player.env = 0.4
            player.read_pos = 3.0
        self.engine._pad_samples[note] = player
        return player

    def test_native_pcm16_is_exact_immutable_float32_and_matches_float64_render(self):
        path = self.fixture()
        player = self.player(path)
        self.assertEqual(player.samples_l.dtype, np.float32)
        self.assertEqual(player.samples_r.dtype, np.float32)
        self.assertEqual(player.memory_bytes, player.length * 8)
        self.assertFalse(player.samples_l.flags.writeable)
        np.testing.assert_array_equal(player.samples_l.astype(np.float64),
                                      np.tile(np.array([-32768, -1234]) / 32768.0, 8))
        reference = self.player(path)
        reference.samples_l = reference.samples_l.astype(np.float64)
        reference.samples_r = reference.samples_r.astype(np.float64)
        for current in (player, reference):
            current.trigger()
        for count in (7, 256, 4096):
            actual_l, actual_r = np.zeros(count), np.zeros(count)
            expected_l, expected_r = np.zeros(count), np.zeros(count)
            player.process(count, actual_l, actual_r)
            reference.process(count, expected_l, expected_r)
            np.testing.assert_array_equal(actual_l, expected_l)
            np.testing.assert_array_equal(actual_r, expected_r)

    def test_supported_integer_widths_preserve_native_values_and_precision(self):
        for bits in (8, 16, 24, 32):
            values = ([0, 127, 128, 255] if bits == 8
                      else [-(1 << (bits - 1)), -1, 0, (1 << (bits - 1)) - 1])
            with self.subTest(bits=bits):
                player = self.player(self.fixture(values=values, bits=bits, channels=1))
                expected = np.array(values, dtype=np.float64)
                if bits == 8:
                    expected -= 128
                expected /= 1 << (bits - 1)
                np.testing.assert_array_equal(player.samples_l.astype(np.float64), expected)
                self.assertIs(player.samples_l, player.samples_r)
                self.assertEqual(player.samples_l.dtype, np.float64 if bits == 32 else np.float32)
                self.assertEqual(player.memory_bytes, player.samples_l.nbytes)

    def test_float_and_extensible_formats_are_supported_without_normalizing_tone(self):
        for bits, tag, extensible in ((32, 3, False), (64, 3, False), (16, 1, True), (32, 3, True)):
            values = [-2.0, 0.1, 0.2, 1.5] if tag == 3 else [-32768, 0, 1, 32767]
            with self.subTest(bits=bits, tag=tag, extensible=extensible):
                path = self.fixture(values=values, bits=bits, tag=tag, channels=1, extensible=extensible)
                player = self.player(path)
                expected = np.array(values, dtype=f"float{bits}" if tag == 3 else np.float64)
                if tag == 1:
                    expected /= 32768.0
                np.testing.assert_array_equal(player.samples_l, expected)

    def test_zero_and_tiny_files_do_not_replace_a_valid_active_player(self):
        player = self.player()
        player.trigger()
        player.env = 0.3
        player.read_pos = 2.0
        original = player.samples_l
        for frames in range(4):
            with self.subTest(frames=frames):
                path = self.fixture("tiny.wav", values=np.zeros(frames * 2, dtype=np.int16))
                with self.assertLogs("isolated_pad_preparation", "WARNING"):
                    self.assertFalse(player.load(path))
                self.assertTrue(player.loaded)
                self.assertTrue(player.active)
                self.assertIs(player.samples_l, original)
                self.assertEqual(player.env, 0.3)
                self.assertEqual(player.read_pos, 2.0)

    def test_four_frame_loop_wraps_arbitrarily_many_times_without_sticking(self):
        player = self.player(self.fixture(values=[0, 4096, 8192, 16384], channels=1))
        player.trigger()
        player.env = player.env_target = 1.0
        actual = np.zeros(4096)
        player.process(4096, actual, np.zeros(4096))
        indices = []
        position = 0
        for _ in range(4096):
            indices.append(position)
            position += 1
            if position >= 4:
                position -= 3
        np.testing.assert_array_equal(actual, np.array([0, 0.125, 0.25, 0.5])[indices])
        self.assertEqual(player.read_pos, position)
        player.process(0, np.zeros(0), np.zeros(0))
        self.assertEqual(player.read_pos, position)

    def test_invalid_headers_and_truncation_are_rejected_before_sample_allocation(self):
        good = self.fixture().read_bytes()
        cases = [b"not a wave", good[:-1], good[:40],
                 wav_bytes(np.zeros(24), channels=3),
                 wav_bytes(np.zeros(8), rate=0),
                 wav_bytes(np.zeros(8), rate=48001),
                 wav_bytes(np.zeros(8), tag=7)]
        malformed_align = bytearray(good)
        malformed_align[32:34] = struct.pack("<H", 3)
        cases.append(bytes(malformed_align))
        for number, contents in enumerate(cases):
            with self.subTest(case=number):
                path = self.directory / "bad.wav"
                path.write_bytes(contents)
                with patch.object(np, "frombuffer", wraps=np.frombuffer) as allocate:
                    with self.assertRaises(ValueError):
                        self.Sample.prepare(path)
                allocate.assert_not_called()

    def test_slot_source_and_preparation_budgets_precede_allocation(self):
        path = self.fixture()
        for name, limit in (("MAX_SLOT_BYTES", 8), ("MAX_SOURCE_BYTES", 16), ("MAX_PREPARE_BYTES", 1)):
            with self.subTest(bound=name), patch.object(self.Sample, name, limit):
                with patch.object(np, "frombuffer", wraps=np.frombuffer) as allocate:
                    with self.assertRaisesRegex(ValueError, "limit|budget"):
                        self.Sample.prepare(path)
                allocate.assert_not_called()
        with self.assertRaisesRegex(ValueError, "budget"):
            self.Sample.prepare(path, max_bytes=8)

    def test_nonfinite_float_payloads_reject_without_replacing_valid_slot(self):
        player = self.player()
        original = player.samples_l
        for bad in (np.nan, np.inf, -np.inf):
            path = self.fixture("badfloat.wav", values=[0, 1, bad, 0], bits=32, tag=3, channels=1)
            with self.assertLogs("isolated_pad_preparation", "WARNING"):
                self.assertFalse(player.load(path))
            self.assertIs(player.samples_l, original)
            self.assertIn("non-finite", player.last_load_error)

    def test_source_changed_during_preparation_is_rejected(self):
        path = self.fixture()
        inspect = self.Sample._inspect_wave

        def change_source(stream, sample_rate, max_bytes):
            result = inspect(stream, sample_rate, max_bytes)
            before = path.stat()
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
            return result

        with patch.object(self.Sample, "_inspect_wave", side_effect=change_source):
            with self.assertRaisesRegex(ValueError, "changed"):
                self.Sample.prepare(path)

    def scipy_mock(self, resample):
        package = ModuleType("scipy")
        signal = ModuleType("scipy.signal")
        signal.resample_poly = resample
        package.signal = signal
        return patch.dict(sys.modules, {"scipy": package, "scipy.signal": signal})

    def test_resample_boundary_uses_float64_expected_shape_and_fails_closed(self):
        path = self.fixture(rate=24000)
        resample = Mock(side_effect=lambda values, up, down: np.repeat(values, up))
        with self.scipy_mock(resample):
            player = self.Sample.prepare(path)
        self.assertEqual(player.length, 32)
        self.assertEqual(player.samples_l.dtype, np.float64)
        self.assertEqual(resample.call_count, 2)
        for call in resample.call_args_list:
            self.assertEqual(call.args[0].dtype, np.float64)
            self.assertEqual(call.args[1:], (2, 1))
        original = player.samples_l
        with self.scipy_mock(Mock(side_effect=RuntimeError("resampler unavailable"))):
            with self.assertLogs("isolated_pad_preparation", "WARNING"):
                self.assertFalse(player.load(path))
        self.assertIs(player.samples_l, original)
        with self.scipy_mock(lambda values, up, down: np.full(values.size * up, np.nan)):
            with self.assertRaisesRegex(ValueError, "resampled"):
                self.Sample.prepare(path)

    def test_replace_only_target_preserves_unrelated_playing_bed(self):
        old = self.add(60)
        other = self.add(62, active=True)
        before = (other.env, other.env_target, other.read_pos, other.samples_l)
        prepared = self.engine.prepare_pad_sample(60, self.fixture("replacement.wav", values=np.zeros(64)))
        self.assertIs(self.engine._pad_samples[60], old)
        self.assertEqual(self.engine.pad_sample_status()["preparing_note"], 60)
        with self.assertRaisesRegex(RuntimeError, "being updated"):
            self.engine.trigger_pad_sample(60)
        self.engine.install_pad_sample(60, prepared)
        self.assertIsNot(self.engine._pad_samples[60], old)
        self.assertIs(self.engine._pad_samples[62], other)
        self.assertEqual((other.env, other.env_target, other.read_pos), before[:3])
        self.assertIs(other.samples_l, before[3])
        self.assertTrue(other.active)
        self.assertIsNone(self.engine.pad_sample_status()["preparing_note"])

    def test_preparation_is_outside_render_lock_and_reservation_blocks_target_trigger(self):
        original_prepare = self.Sample.prepare

        def check_lock(*args, **kwargs):
            self.assertEqual(self.engine._render_lock.depth, 0)
            with self.assertRaisesRegex(RuntimeError, "being updated"):
                self.engine.trigger_pad_sample(60)
            return original_prepare(*args, **kwargs)

        with patch.object(self.Sample, "prepare", side_effect=check_lock):
            prepared = self.engine.prepare_pad_sample(60, self.fixture())
        self.engine.install_pad_sample(60, prepared)

    def test_active_target_cannot_be_replaced_or_cleared(self):
        old = self.add(60, active=True)
        with patch.object(self.Sample, "prepare") as decode:
            for path in (self.fixture(), None):
                with self.assertRaisesRegex(RuntimeError, "Stop this pad"):
                    self.engine.prepare_pad_sample(60, path)
            with self.assertRaisesRegex(RuntimeError, "Stop this pad"):
                self.engine.clear_pad_sample(60)
        decode.assert_not_called()
        self.assertIs(self.engine._pad_samples[60], old)
        self.assertTrue(old.active)

    def test_reserved_clear_only_removes_target_after_install(self):
        old = self.add(60)
        other = self.add(62, active=True)
        prepared = self.engine.prepare_pad_sample(60, None)
        self.assertIs(self.engine._pad_samples[60], old)
        with self.assertRaises(RuntimeError):
            self.engine.trigger_pad_sample(60)
        self.engine.install_pad_sample(60, prepared)
        self.assertNotIn(60, self.engine._pad_samples)
        self.assertIs(self.engine._pad_samples[62], other)
        self.assertTrue(other.active)

    def test_only_one_staging_reservation_and_failed_commit_preserves_old_player(self):
        old = self.add(60)
        prepared = self.engine.prepare_pad_sample(60, self.fixture())
        with patch.object(self.Sample, "prepare") as decode:
            with self.assertRaisesRegex(RuntimeError, "already being prepared"):
                self.engine.prepare_pad_sample(61, self.fixture())
        decode.assert_not_called()
        self.assertFalse(self.engine.discard_prepared_pad(object()))
        self.assertEqual(self.engine.pad_sample_status()["preparing_note"], 60)
        self.assertTrue(self.engine.discard_prepared_pad(prepared))
        self.assertIs(self.engine._pad_samples[60], old)
        with self.assertRaisesRegex(ValueError, "does not own"):
            self.engine.install_pad_sample(60, prepared)
        next_prepared = self.engine.prepare_pad_sample(61, self.fixture())
        self.engine.install_pad_sample(61, next_prepared)

    def test_bank_budget_is_checked_before_decode_and_replacement_excludes_old_slot(self):
        old = self.add(60)
        self.engine.MAX_PAD_BANK_BYTES = old.memory_bytes
        with patch.object(np, "frombuffer", wraps=np.frombuffer) as allocate:
            with self.assertRaisesRegex(ValueError, "bank byte budget"):
                self.engine.prepare_pad_sample(61, self.fixture())
        allocate.assert_not_called()
        self.assertIsNone(self.engine.pad_sample_status()["preparing_note"])
        prepared = self.engine.prepare_pad_sample(60, self.fixture())
        self.engine.install_pad_sample(60, prepared)
        self.assertEqual(self.engine.pad_sample_status()["memory_bytes"], self.engine.MAX_PAD_BANK_BYTES)

    def test_invalid_scan_preserves_valid_and_unchanged_active_slots_with_errors(self):
        first_path = self.fixture("pad_C.wav")
        other_path = self.fixture("pad_D.wav")
        self.assertEqual(self.engine.load_pad_samples(self.directory), 2)
        old = self.engine._pad_samples[60]
        other = self.engine._pad_samples[62]
        other.trigger()
        other.env = 0.6
        first_path.write_bytes(b"invalid replacement")
        with self.assertLogs("isolated_pad_preparation", "WARNING"):
            self.assertEqual(self.engine.load_pad_samples(self.directory), 2)
        self.assertIs(self.engine._pad_samples[60], old)
        self.assertIs(self.engine._pad_samples[62], other)
        self.assertTrue(other.active)
        self.assertEqual(other.env, 0.6)
        self.assertIn(60, self.engine.pad_sample_status()["errors"])
        self.assertNotIn(62, self.engine.pad_sample_status()["errors"])
        other_path.unlink()
        with self.assertLogs("isolated_pad_preparation", "WARNING"):
            self.engine.load_pad_samples(self.directory)
        self.assertIs(self.engine._pad_samples[62], other)

    def test_scan_enforces_whole_bank_budget_without_discarding_valid_prior_slots(self):
        self.fixture("pad_C.wav")
        self.fixture("pad_Cs.wav")
        self.engine.MAX_PAD_BANK_BYTES = 128
        with self.assertLogs("isolated_pad_preparation", "WARNING"):
            self.assertEqual(self.engine.load_pad_samples(self.directory), 1)
        status = self.engine.pad_sample_status()
        self.assertEqual(status["memory_bytes"], 128)
        self.assertIn(61, status["errors"])
        self.assertIsNone(status["preparing_note"])


if __name__ == "__main__":
    unittest.main()
