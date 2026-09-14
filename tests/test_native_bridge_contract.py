"""Compile the actual bridge against fake JACK for safety-contract checks."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class NativeBridgeContractTests(unittest.TestCase):
    def test_graph_overflow_and_transition_contract(self) -> None:
        compiler = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
        if compiler is None:
            self.skipTest("C compiler unavailable")
        with tempfile.TemporaryDirectory(prefix="stave-bridge-contract-") as directory:
            executable = Path(directory) / "bridge_contract"
            compile_result = subprocess.run(
                [
                    compiler,
                    "-std=gnu11",
                    "-Wall",
                    "-Wextra",
                    "-pthread",
                    "-I",
                    str(ROOT / "tests" / "native"),
                    str(ROOT / "tests" / "native" / "test_bridge_contract.c"),
                    "-o",
                    str(executable),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual("", compile_result.stderr)
            result = subprocess.run(
                [str(executable)], text=True, capture_output=True, check=True, timeout=5
            )
            self.assertIn("checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
