"""Faust-native port of the render-skeleton "pad bus" (Phases 1 + 2 + 4).

Consumes the Faust osc bank's 5-channel block (osc1 L/R, osc2 L/R,
shimmer_mono) and produces 9 channels:

    [0] dry_L    [1] dry_R    — Haas + filter routing + shared 12/24 dB
                                lowpass + comp + indep per-OSC filters +
                                shared highpass; on MERGED blocks also the
                                Phase 4 LFO amp/pan block ramps
                                (push_lfo_block / read_lfo_states —
                                exact ×1.0 pass-through otherwise)
    [2] send_L   [3] send_R   — per-OSC reverb-send weighted sum through
                                the _rev_send_filter_* mirror (slow path)
    [4] bypass_L [5] bypass_R — reserved; always zero (the FX bypass
                                carve is post-LFO + data-dependent,
                                stays in Python)
    [6] shim     [7] cloud_L  [8] cloud_R
                              — Phase 2 shimmer chain: HP split × smoothed
                                mix (mono), and the CLOUD multi-tap wet
                                already ×shimmer_send. The engine adds [6]
                                to both reverb_in channels, then [7]/[8] —
                                same add order as the Python chain.

Python owns ALL parameter smoothing (log-space cutoff ramps, f_pos zones);
this wrapper just pushes the per-block values verbatim — see pad_bus.dsp.

Built with -double + -DFAUSTFLOAT=double (faust/build.sh): double zones
and double I/O, so the engine's float64 blocks pass through zero-copy —
no float32 boundary scratch, and the pole-near-unity biquads hold the
~1e-6 parity bar (they can't in float32).
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from cffi import FFI

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent.parent / "faust"
_LIB = _HERE / "libstave_pad_bus.so"

_ffi = FFI()
# NOTE: FAUSTFLOAT == double for this module (build.sh passes
# -DFAUSTFLOAT=double), so every zone/buffer pointer below is double*.
_ffi.cdef("""
typedef struct StavePadBus StavePadBus;

typedef void (*openBoxFn)(void* ui, const char* label);
typedef void (*closeBoxFn)(void* ui);
typedef void (*addBtnFn)(void* ui, const char* label, double* zone);
typedef void (*addSliderFn)(void* ui, const char* label, double* zone,
                            double init, double min, double max, double step);
typedef void (*addBarFn)(void* ui, const char* label, double* zone,
                         double min, double max);
typedef void (*addSFFn)(void* ui, const char* label, const char* url, void** sf);
typedef void (*declareFn)(void* ui, double* zone, const char* key, const char* value);

typedef struct {
    void* uiInterface;
    openBoxFn openTabBox;
    openBoxFn openHorizontalBox;
    openBoxFn openVerticalBox;
    closeBoxFn closeBox;
    addBtnFn addButton;
    addBtnFn addCheckButton;
    addSliderFn addVerticalSlider;
    addSliderFn addHorizontalSlider;
    addSliderFn addNumEntry;
    addBarFn addHorizontalBargraph;
    addBarFn addVerticalBargraph;
    addSFFn addSoundfile;
    declareFn declare;
} UIGlue;

StavePadBus* newStavePadBus(void);
void deleteStavePadBus(StavePadBus*);
void initStavePadBus(StavePadBus*, int sample_rate);
void instanceClearStavePadBus(StavePadBus*);
void buildUserInterfaceStavePadBus(StavePadBus*, UIGlue* ui);
void computeStavePadBus(StavePadBus*, int count, double** inputs, double** outputs);
""")

try:
    _lib = _ffi.dlopen(str(_LIB))
except OSError as e:
    raise RuntimeError(f"Failed to load {_LIB}: {e}. Run faust/build.sh.")

# Haas delay-line ceiling compiled into pad_bus.dsp (HAAS_MAX = 4096).
_HAAS_MAX = 4095

_N_OUT = 9


class FaustPadBus:
    """Phase 1+2 render-skeleton bus: 5-in → 9-out, per-block zone push."""

    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = int(sample_rate)
        self._dsp = _lib.newStavePadBus()
        if self._dsp == _ffi.NULL:
            raise RuntimeError("newStavePadBus returned NULL")
        _lib.initStavePadBus(self._dsp, self.sample_rate)

        self._zones: dict[str, _ffi.CData] = {}
        self._keepalive = _install_ui_callbacks(self._dsp, self._zones)
        self._push_static_zones()

        # Phase 4: monotonically bumped per merged block — arms the .dsp's
        # block-start detector (ramp index reset + smoother state load).
        # Survives clear() (the zone resets to 0, so the next bump differs
        # and the detector still fires).
        self._lfo_blk_ctr = 0.0

        # Persistent float64 out buffer + cached FFI pointers (zero-alloc
        # render rule). Input pointers are cached against the caller's
        # buffer base address — the osc bank's (5, n) block is persistent,
        # so the hot path re-derives nothing.
        self._buf_n = 0
        self._out = np.empty((_N_OUT, 0), dtype=np.float64)
        self._in_base = -1
        self._in_ptrs = _ffi.new("double*[5]")
        self._out_ptrs = _ffi.new(f"double*[{_N_OUT}]")

    def _push_static_zones(self):
        """Init-time zones: CLOUD ring length + tap offsets, computed with
        the EXACT Python expressions from synth_engine.__init__ (float
        truncation matters: int(0.289 * 96000) == 27743). Written once at
        construction and re-pushed by clear(), never per block."""
        z = self._zones
        sr = self.sample_rate
        z["shim_ring_len"][0] = float(int(0.6 * sr))
        for name, sec in (("shim_tap_l1", 0.130), ("shim_tap_l2", 0.247),
                          ("shim_tap_l3", 0.363), ("shim_tap_l4", 0.481),
                          ("shim_tap_r1", 0.173), ("shim_tap_r2", 0.289),
                          ("shim_tap_r3", 0.405), ("shim_tap_r4", 0.523)):
            z[name][0] = float(int(sec * sr))

    def __del__(self):
        try:
            if getattr(self, "_dsp", None):
                _lib.deleteStavePadBus(self._dsp)
                self._dsp = None
        except Exception:
            pass

    def clear(self):
        """Flush filter + Haas + shimmer state (panic/freeze only — never
        on param changes; see the zi-reset rule).

        Full re-init, not just instanceClear: the CLOUD rwtable is filled
        in instanceConstants (Faust codegen puts table init there, NOT in
        instanceClear), so instanceClear alone would leave up to 0.6 s of
        shimmer material to replay after panic. init also resets every
        zone to its default, so the static zones are re-pushed here; the
        per-block zones are rewritten by set_block_params before the next
        compute."""
        _lib.initStavePadBus(self._dsp, self.sample_rate)
        self._push_static_zones()

    def set_block_params(self, *,
                         cutoff_hz: float, resonance: float, slope24: bool,
                         f_pos: float,
                         osc1_filter_enabled: bool, osc2_filter_enabled: bool,
                         osc1_indep_cutoff: float, osc2_indep_cutoff: float,
                         osc1_f_pos: float, osc2_f_pos: float,
                         hp_on: bool, hp_cutoff: float,
                         osc1_send: float, osc2_send: float,
                         send_filter_active: bool,
                         haas_on: bool, haas_samps: int,
                         osc2_audible: bool,
                         shimmer_active: bool, shimmer_hp_hz: float,
                         shimmer_mix_cur: float, shimmer_send: float):
        """Write all per-block zones at once. Values arrive exactly as the
        Python path would consume them (cutoffs already smoothed, f_pos
        zones already computed, sends already bypass-zeroed) — no extra
        clamping here beyond the Haas line bound; the biquad clamps live
        in pad_bus.dsp, mirroring BiquadLowpass.set_params."""
        z = self._zones
        z["cutoff_hz"][0] = float(cutoff_hz)
        z["filter_resonance"][0] = float(resonance)
        z["filter_slope24"][0] = 1.0 if slope24 else 0.0
        z["f_pos"][0] = float(f_pos)
        z["osc1_filter_enabled"][0] = 1.0 if osc1_filter_enabled else 0.0
        z["osc2_filter_enabled"][0] = 1.0 if osc2_filter_enabled else 0.0
        z["osc1_indep_cutoff"][0] = float(osc1_indep_cutoff)
        z["osc2_indep_cutoff"][0] = float(osc2_indep_cutoff)
        z["osc1_f_pos"][0] = float(osc1_f_pos)
        z["osc2_f_pos"][0] = float(osc2_f_pos)
        z["filter_highpass_on"][0] = 1.0 if hp_on else 0.0
        z["filter_highpass_hz"][0] = float(hp_cutoff)
        z["osc1_reverb_send"][0] = float(osc1_send)
        z["osc2_reverb_send"][0] = float(osc2_send)
        z["send_filter_active"][0] = 1.0 if send_filter_active else 0.0
        z["haas_on"][0] = 1.0 if haas_on else 0.0
        z["haas_delay_samps"][0] = float(max(0, min(_HAAS_MAX, int(haas_samps))))
        z["osc2_audible"][0] = 1.0 if osc2_audible else 0.0
        # Phase 2 shimmer chain. shimmer_active mirrors Python's
        # (render_shimmer and voice_idx > 0) gate; mix_cur arrives already
        # block-smoothed by the engine (it owns the ~80 ms one-pole and
        # only advances it on active blocks).
        z["shimmer_active"][0] = 1.0 if shimmer_active else 0.0
        z["shimmer_hp_hz"][0] = float(shimmer_hp_hz)
        z["shimmer_mix_cur"][0] = float(shimmer_mix_cur)
        z["shimmer_send"][0] = float(shimmer_send)

    def push_lfo_block(self, *, n_samples: int,
                       en1: bool, amp1: bool, d1: float,
                       a1_start: float, a1_end: float,
                       b1_start: float, b1_end: float,
                       sm1: float, st1_a: float, st1_b: float,
                       en2: bool, amp2: bool, d2: float,
                       a2_start: float, a2_end: float,
                       b2_start: float, b2_end: float,
                       sm2: float, st2_a: float, st2_b: float):
        """Phase 4 (merged path only): per-block LFO amp/pan ramp zones.

        Python owns every scalar here — the LFO oscillators, ramp endpoints
        (previous block end → new end), depth (motion-mix × cap applied),
        the one-pole coefficient (0.0 == the <=0.001 passthrough, exact)
        and the smoother STATE, which the .dsp loads at block start and
        exports at block end (read_lfo_states). The counter bump arms the
        block-start detector. The two-call path never calls this, so the
        .dsp's LFO section stays at its exact-identity defaults there."""
        z = self._zones
        self._lfo_blk_ctr += 1.0
        if self._lfo_blk_ctr > 1.0e9:
            self._lfo_blk_ctr = 1.0
        z["lfo_blk_ctr"][0] = self._lfo_blk_ctr
        if n_samples > 1:
            z["lfo_ramp_step"][0] = 1.0 / (n_samples - 1)
            z["lfo_last_i"][0] = float(n_samples - 1)
        else:
            # np.linspace(0, 1, 1) == [0.0] — keep t at 0·step = 0.
            z["lfo_ramp_step"][0] = 0.0
            z["lfo_last_i"][0] = 1.0e9
        z["lfo1_en"][0] = 1.0 if en1 else 0.0
        z["lfo1_is_amp"][0] = 1.0 if amp1 else 0.0
        z["lfo1_depth"][0] = float(d1)
        z["lfo1_a_start"][0] = float(a1_start)
        z["lfo1_a_end"][0] = float(a1_end)
        z["lfo1_b_start"][0] = float(b1_start)
        z["lfo1_b_end"][0] = float(b1_end)
        z["lfo1_smooth_coef"][0] = float(sm1)
        z["lfo1_state_a"][0] = float(st1_a)
        z["lfo1_state_b"][0] = float(st1_b)
        z["lfo2_en"][0] = 1.0 if en2 else 0.0
        z["lfo2_is_amp"][0] = 1.0 if amp2 else 0.0
        z["lfo2_depth"][0] = float(d2)
        z["lfo2_a_start"][0] = float(a2_start)
        z["lfo2_a_end"][0] = float(a2_end)
        z["lfo2_b_start"][0] = float(b2_start)
        z["lfo2_b_end"][0] = float(b2_end)
        z["lfo2_smooth_coef"][0] = float(sm2)
        z["lfo2_state_a"][0] = float(st2_a)
        z["lfo2_state_b"][0] = float(st2_b)

    def read_lfo_states(self, which: int) -> tuple[float, float]:
        """Block-end smoother states (a, b) for one LFO — the bargraph
        zones hold the last sample written by the compute that just ran.
        Call only for LFOs whose ramp Faust actually applied this block
        (mirrors which _smooth_one_pole calls Python would have made)."""
        z = self._zones
        p = "lfo1" if int(which) == 1 else "lfo2"
        return float(z[p + "_state_a_out"][0]), float(z[p + "_state_b_out"][0])

    def process(self, inputs: np.ndarray) -> np.ndarray:
        """Run one block. `inputs` is the osc bank's (5, n) float64
        C-contiguous block. Returns the persistent (9, n) float64 output
        buffer — consume within the block."""
        n = int(inputs.shape[1])
        if n == 0:
            return np.zeros((_N_OUT, 0), dtype=np.float64)
        if inputs.dtype != np.float64 or not inputs.flags["C_CONTIGUOUS"]:
            # Cold path — the engine always passes the persistent bank block.
            inputs = np.ascontiguousarray(inputs, dtype=np.float64)

        if n != self._buf_n:
            self._out = np.empty((_N_OUT, n), dtype=np.float64)
            for i in range(_N_OUT):
                self._out_ptrs[i] = _ffi.cast("double*", self._out[i].ctypes.data)
            self._buf_n = n
            self._in_base = -1  # row stride changed → re-derive input ptrs

        base = inputs.ctypes.data
        if base != self._in_base:
            row = inputs.strides[0]
            for i in range(5):
                self._in_ptrs[i] = _ffi.cast("double*", base + i * row)
            self._in_base = base

        _lib.computeStavePadBus(self._dsp, n, self._in_ptrs, self._out_ptrs)
        return self._out


# ────────────────────────────────────────────────────────────────────────
# UI callback glue — same pattern as faust_ping_pong.py, double zones.
# ────────────────────────────────────────────────────────────────────────

def _install_ui_callbacks(dsp, zones: dict):
    keepalive = []

    @_ffi.callback("void(void*, const char*)")
    def _open(ui, label):  # noqa: ARG001
        pass

    @_ffi.callback("void(void*)")
    def _close(ui):  # noqa: ARG001
        pass

    @_ffi.callback("void(void*, const char*, double*)")
    def _button(ui, label, zone):  # noqa: ARG001
        zones[_ffi.string(label).decode()] = zone

    @_ffi.callback("void(void*, const char*, double*, double, double, double, double)")
    def _slider(ui, label, zone, init, lo, hi, step):  # noqa: ARG001
        zones[_ffi.string(label).decode()] = zone

    @_ffi.callback("void(void*, const char*, double*, double, double)")
    def _bar(ui, label, zone, lo, hi):  # noqa: ARG001
        # Bargraphs are read-back zones (Phase 4 LFO smoother block-end
        # states) — record them alongside the sliders.
        zones[_ffi.string(label).decode()] = zone

    @_ffi.callback("void(void*, const char*, const char*, void**)")
    def _sf(ui, label, url, sf):  # noqa: ARG001
        pass

    @_ffi.callback("void(void*, double*, const char*, const char*)")
    def _decl(ui, zone, key, value):  # noqa: ARG001
        pass

    glue = _ffi.new("UIGlue*")
    glue.uiInterface = _ffi.NULL
    glue.openTabBox = _open
    glue.openHorizontalBox = _open
    glue.openVerticalBox = _open
    glue.closeBox = _close
    glue.addButton = _button
    glue.addCheckButton = _button
    glue.addVerticalSlider = _slider
    glue.addHorizontalSlider = _slider
    glue.addNumEntry = _slider
    glue.addHorizontalBargraph = _bar
    glue.addVerticalBargraph = _bar
    glue.addSoundfile = _sf
    glue.declare = _decl

    _lib.buildUserInterfaceStavePadBus(dsp, glue)
    keepalive.extend([_open, _close, _button, _slider, _bar, _sf, _decl, glue])
    return keepalive
