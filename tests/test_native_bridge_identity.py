"""Compile the actual JACK bridge against test-only fake JACK headers."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class NativeBridgeIdentityTests(unittest.TestCase):
    def test_exact_identity_isolation_and_failure_cleanup(self) -> None:
        compiler = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
        if compiler is None:
            self.skipTest("C compiler unavailable")
        with tempfile.TemporaryDirectory(prefix="stave-native-test-") as directory:
            executable = Path(directory) / "bridge_identity"
            subprocess.run(
                [
                    compiler,
                    "-std=gnu11",
                    "-Wall",
                    "-Wextra",
                    "-I",
                    str(ROOT / "tests" / "native"),
                    str(ROOT / "tests" / "native" / "test_bridge_identity.c"),
                    "-o",
                    str(executable),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            result = subprocess.run(
                [str(executable)], text=True, capture_output=True, check=True
            )
            self.assertIn("checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
