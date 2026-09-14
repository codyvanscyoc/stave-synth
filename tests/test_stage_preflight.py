"""Read-only preflight tests: private files and mocked system/device probes."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PATH = Path(__file__).resolve().parents[1] / "tools/stage_preflight.py"
SPEC = importlib.util.spec_from_file_location("stage_preflight", PATH)
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stave-preflight-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "checkout"
        self.root.mkdir()
        self.instance = self.base / "instance"
        self.home = self.base / "home"
        self.env = dict(STAVE_INSTANCE="qa", STAVE_INSTANCE_ROOT=str(self.instance),
                        STAVE_HTTP_PORT="18080", STAVE_WEBSOCKET_PORT="18765")
        for name in (*preflight.NATIVE_FILES, *preflight.SOURCE_FILES):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            header = b"\x7fELF\x02\x01" + b"\x00" * 12 + (183).to_bytes(2, "little")
            path.write_bytes(header if name in preflight.NATIVE_FILES else b"source\n")
        self.manifest = self.base / "hashes.md"
        self.manifest.write_text("```text\n" + "\n".join(
            f"{preflight.hash_file(self.root / name)}  {name}"
            for name in preflight.NATIVE_FILES) + "\n```\n")
        self.state = self.instance / "config/current_state.json"
        self.state.parent.mkdir(parents=True)
        self.state.write_text(json.dumps({"piano": {"soundfont": "Fluid"},
                                          "master": {"audio_output_pref": "Peavey"}}))
        self.font_dir = self.instance / "data/soundfonts"
        self.font_dir.mkdir(parents=True)
        self.font = self.font_dir / "FluidR3_GM.sf2"
        self.font.write_bytes(b"RIFF" + (4).to_bytes(4, "little") + b"sfbk")
        self.proc = self.base / "proc"
        self.proc.mkdir()
        (self.proc / "meminfo").write_text("MemTotal: 2000000 kB\nMemAvailable: 800000 kB\n")
        self.versions = {key: "99.0" for key in preflight.DEPENDENCIES}

    def collect(self, **kwargs):
        with mock.patch.object(preflight, "package_versions", return_value=self.versions):
            return preflight.collect_preflight(self.root, sys.executable,
                manifest=self.manifest, environ=self.env, home=self.home,
                proc_root=self.proc, sys_root=self.base / "sys", font_dirs=(), **kwargs)

    def check(self, result, name):
        return next(item for item in result["checks"] if item["name"] == name)

    def test_default_is_read_only_and_never_runs_commands(self):
        before = {str(path): path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        runner = mock.Mock(side_effect=AssertionError("no commands allowed"))
        with mock.patch.object(preflight.subprocess, "Popen", side_effect=AssertionError("no process allowed")):
            result = self.collect(runner=runner)
        runner.assert_not_called()
        after = {str(path): path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(result["counts"]["fail"], 0)
        self.assertFalse(result["stage_qualified"])
        self.assertEqual(self.check(result, "system_probes")["status"], "warn")
        self.assertFalse((self.state.parent / "instance.lock").exists())

    def test_missing_artifact_bad_architecture_and_hash_mismatch_fail(self):
        target = self.root / preflight.NATIVE_FILES[0]
        for content in (None, b"not an ELF", target.read_bytes() + b"changed"):
            with self.subTest(content=content):
                if content is None:
                    target.unlink()
                else:
                    target.write_bytes(content)
                result = self.collect()
                self.assertEqual(self.check(result, "artifact:" + preflight.NATIVE_FILES[0])["status"], "fail")

    def test_manifest_refuses_escape_duplicate_missing_and_empty(self):
        digest = "a" * 64
        for text in (f"{digest}  ../outside\n", f"{digest}  /absolute\n",
                     f"{digest}  file\n{digest}  file\n", "no hashes"):
            self.manifest.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                preflight.parse_manifest(self.manifest)
        self.manifest.write_text(f"{preflight.hash_file(self.root / preflight.NATIVE_FILES[0])}  {preflight.NATIVE_FILES[0]}\n")
        self.assertEqual(self.check(self.collect(), "artifact:" + preflight.NATIVE_FILES[1])["status"], "fail")

    def test_manifest_free_hashes_are_not_provenance(self):
        report = preflight.Report()
        preflight.check_artifacts(report, self.root)
        self.assertEqual(self.check(report.result(), "manifest")["status"], "warn")

    def test_native_artifact_cannot_resolve_outside_checkout(self):
        name = preflight.NATIVE_FILES[0]
        path = self.root / name
        outside = self.base / "other.so"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        self.assertEqual(self.check(self.collect(), "artifact:" + name)["status"], "fail")

    def test_soundfont_resolves_symlink_but_rejects_broken_or_truncated_bank(self):
        external = self.base / "real.sf2"
        external.write_bytes(self.font.read_bytes())
        self.font.unlink()
        self.font.symlink_to(external)
        item = self.check(self.collect(), "soundfont:FluidR3_GM")
        self.assertEqual(item["status"], "pass")
        self.assertEqual(item["target"], str(external.resolve()))
        external.write_bytes(b"RIFF" + (100).to_bytes(4, "little") + b"sfbk")
        self.assertEqual(self.check(self.collect(), "soundfont:FluidR3_GM")["status"], "fail")
        external.unlink()
        self.assertEqual(self.check(self.collect(), "soundfont:FluidR3_GM")["status"], "fail")

    def test_bad_current_state_fails_without_rewriting_or_falling_back(self):
        self.state.write_text('{"master":{"volume":NaN}}')
        original = self.state.read_bytes()
        result = self.collect()
        self.assertEqual(self.check(result, "saved_state")["status"], "fail")
        self.assertEqual(self.state.read_bytes(), original)

    def test_invalid_identity_refuses_before_artifacts_or_commands(self):
        self.env = {"STAVE_INSTANCE": "stage", "STAVE_HTTP_PORT": "18080"}
        runner = mock.Mock()
        result = self.collect(system_probes=True, runner=runner)
        runner.assert_not_called()
        self.assertEqual(len(result["checks"]), 1)
        self.assertEqual(result["status"], "fail")

    def test_missing_or_old_dependency_is_a_failure(self):
        self.versions.pop("cffi")
        self.versions["numpy"] = "1.20"
        result = self.collect()
        for name in ("cffi", "numpy"):
            self.assertEqual(self.check(result, "dependency:" + name)["status"], "fail")

    def test_metadata_reader_uses_current_search_path_or_selected_venv(self):
        with mock.patch.object(preflight.importlib.metadata, "distributions", return_value=[]) as distributions:
            self.assertEqual(preflight.package_versions(Path(sys.executable).absolute()), {})
            distributions.assert_called_once_with()
        venv = self.base / "venv"
        packages = venv / "lib/python3.13/site-packages"
        packages.mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        dist = packages / "numpy-2.2.4.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text("Name: numpy\nVersion: 2.2.4\n")
        self.assertEqual(preflight.package_versions(venv / "bin/python"), {"numpy": "2.2.4"})

    def test_low_memory_warns_without_claiming_measured_reserve(self):
        (self.proc / "meminfo").write_text("MemTotal: 2000000 kB\nMemAvailable: 1000 kB\n")
        result = self.collect()
        self.assertEqual(self.check(result, "memory")["status"], "warn")
        self.assertFalse(result["stage_qualified"])

    def test_device_and_fifo_targets_are_rejected_without_opening(self):
        pipe = self.base / "not-a-file"
        os.mkfifo(pipe)
        for operation in (preflight.hash_file, preflight.read_text):
            with self.subTest(operation=operation.__name__), self.assertRaises(ValueError):
                operation(pipe)
        self.font.unlink()
        self.font.symlink_to(pipe)
        self.assertEqual(self.check(self.collect(), "soundfont:FluidR3_GM")["status"], "fail")

    def test_optional_probes_are_read_only_redacted_and_failures_truthful(self):
        (self.proc / "123").mkdir()
        (self.proc / "123/limits").write_text("Max realtime priority     0     0\nMax locked memory     65536     65536     bytes\n")
        captured = []
        def runner(argv):
            captured.append(argv)
            if argv[0] == sys.executable:
                # Execute only the stdlib snippet inside this test process.
                with mock.patch.object(preflight.importlib.metadata, "version", side_effect=lambda n: self.versions[n]):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        exec(argv[-1], {})
                    return output.getvalue()
            if argv[0] == "systemctl":
                return 'ActiveState=active\nMainPID=123\nWorkingDirectory=/old/stage\nEnvironment="SECRET=do-not-emit" "STAVE_FAUST_OSC_BANK=1"\n'
            if argv[0] == "loginctl":
                return "no\n"
            if argv[0] == "pw-jack":
                return ("Peavey:playback_FL\n  32 bit float mono audio\n  properties: input,physical\n"
                        "Peavey:playback_FR\n  32 bit float mono audio\n  properties: input,physical\n")
            raise TimeoutError("fixture timeout")
        result = self.collect(system_probes=True, runner=runner)
        self.assertEqual(self.check(result, "linger")["status"], "fail")
        self.assertEqual(self.check(result, "process_limits")["status"], "warn")
        self.assertEqual(self.check(result, "service_checkout")["status"], "warn")
        self.assertEqual(self.check(result, "preferred_output")["status"], "pass")
        self.assertNotIn("do-not-emit", json.dumps(result))
        self.assertNotIn("SECRET", json.dumps(result))
        for argv in captured:
            self.assertNotIn("start", argv)
            self.assertNotIn("restart", argv)
            self.assertNotIn("jack_connect", argv)
            self.assertNotIn("amixer", argv)

    def test_probe_command_list_rejects_malicious_unit_before_execution(self):
        for name in ("--foo.service", "stage.service;reboot", "../other.service"):
            runner = mock.Mock()
            result = self.collect(service=name, system_probes=True, runner=runner)
            self.assertEqual(result["status"], "fail")
            runner.assert_not_called()

    def test_runner_bounds_own_child_without_services_or_devices(self):
        output = preflight.run_probe([sys.executable, "-I", "-B", "-c", "print('metadata')"])
        self.assertEqual(output, "metadata\n")
        with self.assertRaises(TimeoutError):
            preflight.run_probe([sys.executable, "-I", "-B", "-c", "import time; time.sleep(2)"], timeout=.05)
        with mock.patch.object(preflight, "MAX_PROBE_OUTPUT", 16), self.assertRaises(ValueError):
            preflight.run_probe([sys.executable, "-I", "-B", "-c", "print('x'*1000)"])


if __name__ == "__main__":
    unittest.main()
