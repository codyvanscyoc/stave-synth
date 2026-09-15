"""Safe organ-variant checker tests; no real native library, app, or JACK."""

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

from tools import check_organ_variants as checker


def zone_metadata():
    result = {}
    for name in checker.expected_zone_names():
        initial, low, high, step = 0.0, 0.0, 1.0, 0.001
        if name.startswith("freq_v"):
            high = 12000.0
        elif name.startswith("pan_v"):
            low = -1.0
        elif name.startswith("amp_d"):
            high = 2.0
        elif name == "drive":
            initial = 0.05
        elif name == "leslie_depth":
            initial = 0.3
        elif name == "leslie_target_hz":
            initial, high = 0.8, 10.0
        elif name == "volume":
            initial = 0.5
        elif name == "highcut_hz":
            initial, low, high, step = 8000.0, 200.0, 12000.0, 1.0
        elif name == "lowcut_hz":
            initial, low, high, step = 40.0, 20.0, 500.0, 0.5
        elif name == "tone_tilt":
            initial = 0.5
        result[name] = {"initial": initial, "min": low, "max": high, "step": step}
    return result


class FakeOrgan:
    def __init__(self, delta=0.0):
        self.metadata = zone_metadata()
        self.zones = {name: data["initial"] for name, data in self.metadata.items()}
        self.delta = delta
        self.phase = 0
        self.current = (array("f"), array("f"))
        self.closed = False
        self.native_calls = 0
        self.input_values = []

    def set(self, name, value):
        data = self.metadata[name]
        if not data["min"] <= value <= data["max"]:
            raise AssertionError((name, value))
        self.zones[name] = value

    def prepare_input(self, absolute_frame, frames):
        self.input_values = [0.01 if absolute_frame <= 192000 < absolute_frame + frames else 0.0] * frames

    def compute_native(self, frames):
        self.native_calls += 1
        left, right = array("f"), array("f")
        gate = sum(self.zones[f"gate_v{i}"] for i in range(16))
        amps = sum(self.zones[f"amp_d{i}"] for i in range(9))
        for index in range(frames):
            # State evolves one sample at a time and is independent of call partition.
            self.phase += 1
            value = (0.001 * gate + 0.002 * amps + 1e-6 * (self.phase % 97)
                     + self.input_values[index] + self.delta)
            left.append(value)
            right.append(-0.7 * value)
        self.current = left, right

    def output(self, _frames):
        if not all(math.isfinite(value) for channel in self.current for value in channel):
            raise RuntimeError("nonfinite")
        return self.current

    def compute(self, absolute_frame, frames):
        self.prepare_input(absolute_frame, frames)
        self.compute_native(frames)
        return self.output(frames)

    def close(self):
        self.closed = True


class CostClock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value


class CostOrgan(FakeOrgan):
    def __init__(self, clock, cost):
        super().__init__()
        self.clock, self.cost = clock, cost

    def compute_native(self, frames):
        self.native_calls += 1
        self.clock.value += self.cost
        self.current = (array("f", [0.1] * frames), array("f", [-0.1] * frames))


class FakeLibrary:
    def __init__(self):
        self.storage = {}
        self.deleted = []
        self.newStaveOrgan = Mock(return_value=321)
        self.deleteStaveOrgan = Mock(side_effect=self.deleted.append)
        self.initStaveOrgan = Mock()
        self.buildUserInterfaceStaveOrgan = Mock(side_effect=self.build_ui)
        self.computeStaveOrgan = Mock(side_effect=self.compute)

    def build_ui(self, _dsp, glue_pointer):
        glue = ctypes.cast(glue_pointer, ctypes.POINTER(checker.UIGlue)).contents
        for name, data in zone_metadata().items():
            zone = self.storage[name] = ctypes.c_float(data["initial"])
            glue.addHorizontalSlider(None, name.encode(), ctypes.pointer(zone), data["initial"],
                                     data["min"], data["max"], data["step"])

    @staticmethod
    def compute(_dsp, frames, inputs, outputs):
        for index in range(frames):
            outputs[0][index] = inputs[0][index] + 0.25
            outputs[1][index] = -0.125


class OrganVariantTests(unittest.TestCase):
    def test_failed_parity_retains_segment_and_speed_evidence_and_closes_handles(self):
        organs = []
        def factory(path):
            organ = FakeOrgan(delta=0.01 if path == "variant" else 0)
            organs.append(organ)
            return organ
        report = checker.new_report()
        with patch.object(checker, "NativeOrgan", side_effect=factory), \
                patch.object(checker, "SCENARIO_FRAMES", 256), \
                patch.object(checker, "EVENT_FRAMES", (0, 32, 64, 96, 128, 160, 192, 224, 240)), \
                patch.object(checker, "BLOCK_FRAMES", 64), \
                patch.object(checker, "benchmark_pair", return_value={"gate": {"passed": True}}) as benchmark:
            with self.assertRaisesRegex(RuntimeError, "absolute error"):
                checker.check_variants("scalar", "variant", report)
        self.assertEqual(len(report["scalar_variant_segments"]), 9)
        self.assertTrue(report["parity_failures"])
        self.assertTrue(report["benchmark"]["gate"]["passed"])
        self.assertEqual(report["status"], "FAIL")
        benchmark.assert_called_once()
        self.assertEqual(len(organs), 6)
        self.assertTrue(all(organ.closed for organ in organs))

    def test_zone_inventory_is_exact_and_metadata_must_agree(self):
        self.assertEqual(len(checker.expected_zone_names()), 80)
        old, new = zone_metadata(), zone_metadata()
        checker.compare_metadata(old, new)
        new["volume"]["max"] = 0.9
        with self.assertRaisesRegex(RuntimeError, "metadata differs"):
            checker.compare_metadata(old, new)
        new = zone_metadata()
        new["unexpected"] = new["volume"]
        with self.assertRaisesRegex(RuntimeError, "ABI names"):
            checker.compare_metadata(old, new)

    def test_full_scenario_is_partition_invariant_at_control_boundaries(self):
        events = (0, 32, 64, 96, 128, 160, 192, 224, 240)
        production, remainder = FakeOrgan(), FakeOrgan()
        with patch.object(checker, "SCENARIO_FRAMES", 256), \
                patch.object(checker, "EVENT_FRAMES", events), \
                patch.object(checker, "BLOCK_FRAMES", 64), \
                patch.object(checker, "REMAINDER_PATTERN", (7, 13, 19, 25)):
            old, old_chunks = checker.render_scenario(production, "production")
            new, new_chunks = checker.render_scenario(remainder, "remainder")
        metrics = checker.parity_metrics(old, new)
        self.assertTrue(metrics["sample_exact"])
        self.assertEqual(metrics["null_peak"], 0.0)
        self.assertGreater(new_chunks, old_chunks)
        self.assertEqual(len(old[0]), 256)

    def test_parity_reports_every_sample_and_rejects_error_nonfinite_and_silence(self):
        reference = (array("f", [0.1, -0.2, 0.3]), array("f", [0.4, -0.5, 0.6]))
        same = (array("f", reference[0]), array("f", reference[1]))
        metrics = checker.parity_metrics(reference, same)
        self.assertEqual(metrics["sample_count"], 6)
        self.assertTrue(metrics["sample_exact"])
        checker.enforce_parity(metrics, "same")
        changed = (array("f", [0.2, -0.2, 0.3]), array("f", reference[1]))
        with self.assertRaisesRegex(RuntimeError, "absolute error"):
            checker.enforce_parity(checker.parity_metrics(reference, changed), "changed")
        with self.assertRaisesRegex(RuntimeError, "NaN/Inf"):
            checker.parity_metrics(reference, (array("f", [math.nan, 0, 0]), same[1]))
        silence = (array("f", [0, 0, 0]), array("f", [0, 0, 0]))
        with self.assertRaisesRegex(RuntimeError, "silent"):
            checker.enforce_parity(checker.parity_metrics(silence, silence), "silence")

    def test_benchmark_alternates_five_runs_and_applies_material_speed_gate(self):
        clock = CostClock()
        scalar, variant = CostOrgan(clock, 100), CostOrgan(clock, 70)
        with patch.object(checker, "BENCHMARK_RUNS", 5), \
                patch.object(checker, "BENCHMARK_BLOCKS_PER_RUN", 3), \
                patch.object(checker, "BENCHMARK_WARMUP_BLOCKS", 1):
            result = checker.benchmark_pair(scalar, variant, clock)
        self.assertEqual(result["scalar"]["samples"], 15)
        self.assertEqual(result["scalar"]["median_compute_cpu_ns"], 100)
        self.assertEqual(result["variant"]["p95_compute_cpu_ns"], 70)
        self.assertEqual(result["alternating_order"][1], ["variant", "scalar"])
        self.assertTrue(result["gate"]["passed"])
        clock = CostClock()
        with patch.object(checker, "BENCHMARK_RUNS", 5), \
                patch.object(checker, "BENCHMARK_BLOCKS_PER_RUN", 1), \
                patch.object(checker, "BENCHMARK_WARMUP_BLOCKS", 0):
            slower = checker.benchmark_pair(CostOrgan(clock, 100), CostOrgan(clock, 95), clock)
        self.assertFalse(slower["gate"]["passed"])

    def test_native_api_uses_local_handles_and_fixed_buffers(self):
        library = FakeLibrary()
        with patch.object(checker.ctypes, "CDLL", return_value=library) as loader, \
                patch.object(checker.os, "RTLD_LOCAL", 0, create=True), \
                patch.object(checker.os, "RTLD_NOW", 2, create=True):
            organ = checker.NativeOrgan("private.so")
            try:
                organ.set("leslie_target_hz", 0.0)
                output = organ.compute(0, 17)
                self.assertEqual(output[0][0], 0.25)
                self.assertTrue(all(math.isfinite(value) for value in output[0]))
                self.assertEqual(output[1], array("f", [-0.125] * 17))
                self.assertEqual(len(organ.metadata), 80)
                library.initStaveOrgan.assert_called_once_with(321, 48000)
            finally:
                organ.close()
                organ.close()
        loader.assert_called_once_with("private.so", mode=2)
        self.assertEqual(library.deleted, [321])

    @staticmethod
    def elf(machine=183):
        header = bytearray(64)
        header[:7] = b"\x7fELF\x02\x01\x01"
        struct.pack_into("<HH", header, 16, 3, machine)
        return bytes(header)

    def test_library_validation_rejects_active_path_same_inode_and_wrong_platform(self):
        with tempfile.TemporaryDirectory() as temporary:
            old, new = Path(temporary) / "old.so", Path(temporary) / "new.so"
            old.write_bytes(self.elf())
            new.write_bytes(self.elf() + b"new")
            with patch.object(checker.platform, "system", return_value="Linux"), \
                    patch.object(checker.platform, "machine", return_value="aarch64"), \
                    patch.object(checker, "_active_library_path", return_value=Path("/active.so")):
                self.assertEqual(len(checker.validate_libraries(old, new)), 2)
                with self.assertRaisesRegex(RuntimeError, "distinct"):
                    checker.validate_libraries(old, old)
                with patch.object(checker, "_active_library_path", return_value=old.resolve()), \
                        self.assertRaisesRegex(RuntimeError, "active-service"):
                    checker.validate_libraries(old, new)
            with patch.object(checker.platform, "system", return_value="Darwin"), \
                    patch.object(checker.ctypes, "CDLL") as loader, \
                    self.assertRaisesRegex(RuntimeError, "Linux AArch64"):
                checker.validate_libraries(old, new)
            loader.assert_not_called()

    def test_worker_timeout_terminates_only_owned_child(self):
        receive, send, process = Mock(), Mock(), Mock()
        receive.poll.return_value = False
        process.pid = 444
        process.is_alive.side_effect = [True, True]
        context = SimpleNamespace(Pipe=Mock(return_value=(receive, send)), Process=Mock(return_value=process))
        with patch.object(checker, "validate_libraries",
                          return_value=[{"path": "old.so"}, {"path": "new.so"}]), \
                patch.object(checker.multiprocessing, "get_context", return_value=context):
            report = checker.run("old.so", "new.so")
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("deadline" in item for item in report["failures"]))
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        receive.poll.assert_called_once_with(45.0)

    def test_report_is_private_exclusive_and_cli_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            checker.write_new_report(output, {"status": "PASS", "value": 1.0})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(__import__("json").loads(output.read_text())["status"], "PASS")
            with self.assertRaises(FileExistsError):
                checker.write_new_report(output, {"status": "FAIL"})
            with patch.object(checker, "run", return_value={"status": "PASS"}), \
                    patch("builtins.print"):
                second = Path(temporary) / "second.json"
                self.assertEqual(checker.main(["--scalar", "a", "--variant", "b",
                                               "--output", str(second)]), 0)
                self.assertTrue(second.exists())
                self.assertEqual(checker.main(["--scalar", "a", "--variant", "b",
                                               "--output", str(second)]), 2)


if __name__ == "__main__":
    unittest.main()
