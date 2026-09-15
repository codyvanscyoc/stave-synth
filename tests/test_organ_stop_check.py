"""Native checker tests with fake libraries/processes: no native loads or I/O devices."""

from array import array
import ctypes
import math
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools import check_organ_stop as checker


def metadata(minimum=0.0):
    result = {}
    def add(name, initial=0.0, low=0.0, high=1.0, step=0.001):
        result[name] = {"initial": initial, "min": low, "max": high, "step": step}
    for index in range(16):
        add(f"freq_v{index}", high=12000.0)
        add(f"gate_v{index}")
        add(f"phase_v{index}")
        add(f"pan_v{index}", low=-1.0)
    for index in range(9):
        add(f"amp_d{index}", high=2.0)
    for name, initial, low, high in (
            ("drive", 0.05, 0, 1), ("leslie_depth", 0.3, 0, 1),
            ("leslie_target_hz", 0.8, minimum, 10), ("volume", 0.5, 0, 1),
            ("highcut_hz", 8000, 200, 12000), ("lowcut_hz", 40, 20, 500), ("tone_tilt", 0.5, 0, 1)):
        add(name, initial, low, high)
    return result


class FakeOrgan:
    def __init__(self, minimum=0.0, *, delta=0.0, stop_nonfinite=False, stop_mute=False, stop_mutate=False):
        self.metadata = metadata(minimum)
        self.zones = {name: values["initial"] for name, values in self.metadata.items()}
        self.delta = delta
        self.stop_nonfinite, self.stop_mute, self.stop_mutate = stop_nonfinite, stop_mute, stop_mutate
        self.computes = []

    def set(self, name, value):
        self.zones[name] = value

    def get(self, name):
        return self.zones[name]

    def compute(self, frames):
        self.computes.append((frames, self.zones["leslie_target_hz"]))
        value = 0.2 + self.delta
        if self.zones["leslie_target_hz"] == 0:
            if self.stop_nonfinite:
                value = math.nan
            if self.stop_mute:
                value = 0.0
            if self.stop_mutate:
                self.zones["gate_v0"] = 0.0
        channel = array("f", [value] * frames).tobytes()
        return channel, channel


class FakeLibrary:
    def __init__(self, *, duplicate=False):
        self.storage = {}
        self.deleted = []
        self.newStaveOrgan = Mock(return_value=123)
        self.initStaveOrgan = Mock()
        self.deleteStaveOrgan = Mock(side_effect=self.deleted.append)
        self.buildUserInterfaceStaveOrgan = Mock(side_effect=self.build_ui)
        self.computeStaveOrgan = Mock(side_effect=self.compute)
        self.duplicate = duplicate

    def build_ui(self, _dsp, glue_pointer):
        glue = ctypes.cast(glue_pointer, ctypes.POINTER(checker.UIGlue)).contents
        for name, values in metadata().items():
            zone = self.storage[name] = ctypes.c_float(values["initial"])
            glue.addHorizontalSlider(None, name.encode(), ctypes.pointer(zone),
                                     values["initial"], values["min"], values["max"], values["step"])
        if self.duplicate:
            glue.addHorizontalSlider(None, b"leslie_target_hz", ctypes.pointer(self.storage["leslie_target_hz"]),
                                     0.8, 0.0, 10.0, 0.001)

    def compute(self, _dsp, frames, inputs, outputs):
        for index in range(frames):
            if inputs[0][index] != 0:
                raise AssertionError("click input was not deterministically silent")
            outputs[0][index] = 0.25
            outputs[1][index] = -0.125


class OrganStopCheckTests(unittest.TestCase):
    def small_pair(self, baseline=None, candidate=None):
        baseline = baseline or FakeOrgan(0.1)
        candidate = candidate or FakeOrgan()
        report = checker.new_report()
        with patch.object(checker, "SAMPLE_RATE", 32), patch.object(checker, "BLOCK_FRAMES", 8), \
                patch.object(checker, "STOP_SECONDS", 3):
            checker.check_pair(baseline, candidate, report)
        return report, baseline, candidate

    def test_exact_parity_and_stop_have_complete_bounded_evidence(self):
        report, baseline, candidate = self.small_pair()
        self.assertEqual([item["mode"] for item in report["parity"]], ["slow", "fast"])
        self.assertTrue(all(item["sample_exact"] and item["max_abs_delta"] == 0 for item in report["parity"]))
        self.assertEqual(report["stop"]["frames"], 96)
        self.assertTrue(report["stop"]["all_samples_finite"])
        self.assertTrue(report["stop"]["completed"])
        self.assertEqual(len(report["stop"]["one_second_windows"]), 3)
        self.assertEqual(len(baseline.computes), 8)
        self.assertEqual(len(candidate.computes), 20)
        self.assertEqual(candidate.get("leslie_depth"), 0.3)
        self.assertEqual(candidate.get("gate_v0"), 0.55)

    def test_any_native_waveform_difference_fails_exact_parity(self):
        with self.assertRaisesRegex(RuntimeError, "not sample-exact"):
            self.small_pair(candidate=FakeOrgan(delta=1e-7))

    def test_nonfinite_silent_or_mutated_stop_fails(self):
        for option in ("stop_nonfinite", "stop_mute", "stop_mutate"):
            with self.subTest(option=option), self.assertRaises(RuntimeError):
                self.small_pair(candidate=FakeOrgan(**{option: True}))

    def test_wrong_or_additional_metadata_changes_refuse_before_compute(self):
        for mutate in (
                lambda data: data["leslie_target_hz"].update(min=0.1),
                lambda data: data["volume"].update(max=0.9),
                lambda data: data.pop("gate_v15")):
            candidate = FakeOrgan()
            mutate(candidate.metadata)
            with self.assertRaises(RuntimeError):
                self.small_pair(candidate=candidate)
            self.assertEqual(candidate.computes, [])

    def test_ctypes_api_captures_metadata_zero_target_and_exact_output_shape(self):
        library = FakeLibrary()
        with patch.object(checker.ctypes, "CDLL", return_value=library), \
                patch.object(checker.os, "RTLD_LOCAL", 0, create=True), \
                patch.object(checker.os, "RTLD_NOW", 2, create=True):
            organ = checker.NativeOrgan("not-loaded.so")
            try:
                checker.configure(organ)
                organ.set("leslie_target_hz", 0.0)
                self.assertEqual(organ.get("leslie_target_hz"), 0.0)
                raw = organ.compute(5)
                self.assertEqual(checker.samples(raw, 5), [0.25] * 5 + [-0.125] * 5)
                library.initStaveOrgan.assert_called_once_with(123, 48000)
                self.assertEqual(organ.metadata["leslie_target_hz"]["min"], 0.0)
                with self.assertRaises(RuntimeError):
                    organ.set("leslie_target_hz", -0.1)
            finally:
                organ.close()
                organ.close()
        self.assertEqual(library.deleted, [123])
        self.assertFalse(hasattr(library, "instanceClearStaveOrgan"))

    def test_bad_ui_callbacks_fail_and_delete_allocated_dsp(self):
        library = FakeLibrary(duplicate=True)
        with patch.object(checker.ctypes, "CDLL", return_value=library), \
                patch.object(checker.os, "RTLD_LOCAL", 0, create=True), \
                patch.object(checker.os, "RTLD_NOW", 2, create=True), \
                self.assertRaisesRegex(RuntimeError, "duplicate"):
            checker.NativeOrgan("not-loaded.so")
        self.assertEqual(library.deleted, [123])

    @staticmethod
    def elf(machine=183):
        header = bytearray(64)
        header[:7] = b"\x7fELF\x02\x01\x01"
        struct.pack_into("<HH", header, 16, 3, machine)
        return bytes(header)

    def test_paths_require_distinct_regular_aarch64_shared_objects(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(checker.platform, "system", return_value="Linux"), \
                patch.object(checker.platform, "machine", return_value="aarch64"):
            baseline, candidate = Path(temporary) / "old.so", Path(temporary) / "new.so"
            baseline.write_bytes(self.elf())
            candidate.write_bytes(self.elf() + b"candidate")
            result = checker.validate_libraries(baseline, candidate)
            self.assertEqual(len(result), 2)
            self.assertNotEqual(result[0]["sha256"], result[1]["sha256"])
            with self.assertRaises(RuntimeError):
                checker.validate_libraries(baseline, baseline)
            candidate.write_bytes(self.elf(machine=62))
            with self.assertRaises(RuntimeError):
                checker.validate_libraries(baseline, candidate)
            hardlink = Path(temporary) / "hardlink.so"
            os.link(baseline, hardlink)
            with self.assertRaisesRegex(RuntimeError, "hard links"):
                checker.validate_libraries(baseline, hardlink)

    def test_unsupported_platform_refuses_without_loading_or_spawning(self):
        with patch.object(checker.platform, "system", return_value="Darwin"), \
                patch.object(checker.ctypes, "CDLL") as load, \
                patch.object(checker.multiprocessing, "get_context") as context:
            report = checker.run("old.so", "new.so")
        self.assertEqual(report["status"], "FAIL")
        load.assert_not_called()
        context.assert_not_called()

    def test_native_worker_deadline_terminates_only_its_owned_child(self):
        receive, send, process = Mock(), Mock(), Mock()
        receive.poll.return_value = False
        process.pid = 100
        process.is_alive.side_effect = [True, True]
        context = SimpleNamespace(Pipe=Mock(return_value=(receive, send)), Process=Mock(return_value=process))
        with patch.object(checker, "validate_libraries", return_value=[{"path": "old.so"}, {"path": "new.so"}]), \
                patch.object(checker.multiprocessing, "get_context", return_value=context):
            report = checker.run("old.so", "new.so")
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("deadline" in failure for failure in report["failures"]))
        receive.poll.assert_called_once_with(10.0)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertTrue(receive.close.called)
        self.assertTrue(send.close.called)

    def test_cli_requires_both_explicit_paths(self):
        with patch.object(checker, "run") as run, patch("sys.stderr"), self.assertRaises(SystemExit):
            checker.main(["--baseline", "old.so"])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
