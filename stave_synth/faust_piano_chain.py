"""Faust-native port of the piano post-processing chain (Phase 3).

Consumes the FluidSynth block (L, R — int16-converted float64, PRE-volume)
and produces 3 channels:

    [0] out_L  [1] out_R — volume gain → 24 dB low-cut → 24 dB high-cut →
                           4-band parametric EQ (frozen-state per band)
    [2] mono             — (out_L + out_R) · 0.5 post-EQ: the LA-2A
                           compressor's sidechain. The comp itself stays
                           in Python (block-rate envelope semantics); it
                           consumes this channel and applies its gain
                           ramp to [0]/[1].

Python owns ALL parameter smoothing (the ~10 ms volume one-pole + dB
curve + the ≤0.001 hard-zero) and pushes block-constant zones verbatim —
see piano_chain.dsp.

Built with -double + -DFAUSTFLOAT=double (faust/build.sh): double zones
and double I/O, so the player's float64 blocks pass through zero-copy —
no float32 boundary scratch, and the low-frequency EQ biquads hold the
~1e-6 parity bar (they can't in float32).
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from cffi import FFI

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent.parent / "faust"
_LIB = _HERE / "libstave_piano_chain.so"

_ffi = FFI()
# NOTE: FAUSTFLOAT == double for this module (build.sh passes
# -DFAUSTFLOAT=double), so every zone/buffer pointer below is double*.
_ffi.cdef("""
typedef struct StavePianoChain StavePianoChain;

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

StavePianoChain* newStavePianoChain(void);
void deleteStavePianoChain(StavePianoChain*);
void initStavePianoChain(StavePianoChain*, int sample_rate);
void instanceClearStavePianoChain(StavePianoChain*);
void buildUserInterfaceStavePianoChain(StavePianoChain*, UIGlue* ui);
void computeStavePianoChain(StavePianoChain*, int count, double** inputs, double** outputs);
""")

try:
    _lib = _ffi.dlopen(str(_LIB))
except OSError as e:
    raise RuntimeError(f"Failed to load {_LIB}: {e}. Run faust/build.sh.")

_N_OUT = 3


class FaustPianoChain:
    """Phase 3 piano chain: 2-in → 3-out, per-block zone push."""

    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = int(sample_rate)
        self._dsp = _lib.newStavePianoChain()
        if self._dsp == _ffi.NULL:
            raise RuntimeError("newStavePianoChain returned NULL")
        _lib.initStavePianoChain(self._dsp, self.sample_rate)

        self._zones: dict[str, _ffi.CData] = {}
        self._keepalive = _install_ui_callbacks(self._dsp, self._zones)

        # Persistent float64 in/out buffers + cached FFI pointers
        # (zero-alloc render rule). The IN buffer is owned here because
        # FluidSynth hands the player a fresh int16 array every block —
        # the player writes the converted floats straight into these rows,
        # so the pointers stay valid for the life of the buffer.
        self._buf_n = 0
        self._in = np.empty((2, 0), dtype=np.float64)
        self._out = np.empty((_N_OUT, 0), dtype=np.float64)
        self._in_ptrs = _ffi.new("double*[2]")
        self._out_ptrs = _ffi.new(f"double*[{_N_OUT}]")

    def __del__(self):
        try:
            if getattr(self, "_dsp", None):
                _lib.deleteStavePianoChain(self._dsp)
                self._dsp = None
        except Exception:
            pass

    def clear(self):
        """Flush all filter state. Full re-init, not instanceClear — the
        Phase-2 gotcha (rwtable contents live in instanceConstants) makes
        full init the only safe clear pattern, and it also re-defaults
        every zone; the per-block zones are rewritten by set_block_params
        before the next compute. NOTE: the Python path never resets these
        biquads (all_notes_off touches only comp/room state), so nothing
        calls this in normal operation — it exists for parity of the
        wrapper contract and for future explicit flushes."""
        _lib.initStavePianoChain(self._dsp, self.sample_rate)

    def in_buffer(self, n: int) -> np.ndarray:
        """Return the persistent (2, n) float64 input buffer (allocating
        both in/out buffers + pointers if the block size changed). The
        player writes the int16-converted samples into its rows."""
        if n != self._buf_n:
            self._in = np.empty((2, n), dtype=np.float64)
            self._out = np.empty((_N_OUT, n), dtype=np.float64)
            for i in range(2):
                self._in_ptrs[i] = _ffi.cast("double*", self._in[i].ctypes.data)
            for i in range(_N_OUT):
                self._out_ptrs[i] = _ffi.cast("double*", self._out[i].ctypes.data)
            self._buf_n = n
        return self._in

    def set_block_params(self, *, gain: float, lowcut_hz: float,
                         highcut_hz: float, eq_bands: list):
        """Write all per-block zones at once. `gain` is the Python-smoothed
        dB-curve volume gain (0.0 on the ≤0.001 branch); cutoffs and EQ
        params arrive exactly as the setters clamped them — the biquad
        clamps live in piano_chain.dsp, mirroring the Biquad* classes."""
        z = self._zones
        z["piano_gain"][0] = float(gain)
        z["lowcut_hz"][0] = float(lowcut_hz)
        z["highcut_hz"][0] = float(highcut_hz)
        for i, b in enumerate(eq_bands):
            z[f"eq{i}_freq"][0] = float(b["freq_hz"])
            z[f"eq{i}_gain"][0] = float(b["gain_db"])
            z[f"eq{i}_q"][0] = float(b["q"])
            z[f"eq{i}_on"][0] = 1.0 if b["enabled"] else 0.0

    def process_in_place(self, n: int) -> np.ndarray:
        """Run one block over the samples previously written into
        in_buffer(n). Returns the persistent (3, n) float64 output buffer
        — consume within the block."""
        _lib.computeStavePianoChain(self._dsp, n, self._in_ptrs, self._out_ptrs)
        return self._out


# ────────────────────────────────────────────────────────────────────────
# UI callback glue — same pattern as faust_pad_bus.py, double zones.
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
        pass

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

    _lib.buildUserInterfaceStavePianoChain(dsp, glue)
    keepalive.extend([_open, _close, _button, _slider, _bar, _sf, _decl, glue])
    return keepalive
