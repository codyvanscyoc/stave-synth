"""Regression coverage for render-path NumPy/ctypes pointer ownership."""
import ast
import ctypes
import gc
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

import numpy as np


ROOT = Path(__file__).resolve().parents[1]


def _method(tree, class_name, method_name):
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == class_name)
    return next(node for node in cls.body
                if isinstance(node, ast.FunctionDef) and node.name == method_name)


class _ArrayName(ast.NodeTransformer):
    """Retarget a production ``something.ctypes.data`` expression for eval."""

    def visit_Attribute(self, node):
        node = self.generic_visit(node)
        if (node.attr == "data" and isinstance(node.value, ast.Attribute)
                and node.value.attr == "ctypes"):
            node.value.value = ast.Name(id="array", ctx=ast.Load())
        return node


def _hot_cast_expressions():
    jack_tree = ast.parse((ROOT / "stave_synth/jack_engine.py").read_text())
    synth_tree = ast.parse((ROOT / "stave_synth/synth_engine.py").read_text())
    regions = (
        _method(jack_tree, "LookaheadLimiter", "process_inplace"),
        _method(jack_tree, "JackEngine", "_render_loop"),
        next(node for node in synth_tree.body
             if isinstance(node, ast.FunctionDef) and node.name == "_biquad_run"),
        next(node for node in synth_tree.body
             if isinstance(node, ast.FunctionDef) and node.name == "_onepole_run"),
    )
    expressions = []
    for region in regions:
        for node in ast.walk(region):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "ctypes"
                    and node.func.attr == "cast"
                    and isinstance(node.args[0], ast.Attribute)
                    and node.args[0].attr == "data"):
                expression = _ArrayName().visit(ast.Expression(body=node))
                expressions.append(ast.fix_missing_locations(expression))
    return expressions


class RenderPointerCycleTests(unittest.TestCase):
    def test_six_production_casts_create_no_collectible_cycles(self):
        expressions = _hot_cast_expressions()
        self.assertEqual(len(expressions), 6)
        encoded = [ast.unparse(expression.body) for expression in expressions]
        script = """
import ctypes, gc, json, numpy as np
_PD = ctypes.POINTER(ctypes.c_double)
array = np.zeros(512, dtype=np.float64)
expressions = json.loads(%r)
compiled = [compile(expr, '<production-pointer>', 'eval') for expr in expressions]
gc.collect()
gc.disable()
for code in compiled:
    for _ in range(2000):
        pointer = eval(code)
del pointer
new_collected = gc.collect()
for _ in range(2000):
    pointer = array.ctypes.data_as(_PD)
del pointer
old_collected = gc.collect()
print(json.dumps({'new': new_collected, 'old': old_collected}))
""" % json.dumps(encoded)
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, text=True,
            capture_output=True, check=True, timeout=10,
        )
        counts = json.loads(result.stdout)
        self.assertEqual(counts["new"], 0)
        self.assertEqual(counts["old"], 4000)

    def test_cast_preserves_address_type_and_array_for_synchronous_call(self):
        array = np.arange(8, dtype=np.float64)
        pointer_type = ctypes.POINTER(ctypes.c_double)
        pointer = ctypes.cast(array.ctypes.data, pointer_type)
        self.assertIs(type(pointer), pointer_type)
        self.assertEqual(ctypes.addressof(pointer.contents), array.ctypes.data)
        self.assertEqual([pointer[i] for i in range(array.size)], array.tolist())

    def test_production_biquad_cast_reaches_synchronous_native_kernel(self):
        pointer_type = ctypes.POINTER(ctypes.c_double)
        callback_type = ctypes.CFUNCTYPE(
            None, pointer_type, pointer_type, ctypes.c_int,
            pointer_type, pointer_type, pointer_type,
        )

        @callback_type
        def bridge_biquad(x, y, n, b, a, zi):
            z0, z1 = zi[0], zi[1]
            for i in range(n):
                xi = x[i]
                yi = b[0] * xi + z0
                z0 = z1 + b[1] * xi - a[1] * yi
                z1 = b[2] * xi - a[2] * yi
                y[i] = yi
            zi[0], zi[1] = z0, z1

        tree = ast.parse((ROOT / "stave_synth/synth_engine.py").read_text())
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_biquad_run")
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        namespace = {
            "np": np, "ctypes": ctypes, "_PD": pointer_type,
            "_BIQUAD_C": bridge_biquad,
        }
        exec(compile(module, "synth_engine.py", "exec"), namespace)

        flt = SimpleNamespace(
            b=np.array([0.5, 0.25, 0.125]),
            a=np.array([1.0, -0.1, 0.05]),
            zi=np.array([0.0, 0.0]),
        )
        samples = np.array([1.0, 0.0, -0.5, 0.25], dtype=np.float64)
        actual = namespace["_biquad_run"](flt, samples).copy()
        np.testing.assert_allclose(
            actual,
            np.array([0.5, 0.3, -0.12, -0.027]),
            rtol=0.0, atol=1e-15,
        )
        np.testing.assert_allclose(flt.zi, np.array([0.0033, 0.0326]),
                                   rtol=0.0, atol=1e-15)


if __name__ == "__main__":
    unittest.main()
