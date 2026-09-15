"""Offline regressions for FluidSynth's allocation-sensitive load policy."""

import ast
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "stave_synth/fluidsynth_player.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
PLAYER = next(node for node in TREE.body
              if isinstance(node, ast.ClassDef) and node.name == "FluidSynthPlayer")


def method(name, *, low_ram):
    node = next(item for item in PLAYER.body
                if isinstance(item, ast.FunctionDef) and item.name == name)
    namespace = {"LOW_RAM_MODE": low_ram, "RuntimeError": RuntimeError}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace[name]


class FakeSynth:
    def __init__(self, fail_dynamic=False, readback=0, fail_readback=False):
        self.calls = []
        self.fail_dynamic = fail_dynamic
        self.readback = readback
        self.fail_readback = fail_readback

    def setting(self, name, value):
        self.calls.append((name, value))
        if self.fail_dynamic and name == "synth.dynamic-sample-loading":
            raise OSError("unsupported")

    def get_setting(self, name):
        self.calls.append(("get_setting", name))
        if self.fail_readback:
            raise OSError("readback unavailable")
        return self.readback


class SoundfontRealtimeLoadingTests(unittest.TestCase):
    def test_every_profile_disables_dynamic_loading_and_preserves_polyphony(self):
        expected_common = [
            ("synth.gain", 1.0),
            ("synth.reverb.active", 0),
            ("synth.chorus.active", 0),
        ]
        for low_ram, polyphony in ((False, 64), (True, 32)):
            with self.subTest(low_ram=low_ram):
                synth = FakeSynth()
                owner = types.SimpleNamespace(fs=synth)
                method("_configure_native_settings", low_ram=low_ram)(owner)
                self.assertEqual(synth.calls[0], ("synth.dynamic-sample-loading", 0))
                self.assertEqual(
                    synth.calls[1],
                    ("get_setting", "synth.dynamic-sample-loading"),
                )
                self.assertEqual(synth.calls[2], ("synth.polyphony", polyphony))
                self.assertEqual(synth.calls[3:], expected_common)

    def test_configuration_failure_is_truthful_and_stops_before_other_settings(self):
        synth = FakeSynth(fail_dynamic=True)
        owner = types.SimpleNamespace(fs=synth)
        with self.assertRaisesRegex(RuntimeError, "full-bank residency"):
            method("_configure_native_settings", low_ram=True)(owner)
        self.assertEqual(synth.calls, [("synth.dynamic-sample-loading", 0)])

    def test_missing_invalid_or_failed_readback_stops_before_other_settings(self):
        cases = (
            FakeSynth(readback=None),
            FakeSynth(readback=1),
            FakeSynth(readback=False),
            FakeSynth(fail_readback=True),
            types.SimpleNamespace(setting=lambda *_: None),
        )
        for synth in cases:
            with self.subTest(synth=synth):
                owner = types.SimpleNamespace(fs=synth)
                with self.assertRaisesRegex(RuntimeError, "full-bank residency"):
                    method("_configure_native_settings", low_ram=True)(owner)
                calls = getattr(synth, "calls", None)
                if calls is not None:
                    self.assertEqual(
                        calls[:2],
                        [("synth.dynamic-sample-loading", 0),
                         ("get_setting", "synth.dynamic-sample-loading")],
                    )
                    self.assertFalse(any(name == "synth.polyphony"
                                         for name, _ in calls))

    def test_start_configures_residency_before_any_sfload(self):
        start = next(item for item in PLAYER.body
                     if isinstance(item, ast.FunctionDef) and item.name == "start")
        configure = [node for node in ast.walk(start)
                     if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute)
                     and node.func.attr == "_configure_native_settings"]
        loads = [node for node in ast.walk(start)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "sfload"]
        load_one_calls = [node for node in ast.walk(start)
                          if isinstance(node, ast.Call)
                          and isinstance(node.func, ast.Name)
                          and node.func.id == "_load_one"]
        self.assertEqual(len(configure), 1)
        self.assertGreaterEqual(len(loads), 1)
        self.assertGreaterEqual(len(load_one_calls), 1)
        self.assertLess(configure[0].lineno, min(node.lineno for node in loads))
        self.assertLess(configure[0].lineno,
                        min(node.lineno for node in load_one_calls))

    def test_no_path_enables_dynamic_sample_loading(self):
        dynamic_calls = []
        for node in ast.walk(PLAYER):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "setting" and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "synth.dynamic-sample-loading"):
                dynamic_calls.append(node)
        self.assertEqual(len(dynamic_calls), 1)
        self.assertIsInstance(dynamic_calls[0].args[1], ast.Constant)
        self.assertEqual(dynamic_calls[0].args[1].value, 0)


if __name__ == "__main__":
    unittest.main()
