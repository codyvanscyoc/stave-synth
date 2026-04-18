"""Faust-native port of sympathetic resonance (synth_engine.py:2373)."""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from cffi import FFI

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent.parent / "faust"
_LIB = _HERE / "libstave_sympathetic.so"

N_SLOTS = 16  # must match N_SLOTS in sympathetic.dsp


_ffi = FFI()
_ffi.cdef("""
typedef struct StaveSympathetic StaveSympathetic;

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

StaveSympathetic* newStaveSympathetic(void);
void deleteStaveSympathetic(StaveSympathetic*);
void initStaveSympathetic(StaveSympathetic*, int sample_rate);
void instanceClearStaveSympathetic(StaveSympathetic*);
void buildUserInterfaceStaveSympathetic(StaveSympathetic*, UIGlue* ui);
void computeStaveSympathetic(StaveSympathetic*, int count, float** inputs, float** outputs);
""")

try:
    _lib = _ffi.dlopen(str(_LIB))
except OSError as e:
    raise RuntimeError(f"Failed to load {_LIB}: {e}. Run faust/build.sh.")


class FaustSympathetic:
    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = int(sample_rate)
        self._dsp = _lib.newStaveSympathetic()
        _lib.initStaveSympathetic(self._dsp, self.sample_rate)

        self._zones: dict[str, _ffi.CData] = {}
        self._keepalive = _install_ui_callbacks(self._dsp, self._zones)

        self._freq_zones = [self._zones[f"symp_freq_s{i}"] for i in range(N_SLOTS)]
        self._gate_zones = [self._zones[f"symp_gate_s{i}"] for i in range(N_SLOTS)]

        self._buf_n = 0
        self._out_l = np.empty(0, dtype=np.float32)
        self._out_r = np.empty(0, dtype=np.float32)
        self._in_ptrs = _ffi.new("float*[0]")
        self._out_ptrs = _ffi.new("float*[2]")

    def __del__(self):
        try:
            if getattr(self, "_dsp", None):
                _lib.deleteStaveSympathetic(self._dsp)
                self._dsp = None
        except Exception:
            pass

    def clear_all(self):
        _lib.instanceClearStaveSympathetic(self._dsp)
        for i in range(N_SLOTS):
            self._freq_zones[i][0] = 0.0
            self._gate_zones[i][0] = 0.0

    def set_sym_level(self, level: float):
        self._zones["sym_level"][0] = float(max(0.0, min(1.0, level)))

    def set_slot(self, slot: int, freq_hz: float, gate_effective: float):
        """`gate_effective` already includes the HF rolloff (compute in Python)."""
        self._freq_zones[slot][0] = float(freq_hz)
        self._gate_zones[slot][0] = float(max(0.0, min(1.0, gate_effective)))

    def clear_slot(self, slot: int):
        self._gate_zones[slot][0] = 0.0

    def process(self, n_samples: int) -> np.ndarray:
        if n_samples == 0:
            return np.zeros((2, 0), dtype=np.float64)

        if n_samples != self._buf_n:
            self._out_l = np.empty(n_samples, dtype=np.float32)
            self._out_r = np.empty(n_samples, dtype=np.float32)
            self._out_ptrs[0] = _ffi.cast("float*", self._out_l.ctypes.data)
            self._out_ptrs[1] = _ffi.cast("float*", self._out_r.ctypes.data)
            self._buf_n = n_samples

        _lib.computeStaveSympathetic(self._dsp, n_samples, self._in_ptrs, self._out_ptrs)

        out = np.empty((2, n_samples), dtype=np.float64)
        np.copyto(out[0], self._out_l, casting="unsafe")
        np.copyto(out[1], self._out_r, casting="unsafe")
        return out


def _install_ui_callbacks(dsp, zones: dict):
    keepalive = []

    @_ffi.callback("void(void*, const char*)")
    def _open(u, l): pass  # noqa: E701

    @_ffi.callback("void(void*)")
    def _close(u): pass  # noqa: E701

    @_ffi.callback("void(void*, const char*, float*)")
    def _btn(u, l, z): zones[_ffi.string(l).decode()] = z  # noqa: E701

    @_ffi.callback("void(void*, const char*, float*, float, float, float, float)")
    def _sl(u, l, z, i, lo, hi, st): zones[_ffi.string(l).decode()] = z  # noqa: E701

    @_ffi.callback("void(void*, const char*, float*, float, float)")
    def _bar(u, l, z, lo, hi): pass  # noqa: E701

    @_ffi.callback("void(void*, const char*, const char*, void**)")
    def _sf(u, l, url, s): pass  # noqa: E701

    @_ffi.callback("void(void*, float*, const char*, const char*)")
    def _dec(u, z, k, v): pass  # noqa: E701

    glue = _ffi.new("UIGlue*")
    glue.uiInterface = _ffi.NULL
    glue.openTabBox = _open
    glue.openHorizontalBox = _open
    glue.openVerticalBox = _open
    glue.closeBox = _close
    glue.addButton = _btn
    glue.addCheckButton = _btn
    glue.addVerticalSlider = _sl
    glue.addHorizontalSlider = _sl
    glue.addNumEntry = _sl
    glue.addHorizontalBargraph = _bar
    glue.addVerticalBargraph = _bar
    glue.addSoundfile = _sf
    glue.declare = _dec

    _lib.buildUserInterfaceStaveSympathetic(dsp, glue)
    keepalive.extend([_open, _close, _btn, _sl, _bar, _sf, _dec, glue])
    return keepalive
