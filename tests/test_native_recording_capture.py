"""Bounded native recorder transport only; no disk writer, runtime or device."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NativeRecordingCaptureTests(unittest.TestCase):
    def test_queue_ownership_and_failure_guards(self):
        with tempfile.TemporaryDirectory(prefix="stave-capture-offline-") as directory:
            binary = Path(directory) / "capture"
            subprocess.run(["c++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                            "-fsanitize=undefined", "-fno-sanitize-recover=all", "-I", str(ROOT / "native_v2/include"),
                            str(ROOT / "native_v2/tests/test_recording_capture.cpp"), "-o", str(binary)],
                           check=True, capture_output=True, text=True, timeout=60)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=30)
            self.assertIn("PASS:", result.stdout)


if __name__ == "__main__":
    unittest.main()
