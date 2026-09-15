"""Differential checks for the inactive per-OSC modulation fast path."""

import ast
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np


SOURCE = Path(__file__).parents[1] / "stave_synth" / "synth_engine.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))


def _method_node(name):
    engine = next(node for node in TREE.body
                  if isinstance(node, ast.ClassDef) and node.name == "SynthEngine")
    return next(node for node in engine.body
                if isinstance(node, ast.FunctionDef) and node.name == name)


def _load_split_predicate():
    node = next(node for node in TREE.body
                if isinstance(node, ast.FunctionDef)
                and node.name == "_osc_modulation_needs_split")
    module = ast.Module(body=[copy.deepcopy(node)], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {}
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace[node.name]


def _load_legacy_split_body():
    """Compile the exact production split body as a small test function."""
    render = _method_node("_render_locked")
    all_recv_if = next(
        node for node in ast.walk(render)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "all_recv"
        and node.orelse
        and isinstance(node.orelse[0], ast.If)
        and isinstance(node.orelse[0].test, ast.Call)
        and isinstance(node.orelse[0].test.func, ast.Name)
        and node.orelse[0].test.func.id == "_osc_modulation_needs_split"
    )
    split_body = copy.deepcopy(all_recv_if.orelse[0].body)
    fn = ast.FunctionDef(
        name="run_split",
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg=name) for name in
                  ("self", "output_l", "output_r", "n_samples", "_osc_amp_pan")],
            kwonlyargs=[], kw_defaults=[], defaults=[], vararg=None, kwarg=None,
        ),
        body=split_body + [ast.Return(value=ast.Tuple(
            elts=[ast.Name(id="output_l", ctx=ast.Load()),
                  ast.Name(id="output_r", ctx=ast.Load())],
            ctx=ast.Load()))],
        decorator_list=[],
    )
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"np": np}
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace["run_split"]


needs_split = _load_split_predicate()
run_legacy_split = _load_legacy_split_body()


def _decision(**overrides):
    values = dict(
        use_faust=True,
        lfo1_depth=0.0, lfo1_target="pan", lfo1_poly=False,
        lfo1_received=False,
        lfo2_depth=0.0, lfo2_target="pan", lfo2_poly=False,
        lfo2_received=False,
    )
    values.update(overrides)
    return needs_split(**values)


class OscModulationIdentityTests(unittest.TestCase):
    def test_inactive_and_non_osc_targets_skip_split(self):
        self.assertFalse(_decision())
        self.assertFalse(_decision(lfo1_depth=0.001, lfo1_received=True))
        self.assertFalse(_decision(
            lfo1_depth=0.7, lfo1_received=False, lfo1_target="pan"))
        self.assertFalse(_decision(
            lfo1_depth=0.7, lfo1_received=True, lfo1_target="filter"))
        self.assertFalse(_decision(
            lfo1_depth=0.7, lfo1_received=True, lfo1_target="bus"))

    def test_actual_python_amp_or_pan_modulation_keeps_split(self):
        self.assertTrue(_decision(
            lfo1_depth=0.00101, lfo1_received=True, lfo1_target="pan"))
        self.assertTrue(_decision(
            lfo1_depth=0.4, lfo1_received=True, lfo1_target="amp",
            use_faust=False, lfo1_poly=True))
        self.assertTrue(_decision(
            lfo2_depth=0.3, lfo2_received=True, lfo2_target="amp"))

    def test_faust_poly_amp_is_not_a_python_split_consumer(self):
        self.assertFalse(_decision(
            lfo1_depth=0.5, lfo1_received=True, lfo1_target="amp",
            use_faust=True, lfo1_poly=True))
        self.assertTrue(_decision(
            lfo1_depth=0.5, lfo1_received=True, lfo1_target="pan",
            use_faust=True, lfo1_poly=True))

    def test_legacy_inactive_split_is_numerically_identity_and_stateless(self):
        rng = np.random.default_rng(1837)
        owner = SimpleNamespace(
            osc1_recv_lfo1=False, osc1_recv_lfo2=False,
            osc2_recv_lfo1=False, osc2_recv_lfo2=False,
            tail_marker=(0.125, -0.25), filter_marker=19,
        )
        owner_before = copy.deepcopy(owner.__dict__)
        for _ in range(32):
            left = rng.normal(size=512)
            right = rng.normal(size=512)
            original_l = left.copy()
            original_r = right.copy()
            pre = [rng.normal(size=512) for _ in range(4)]
            owner._osc1_pre_l, owner._osc1_pre_r = pre[0], pre[1]
            owner._osc2_pre_l, owner._osc2_pre_r = pre[2], pre[3]

            calls = []
            def no_mod(recv1, recv2):
                calls.append((recv1, recv2))
                return None, None, None, None

            got_l, got_r = run_legacy_split(owner, left, right, 512, no_mod)
            np.testing.assert_allclose(got_l, original_l, rtol=2e-16, atol=2e-16)
            np.testing.assert_allclose(got_r, original_r, rtol=2e-16, atol=2e-16)
            self.assertEqual(calls, [(False, False), (False, False)])

        # The optimized path avoids only the algebra above; DSP/RNG owners are
        # neither passed to nor mutated by the decision helper.
        for key in ("tail_marker", "filter_marker"):
            self.assertEqual(owner.__dict__[key], owner_before[key])

    def test_skipping_identity_preserves_upstream_state_and_rng_trajectory(self):
        def trajectory(apply_legacy):
            rng = np.random.default_rng(947)
            state_l, state_r = 0.75, -0.5
            rendered = []
            owner = SimpleNamespace(
                _osc1_pre_l=np.zeros(32), _osc1_pre_r=np.zeros(32),
                _osc2_pre_l=np.zeros(32), _osc2_pre_r=np.zeros(32),
                osc1_recv_lfo1=False, osc1_recv_lfo2=False,
                osc2_recv_lfo1=False, osc2_recv_lfo2=False,
            )
            for _ in range(24):
                excitation = rng.normal(scale=0.01, size=(2, 32))
                left = np.empty(32)
                right = np.empty(32)
                for i in range(32):
                    state_l = state_l * 0.97 + excitation[0, i]
                    state_r = state_r * 0.96 + excitation[1, i]
                    left[i], right[i] = state_l, state_r
                owner._osc1_pre_l[:] = excitation[0]
                owner._osc1_pre_r[:] = excitation[1]
                owner._osc2_pre_l[:] = excitation[1]
                owner._osc2_pre_r[:] = excitation[0]
                if apply_legacy:
                    left, right = run_legacy_split(
                        owner, left, right, 32,
                        lambda _r1, _r2: (None, None, None, None))
                rendered.append(np.stack((left.copy(), right.copy())))
            return np.stack(rendered), (state_l, state_r), rng.random(4)

        legacy_audio, legacy_state, legacy_rng = trajectory(True)
        gated_audio, gated_state, gated_rng = trajectory(False)
        np.testing.assert_allclose(legacy_audio, gated_audio,
                                   rtol=2e-16, atol=2e-16)
        self.assertEqual(legacy_state, gated_state)
        np.testing.assert_array_equal(legacy_rng, gated_rng)

    def test_exact_active_split_body_still_applies_routed_modulation(self):
        owner = SimpleNamespace(
            _osc1_pre_l=np.ones(8), _osc1_pre_r=np.ones(8),
            _osc2_pre_l=np.ones(8), _osc2_pre_r=np.ones(8),
            osc1_recv_lfo1=True, osc1_recv_lfo2=False,
            osc2_recv_lfo1=False, osc2_recv_lfo2=True,
        )
        calls = []
        def routed_mod(recv1, recv2):
            calls.append((recv1, recv2))
            if recv1:
                return np.full(8, 0.5), np.full(8, 0.5), None, None
            return None, None, np.full(8, 0.2), np.full(8, -0.2)

        left, right = run_legacy_split(
            owner, np.ones(8), np.ones(8), 8, routed_mod)
        self.assertEqual(calls, [(True, False), (False, True)])
        np.testing.assert_allclose(left, 0.85)
        np.testing.assert_allclose(right, 0.65)


if __name__ == "__main__":
    unittest.main()
