"""Durable one-take WAV worker; no JACK, service, browser or devices."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NativeRecordingWriterTests(unittest.TestCase):
    def test_writer_finalization_and_no_replace(self):
        with tempfile.TemporaryDirectory(prefix="stave-writer-offline-") as directory:
            binary = Path(directory) / "writer"
            subprocess.run(["c++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                            "-fsanitize=undefined", "-fno-sanitize-recover=all", "-I", str(ROOT / "native_v2/include"),
                            str(ROOT / "native_v2/tests/test_recording_writer.cpp"), "-o", str(binary)],
                           check=True, capture_output=True, text=True, timeout=60)
            result = subprocess.run([str(binary), directory], check=True, capture_output=True, text=True, timeout=30)
            self.assertIn("PASS:", result.stdout)


if __name__ == "__main__":
    unittest.main()
