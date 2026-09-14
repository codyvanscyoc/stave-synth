"""Prove retired live-stress scripts fail before importing their dependencies."""

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    ROOT / "tests/test_2_param_flood.py",
    ROOT / "tests/test_3_connection_chaos.py",
    ROOT / "tests/test_4_boundary_sweep.py",
)


class RetiredStressGuardTests(unittest.TestCase):
    def test_direct_run_is_unconditionally_refused(self):
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                result = subprocess.run(
                    [sys.executable, str(script)],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("RETIRED", result.stderr)
                self.assertIn("tools/run_offline_tests.py", result.stderr)
                self.assertIn("There is no production override", result.stderr)

    def test_discovery_import_skips_before_dangerous_imports(self):
        probe = textwrap.dedent(
            """
            import importlib.abc
            import runpy
            import sys
            import unittest

            blocked = {
                'asyncio', 'numpy', 'psutil', 'subprocess', 'websockets',
                '_midi_util', 'stave_synth',
            }
            class Blocker(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.split('.', 1)[0] in blocked:
                        raise AssertionError('dangerous import reached: ' + fullname)
                    return None
            sys.meta_path.insert(0, Blocker())
            try:
                runpy.run_path(sys.argv[1], run_name='retired_discovery_probe')
            except unittest.SkipTest as exc:
                assert 'RETIRED' in str(exc)
            else:
                raise AssertionError('retired script did not raise SkipTest')
            """
        )
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                result = subprocess.run(
                    [sys.executable, "-c", probe, str(script)],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
