"""Device-free parity checks for gating redundant Python oscillator work.

Executes the actual render preparation/voice loop extracted from source, with
fake native-bank calls and deterministic envelopes/randomness. It stops before
filters, effects and JACK: these tests prove unchanged bank inputs and fallback
oscillator samples/state, not full native audio parity or measured Pi speedup.
No application config, SciPy, CFFI, native library or audio device is imported.
"""

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "stave_synth/synth_engine.py"


class NumpyProxy:
    """Private RNG and call counters; never changes process-global NumPy."""

    def __init__(self):
        self.random = np.random.RandomState(1741)
        self.clip_calls = 0

    def clip(self, *args, **kwargs):
        self.clip_calls += 1
        return np.clip(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(np, name)


class LegacyUngatedPreparation(ast.NodeTransformer):
    """Counterfactual old execution: evaluate the three pure prep groups.

    All formulas/order remain the actual source. Only the newly introduced
    guards are removed; existing waveform/routing guards are never removed.
    This isolates the behavioral effect of gating from unrelated render code.
    """

    def __init__(self):
        self.ungated = 0

    def visit_If(self, node):
        guarded = (isinstance(node.test, ast.UnaryOp)
                   and isinstance(node.test.op, ast.Not)
                   and isinstance(node.test.operand, ast.Name)
                   and node.test.operand.id == "use_faust")
        assigned = {target.id for item in ast.walk(node)
                    if isinstance(item, ast.Assign) for target in item.targets
                    if isinstance(target, ast.Name)}
        # These identify only the three optimization groups, not their users.
        if guarded and assigned & {"detune_mult", "base_inc", "scale_common"}:
            self.ungated += 1
            return node.body
        return self.generic_visit(node)


def load_preparation(*, legacy=False):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == "SynthEngine")
    method = next(node for node in cls.body
                  if isinstance(node, ast.FunctionDef) and node.name == "_render_locked")
    voice_loop = next(index for index, node in enumerate(method.body)
                      if isinstance(node, ast.For)
                      and isinstance(node.target, ast.Name)
                      and node.target.id == "voice")
    # Include the immediately following native gate clears/compute/routing.
    native_compute = method.body[voice_loop + 1]
    assert isinstance(native_compute, ast.If)
    assert isinstance(native_compute.test, ast.Name)
    assert native_compute.test.id == "use_faust"
    method.body = method.body[:voice_loop + 2]
    if legacy:
        transform = LegacyUngatedPreparation()
        method = transform.visit(method)
        if transform.ungated != 3:
            raise AssertionError("Expected exactly three pure preparation guards")
    method.body.append(ast.Return(value=ast.Call(
        func=ast.Name(id="locals", ctx=ast.Load()), args=[], keywords=[])))
    helpers = [node for node in tree.body
               if isinstance(node, ast.FunctionDef)
               and node.name in {"blend_to_amplitude", "_poly_blep", "generate_waveform"}]
    proxy = NumpyProxy()
    namespace = {"np": proxy, "TWO_PI": 2.0 * np.pi, "BLEND_DB_RANGE": 24.0,
                 "ADSREnvelope": SimpleNamespace(SUSTAIN="sustain")}
    module = ast.fix_missing_locations(ast.Module(body=helpers + [method], type_ignores=[]))
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace["_render_locked"], proxy


class Envelope:
    def __init__(self, stage, level):
        self.stage = stage
        self.config = SimpleNamespace(sustain_percent=level * 100.0)
        self.level = level
        self.samples_processed = 0

    def process(self, count):
        direction = -1 if self.stage == "release" else 1
        result = self.level + direction * np.arange(1, count + 1) / 100000.0
        self.level = float(result[-1])
        self.samples_processed += count
        return result


class Voice:
    def __init__(self, note, slot, unison, stages, active=True):
        self.note = note
        self.faust_slot = slot
        self.velocity = 0.62 + slot * 0.03
        self.osc1_weight, self.osc2_weight, self.shimmer_weight = 0.8, 0.65, 0.45
        self.adsr_osc1 = Envelope(stages[0], 0.6)
        self.adsr_osc2 = Envelope(stages[1], 0.4)
        self.drift_val = 0.14 * (slot + 1)
        self.osc1_phases = [0.1 + index * 0.23 for index in range(unison)]
        self.osc2_phases = [0.7 + index * 0.19 for index in range(unison)]
        self.shimmer_phases = [0.3 + index * 0.17 for index in range(unison)]
        self.active = active

    def is_active(self):
        return self.active


class Bank:
    def __init__(self, supported=3):
        self.supported = supported
        self.calls = []

    def supports_unison(self, count):
        self.calls.append(("supports_unison", count))
        return count == self.supported

    def __getattr__(self, name):
        if name not in {"set_osc_params", "set_shimmer_params", "set_lfo_params",
                        "set_voice", "set_shimmer_weight", "clear_voice"}:
            raise AttributeError(name)

        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return record

    def process(self, count):
        self.calls.append(("process", count))
        # A nonzero deterministic stand-in checks native-output routing too.
        return np.arange(5 * count, dtype=np.float64).reshape(5, count) / 10000.0


def make_owner(*, native=True, unison=3, shimmer=True, merged=True,
               filtered=(True, False), muted=False, block=512):
    owner = SimpleNamespace(
        _panic_pending=False, _ensure_buffers=lambda count: None,
        _sample_indices=np.arange(1, block + 1, dtype=np.float64),
        sample_rate=48000, osc1_blend=0.0 if muted else 0.35,
        osc2_blend=0.0 if muted else 0.25,
        _osc1_blend_cur=0.0 if muted else 0.32,
        _osc2_blend_cur=0.0 if muted else 0.23,
        shimmer_enabled=shimmer, shimmer_mix=0.3, shimmer_high=False,
        osc1_filter_enabled=filtered[0], osc2_filter_enabled=filtered[1],
        osc1_waveform="saw", osc2_waveform="triangle",
        unison_spread=0.7, unison_voices=unison, unison_detune=0.035,
        osc_hard_pan=False, osc1_pan=-0.15, osc2_pan=0.15,
        osc1_octave=-1, osc2_octave=1, _pitch_bend_semitones=0.3,
        analog_drift_cents=3.0, _faust_osc_bank=Bank() if native else None,
        _faust_pad_bus=object() if merged else None,
        _faust_merged=object() if merged else None, _faust_nvoices=5,
        motion_mix=0.4, bpm=80.0, _DELAY_DIVISIONS={"quarter": 1.0},
        voices=[Voice(48, 0, unison, ("attack", "sustain")),
                Voice(60, 1, unison, ("sustain", "release")),
                Voice(67, 2, unison, ("release", "attack")),
                Voice(72, 3, unison, ("sustain", "sustain"), active=False)],
    )
    for prefix in ("lfo", "lfo2"):
        for key, value in {"active": True, "poly": True, "depth": 0.3,
                           "target": "amp", "rate_mode": "quarter",
                           "rate_hz": 0.7, "rate_multiplier": 1.0,
                           "shape": "sine"}.items():
            setattr(owner, prefix + "_" + key, value)
    for name in ("_osc1_pre_l", "_osc1_pre_r", "_osc2_pre_l", "_osc2_pre_r",
                 "_shimmer_buf", "_osc2_accum_l", "_osc2_accum_r"):
        setattr(owner, name, np.full(block, 0.123, dtype=np.float64))
    for name in ("_filter_buf", "_osc1_indep_buf", "_osc2_indep_buf"):
        setattr(owner, name, np.full((2, block), 0.123, dtype=np.float64))
    for name in ("_voice_shimmer", "_voice_osc1_l", "_voice_osc1_r",
                 "_voice_osc2_l", "_voice_osc2_r"):
        setattr(owner, name, np.full((5, block), 0.123, dtype=np.float64))
    return owner


class NativeRenderPreparationTests(unittest.TestCase):
    def compare(self, **options):
        current, proxy = load_preparation()
        legacy, legacy_proxy = load_preparation(legacy=True)
        owner, baseline = make_owner(**options), make_owner(**options)
        # Multiple blocks exercise envelope/phase/drift progression, not only
        # a zero-initialized or sustained scalar block.
        for _ in range(4):
            actual = current(owner, options.get("block", 512), False, None, None)
            expected = legacy(baseline, options.get("block", 512), False, None, None)
            for key in ("use_faust", "use_pad_bus", "use_merged", "voice_idx",
                        "render_osc1", "render_osc2", "render_shimmer", "skip_voices"):
                self.assertEqual(actual[key], expected[key], key)
            for name in ("_osc1_pre_l", "_osc1_pre_r", "_osc2_accum_l",
                         "_osc2_accum_r", "_filter_buf", "_osc1_indep_buf",
                         "_osc2_indep_buf", "_shimmer_buf"):
                np.testing.assert_array_equal(getattr(owner, name), getattr(baseline, name))
            for voice, reference in zip(owner.voices, baseline.voices):
                for name in ("drift_val", "osc1_phases", "osc2_phases", "shimmer_phases"):
                    self.assertEqual(getattr(voice, name), getattr(reference, name), name)
                for name in ("adsr_osc1", "adsr_osc2"):
                    self.assertEqual(vars(getattr(voice, name))["samples_processed"],
                                     vars(getattr(reference, name))["samples_processed"])
                    self.assertEqual(getattr(voice, name).level, getattr(reference, name).level)
            if owner._faust_osc_bank is not None:
                self.assertEqual(owner._faust_osc_bank.calls, baseline._faust_osc_bank.calls)
            # Identical RNG state proves draws/count/order were not changed.
            left, right = proxy.random.get_state(), legacy_proxy.random.get_state()
            self.assertEqual(left[0], right[0])
            np.testing.assert_array_equal(left[1], right[1])
            self.assertEqual(left[2:], right[2:])
        return owner, actual, proxy, legacy_proxy

    def test_native_inputs_envelopes_and_random_draws_unchanged(self):
        for shimmer in (False, True):
            for merged in (False, True):
                with self.subTest(shimmer=shimmer, merged=merged):
                    owner, result, proxy, legacy_proxy = self.compare(shimmer=shimmer, merged=merged)
                    self.assertTrue(result["use_faust"])
                    self.assertEqual(proxy.clip_calls, 0)
                    self.assertEqual(legacy_proxy.clip_calls, 8)
                    # Unused Python scratch is genuinely untouched natively.
                    np.testing.assert_array_equal(owner._voice_shimmer, 0.123)

    def test_fallback_samples_and_state_are_exactly_unchanged(self):
        for unison in (1, 3, 5):
            for shimmer in (False, True):
                for filtered in ((True, True), (False, False), (True, False)):
                    with self.subTest(unison=unison, shimmer=shimmer, filtered=filtered):
                        _, result, proxy, legacy_proxy = self.compare(
                            native=False, unison=unison, shimmer=shimmer, filtered=filtered)
                        self.assertFalse(result["use_faust"])
                        self.assertEqual(proxy.clip_calls, legacy_proxy.clip_calls)

    def test_unsupported_native_unison_preserves_fallback(self):
        for unison in (1, 5):
            with self.subTest(unison=unison):
                owner, result, _, _ = self.compare(unison=unison)
                self.assertFalse(result["use_faust"])
                self.assertEqual(owner._faust_osc_bank.calls, [("supports_unison", unison)] * 4)

    def test_muted_voice_envelopes_keep_advancing(self):
        owner, result, _, _ = self.compare(muted=True, shimmer=False)
        self.assertTrue(result["skip_voices"])
        self.assertFalse(result["use_faust"])
        self.assertEqual(owner.voices[0].adsr_osc1.samples_processed, 4 * 512)
        self.assertEqual(owner.voices[0].drift_val, 0.14)

    def test_shimmer_only_still_uses_native_bank(self):
        _, result, proxy, _ = self.compare(muted=True, shimmer=True)
        self.assertTrue(result["use_faust"])
        self.assertFalse(result["render_osc1"])
        self.assertFalse(result["render_osc2"])
        self.assertEqual(proxy.clip_calls, 0)

    def test_native_then_fallback_clears_its_scratch_before_use(self):
        current, proxy = load_preparation()
        legacy, legacy_proxy = load_preparation(legacy=True)
        owner, baseline = make_owner(), make_owner()
        current(owner, 512, False, None, None)
        legacy(baseline, 512, False, None, None)
        owner._faust_osc_bank = baseline._faust_osc_bank = None
        current(owner, 512, False, None, None)
        legacy(baseline, 512, False, None, None)
        for name in ("_voice_shimmer", "_shimmer_buf", "_filter_buf", "_osc2_accum_l"):
            np.testing.assert_array_equal(getattr(owner, name), getattr(baseline, name))
        self.assertEqual(proxy.clip_calls, 2)
        self.assertEqual(legacy_proxy.clip_calls, 4)

    def test_multiple_block_sizes_preserve_preparation(self):
        for block in (32, 128, 256, 512):
            for native in (False, True):
                with self.subTest(block=block, native=native):
                    self.compare(block=block, native=native)


if __name__ == "__main__":
    unittest.main()
