"""Device-free regressions for failed Faust DSP allocation."""

import ast
import types
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]


def extract_init(relative, class_name, namespace):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == class_name)
    method = next(node for node in cls.body
                  if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    ast.fix_missing_locations(method)
    exec(compile(ast.Module(body=[method], type_ignores=[]), relative, "exec"), namespace)
    return namespace["__init__"]


class NullLibrary:
    def __init__(self, constructor, initializer):
        self.constructor = constructor
        self.initializer = initializer
        self.init_calls = 0

    def __getattr__(self, name):
        if name == self.constructor:
            return lambda: None
        if name == self.initializer:
            def init(*args):
                self.init_calls += 1
            return init
        raise AttributeError(name)


class NativeAllocationGuardTests(unittest.TestCase):
    CASES = (
        ("stave_synth/faust_sympathetic.py", "FaustSympathetic",
         "newStaveSympathetic", "initStaveSympathetic"),
        ("stave_synth/faust_master_fx.py", "FaustMasterFX",
         "newStaveMasterFX", "initStaveMasterFX"),
        ("stave_synth/faust_bus_comp.py", "FaustBusComp",
         "newStaveBusComp", "initStaveBusComp"),
        ("stave_synth/faust_organ.py", "FaustOrganEngine",
         "newStaveOrgan", "initStaveOrgan"),
        ("stave_synth/faust_plate.py", "FaustPlate",
         "newStavePlate", "initStavePlate"),
        ("stave_synth/faust_drone.py", "FaustDrone",
         "newStaveDrone", "initStaveDrone"),
        ("stave_synth/faust_piano_room.py", "FaustPianoRoom",
         "newStavePianoRoom", "initStavePianoRoom"),
    )

    def test_null_allocation_raises_before_native_init(self):
        for relative, class_name, constructor, initializer in self.CASES:
            with self.subTest(wrapper=relative):
                library = NullLibrary(constructor, initializer)
                namespace = {
                    "_lib": library,
                    "_ffi": types.SimpleNamespace(NULL=None),
                    "np": np,
                    "SAMPLE_RATE": 48000,
                    "ORGAN_PRESETS": {"mellow": [0] * 9},
                    "_generate_click_sample": lambda rate: np.zeros(1),
                }
                init = extract_init(relative, class_name, namespace)
                owner = types.SimpleNamespace()
                with self.assertRaisesRegex(
                        RuntimeError, rf"{constructor}\(\) returned NULL"):
                    init(owner)
                self.assertEqual(library.init_calls, 0)
                self.assertIsNone(owner._dsp)


if __name__ == "__main__":
    unittest.main()
