"""Device-free harness safety checks; compiler/probe subprocesses are mocked."""
import importlib.util
import json
from pathlib import Path
import signal
import tempfile
import unittest
from unittest import mock

PATH=Path(__file__).resolve().parents[1]/"tools/benchmark_native_instrument.py"
spec=importlib.util.spec_from_file_location("offline_instrument_benchmark",PATH)
bench=importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

class InstrumentBenchmarkTests(unittest.TestCase):
    def arguments(self,root,blocks="100"):
        font=root/"fixture.sf2"; font.write_bytes(b"test fixture, not loaded")
        return [str(PATH),"--soundfont",str(font),"--output-dir",str(root/"evidence"),
                "--source-commit","test-only","--blocks",blocks]

    def test_existing_evidence_refused_before_process_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); args=self.arguments(root); (root/"evidence").mkdir()
            with mock.patch("sys.argv",args),mock.patch.object(bench.subprocess,"Popen") as launch:
                with self.assertRaises(FileExistsError): bench.main()
                launch.assert_not_called()

    def test_unbounded_block_count_refused_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with mock.patch("sys.argv",self.arguments(root,"2001")),mock.patch.object(bench.subprocess,"Popen") as launch:
                with self.assertRaises(SystemExit): bench.main()
                self.assertFalse((root/"evidence").exists()); launch.assert_not_called()

    def test_missing_tools_reported_without_install_or_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with mock.patch("sys.argv",self.arguments(root)),mock.patch.object(bench.shutil,"which",return_value=None),mock.patch.object(bench.subprocess,"Popen") as launch:
                self.assertEqual(bench.main(),1); launch.assert_not_called()
            report=json.loads((root/"evidence/report.json").read_text())
            self.assertEqual(report["commands"],[])
            self.assertEqual(report["status"],"failed_or_guard_stopped")

    def test_hot_guard_terminates_only_own_process_group(self):
        class Child:
            pid=987654; returncode=None
            def poll(self): return self.returncode
            def wait(self,timeout): self.returncode=-15; return -15
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            which=lambda name:None if name=="vcgencmd" else "/mock/"+name
            with mock.patch("sys.argv",self.arguments(root)),mock.patch.object(bench.shutil,"which",side_effect=which),\
                 mock.patch.object(bench,"temperature",return_value=81),\
                 mock.patch.object(bench.subprocess,"Popen",return_value=Child()) as launch,\
                 mock.patch.object(bench.os,"killpg") as kill:
                self.assertEqual(bench.main(),1)
                kill.assert_called_once_with(987654,signal.SIGTERM)
                self.assertTrue(launch.call_args.kwargs["start_new_session"])
            report=json.loads((root/"evidence/report.json").read_text())
            self.assertEqual(report["commands"][0]["stop_reason"],"temperature_guard_80C")
            self.assertEqual(report["runs"],[])

    def test_interrupted_monitor_cleans_child_and_restores_handlers(self):
        class Child:
            pid=987655; returncode=None
            def poll(self): return self.returncode
            def wait(self,timeout): self.returncode=-15; return -15
        prior=signal.getsignal(signal.SIGTERM)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with mock.patch("sys.argv",self.arguments(root)),\
                 mock.patch.object(bench.shutil,"which",side_effect=lambda n:None if n=="vcgencmd" else "/mock/"+n),\
                 mock.patch.object(bench,"temperature",side_effect=[60,RuntimeError("Interrupted fixture"),60]),\
                 mock.patch.object(bench.subprocess,"Popen",return_value=Child()),\
                 mock.patch.object(bench.os,"killpg") as kill:
                self.assertEqual(bench.main(),1)
                kill.assert_called_once_with(987655,signal.SIGTERM)
            self.assertEqual(signal.getsignal(signal.SIGTERM),prior)
            report=json.loads((root/"evidence/report.json").read_text())
            self.assertEqual(report["error"],"Interrupted fixture")

    def test_reuse_rejects_changed_toolchain_before_dsp_or_instrument(self):
        class Child:
            pid=987656; returncode=0
            def poll(self): return 0
        def version_only(argv,stdout,**kwargs):
            self.assertIn(argv[1],("--version","--modversion","--cflags"))
            stdout.write("current toolchain\n"); return Child()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); prior=root/"prior"; prior.mkdir()
            (prior/"report.json").write_text(json.dumps({"compiler":"different compiler"}))
            args=self.arguments(root)+["--reuse-dsp-dir",str(prior)]
            with mock.patch("sys.argv",args),mock.patch.object(bench,"temperature",return_value=60),\
                 mock.patch.object(bench.shutil,"which",side_effect=lambda n:None if n=="vcgencmd" else "/mock/"+n),\
                 mock.patch.object(bench.subprocess,"Popen",side_effect=version_only):
                self.assertEqual(bench.main(),1)
            report=json.loads((root/"evidence/report.json").read_text())
            self.assertEqual(report["error"],"DSP reuse toolchain mismatch: compiler")
            self.assertEqual(report["runs"],[])
            self.assertFalse((root/"evidence/osc_bank.o").exists())
