"""Faust-native 16-voice oscillator bank.

Public API mirrors what SynthEngine needs from its oscillator-rendering
code: set per-voice freq + gate, set global osc params, call process() to
produce a stereo block. Voice allocation / ADSR / note_on still live in
Python; Faust owns only wave gen + unison + per-osc pan + blend.

This is Phase 3a (first integration-ready iteration). Unison is fixed at
3 copies. Variable unison comes in Phase 3b.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from cffi import FFI

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent.parent / "faust"
_LIB = _HERE / "libstave_osc_bank.so"

NVOICES = 16  # must match NVOICES in osc_bank.dsp

# Waveform index: matches DEFAULT_STATE / generate_waveform names
_WF_INDEX = {"sine": 0, "square": 1, "saw": 2, "triangle": 3, "saturated": 4}


_ffi = FFI()
_ffi.cdef("""
typedef struct StaveOscBank StaveOscBank;

typedef void (*openBoxFn)(void* ui, const char* label);
typedef void (*closeBoxFn)(void* ui);
typedef void (*addBtnFn)(void* ui, const char* label, float* zone);
typedef void (*addSliderFn)(void* ui, const char* label, float* zone,
                            float init, float min, float max, float step);
typedef void (*addBarFn)(void* ui, const char* label, float* zone, float min, float max);
typedef void (*addSFFn)(void* ui, const char* label, const char* url, void** sf);
typedef void (*declareFn)(void* ui, float* zone, const char* key, const char* value);

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

StaveOscBank* newStaveOscBank(void);
void deleteStaveOscBank(StaveOscBank*);
void initStaveOscBank(StaveOscBank*, int sample_rate);
void instanceClearStaveOscBank(StaveOscBank*);
void buildUserInterfaceStaveOscBank(StaveOscBank*, UIGlue* ui);
void computeStaveOscBank(StaveOscBank*, int count, float** inputs, float** outputs);
""")

try:
    _lib = _ffi.dlopen(str(_LIB))
except OSError as e:
    raise RuntimeError(f"Failed to load {_LIB}: {e}. Run faust/build.sh.")


class FaustOscBank:
    """16-voice Faust oscillator bank. Pre-computed stereo output per block."""

    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = int(sample_rate)
        self._dsp = _lib.newStaveOscBank()
        if self._dsp == _ffi.NULL:
            raise RuntimeError("newStaveOscBank returned NULL")
        _lib.initStaveOscBank(self._dsp, self.sample_rate)

        self._zones: dict[str, _ffi.CData] = {}
        self._keepalive = _install_ui_callbacks(self._dsp, self._zones)

        # Convenient flat arrays of per-voice zone pointers — avoid dict
        # lookup on the hot path.
        self._freq_zones = [self._zones[f"freq_v{i}"] for i in range(NVOICES)]
        self._gate_zones = [self._zones[f"gate_v{i}"] for i in range(NVOICES)]
        self._osc1_phase_zones = [self._zones[f"osc1_phase_v{i}"] for i in range(NVOICES)]
        self._osc2_phase_zones = [self._zones[f"osc2_phase_v{i}"] for i in range(NVOICES)]

        # Scratch — Faust emits 6 channels: osc1_L/R, osc2_L/R, shimmer_mono, drone_mono
        self._buf_n = 0
        self._out_osc1_l = np.empty(0, dtype=np.float32)
        self._out_osc1_r = np.empty(0, dtype=np.float32)
        self._out_osc2_l = np.empty(0, dtype=np.float32)
        self._out_osc2_r = np.empty(0, dtype=np.float32)
        self._out_shimmer = np.empty(0, dtype=np.float32)
        self._out_drone = np.empty(0, dtype=np.float32)
        self._in_ptrs = _ffi.new("float*[0]")  # zero inputs
        self._out_ptrs = _ffi.new("float*[6]")

    def __del__(self):
        try:
            if getattr(self, "_dsp", None):
                _lib.deleteStaveOscBank(self._dsp)
                self._dsp = None
        except Exception:
            pass

    def panic(self):
        _lib.instanceClearStaveOscBank(self._dsp)
        for i in range(NVOICES):
            self._gate_zones[i][0] = 0.0

    # ─── Per-voice setters (hot path — called every block per active voice) ───
    def set_voice(self, slot: int, freq_hz: float, gate_level: float):
        """gate_level already includes ADSR envelope × velocity."""
        self._freq_zones[slot][0] = float(freq_hz)
        self._gate_zones[slot][0] = float(max(0.0, min(1.0, gate_level)))

    def clear_voice(self, slot: int):
        self._gate_zones[slot][0] = 0.0

    def randomize_phase(self, slot: int):
        """Assign fresh random osc1/osc2 phase offsets for a slot — call at
        note_on so osc1 and osc2 of the same note aren't perfectly in-phase
        (which would otherwise cause their fundamentals to sum coherently,
        doubling apparent loudness vs the Python implementation)."""
        self._osc1_phase_zones[slot][0] = float(np.random.uniform(0.0, 1.0))
        self._osc2_phase_zones[slot][0] = float(np.random.uniform(0.0, 1.0))

    # ─── Global osc setters (set when UI changes, not per-block) ───
    def set_osc_params(self,
                       osc1_wf: str, osc2_wf: str,
                       osc1_blend: float, osc2_blend: float,
                       osc1_octave: int, osc2_octave: int,
                       unison_detune: float, unison_spread: float,
                       osc1_pan: float, osc2_pan: float):
        self._zones["osc1_wf"][0] = float(_WF_INDEX.get(osc1_wf, 0))
        self._zones["osc2_wf"][0] = float(_WF_INDEX.get(osc2_wf, 0))
        self._zones["osc1_blend"][0] = float(osc1_blend)
        self._zones["osc2_blend"][0] = float(osc2_blend)
        self._zones["osc1_oct"][0] = float(2.0 ** osc1_octave)
        self._zones["osc2_oct"][0] = float(2.0 ** osc2_octave)
        self._zones["uni_detune"][0] = float(unison_detune)
        self._zones["uni_spread"][0] = float(unison_spread)
        self._zones["osc1_pan"][0] = float(osc1_pan)
        self._zones["osc2_pan"][0] = float(osc2_pan)

    def set_shimmer_params(self, enabled: bool, high: bool):
        """`high`=False → +12 st (2x), True → +24 st (4x)."""
        self._zones["shimmer_enable"][0] = 1.0 if enabled else 0.0
        self._zones["shimmer_mult"][0] = 4.0 if high else 2.0

    def set_drone_params(self, root_freq: float, fifth_freq: float, gain_lvl: float):
        """`gain_lvl` = drone_gain × drone_level × fade_scale (combined Python-side)."""
        self._zones["drone_root_freq"][0] = float(max(0.0, root_freq))
        self._zones["drone_fifth_freq"][0] = float(max(0.0, fifth_freq))
        self._zones["drone_gain_lvl"][0] = float(max(0.0, min(2.0, gain_lvl)))

    # ─── Process ───
    def process(self, n_samples: int) -> np.ndarray:
        """Generate a 6-channel (6, n_samples) float64 block:
             [0] osc1_L  [1] osc1_R  [2] osc2_L  [3] osc2_R
             [4] shimmer_mono  [5] drone_mono"""
        if n_samples == 0:
            return np.zeros((6, 0), dtype=np.float64)

        if n_samples != self._buf_n:
            self._out_osc1_l = np.empty(n_samples, dtype=np.float32)
            self._out_osc1_r = np.empty(n_samples, dtype=np.float32)
            self._out_osc2_l = np.empty(n_samples, dtype=np.float32)
            self._out_osc2_r = np.empty(n_samples, dtype=np.float32)
            self._out_shimmer = np.empty(n_samples, dtype=np.float32)
            self._out_drone = np.empty(n_samples, dtype=np.float32)
            self._out_ptrs[0] = _ffi.cast("float*", self._out_osc1_l.ctypes.data)
            self._out_ptrs[1] = _ffi.cast("float*", self._out_osc1_r.ctypes.data)
            self._out_ptrs[2] = _ffi.cast("float*", self._out_osc2_l.ctypes.data)
            self._out_ptrs[3] = _ffi.cast("float*", self._out_osc2_r.ctypes.data)
            self._out_ptrs[4] = _ffi.cast("float*", self._out_shimmer.ctypes.data)
            self._out_ptrs[5] = _ffi.cast("float*", self._out_drone.ctypes.data)
            self._buf_n = n_samples

        _lib.computeStaveOscBank(self._dsp, n_samples, self._in_ptrs, self._out_ptrs)

        out = np.empty((6, n_samples), dtype=np.float64)
        np.copyto(out[0], self._out_osc1_l, casting="unsafe")
        np.copyto(out[1], self._out_osc1_r, casting="unsafe")
        np.copyto(out[2], self._out_osc2_l, casting="unsafe")
        np.copyto(out[3], self._out_osc2_r, casting="unsafe")
        np.copyto(out[4], self._out_shimmer, casting="unsafe")
        np.copyto(out[5], self._out_drone, casting="unsafe")
        return out


# ────────────────────────────────────────────────────────────────────────
# UI callback glue (same pattern as faust_reverb.py / faust_ping_pong.py)
# ────────────────────────────────────────────────────────────────────────

def _install_ui_callbacks(dsp, zones: dict):
    keepalive = []

    @_ffi.callback("void(void*, const char*)")
    def _open(u, l): pass  # noqa: E701

    @_ffi.callback("void(void*)")
    def _close(u): pass  # noqa: E701

    @_ffi.callback("void(void*, const char*, float*)")
    def _button(u, l, z): zones[_ffi.string(l).decode()] = z  # noqa: E701

    @_ffi.callback("void(void*, const char*, float*, float, float, float, float)")
    def _slider(u, l, z, i, lo, hi, st): zones[_ffi.string(l).decode()] = z  # noqa: E701

    @_ffi.callback("void(void*, const char*, float*, float, float)")
    def _bar(u, l, z, lo, hi): pass  # noqa: E701

    @_ffi.callback("void(void*, const char*, const char*, void**)")
    def _sf(u, l, url, s): pass  # noqa: E701

    @_ffi.callback("void(void*, float*, const char*, const char*)")
    def _decl(u, z, k, v): pass  # noqa: E701

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

    _lib.buildUserInterfaceStaveOscBank(dsp, glue)
    keepalive.extend([_open, _close, _button, _slider, _bar, _sf, _decl, glue])
    return keepalive
