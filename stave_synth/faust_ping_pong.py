"""Faust-native port of _process_ping_pong.

Mirrors SynthEngine's ping-pong delay behaviour so it can drop in behind
a flag without changing callers. float32 boundary, same as FaustReverb.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from cffi import FFI

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent.parent / "faust"
_LIB = _HERE / "libstave_ping_pong.so"


_ffi = FFI()
_ffi.cdef("""
typedef struct StavePingPong StavePingPong;

typedef void (*openBoxFn)(void* ui, const char* label);
typedef void (*closeBoxFn)(void* ui);
typedef void (*addBtnFn)(void* ui, const char* label, float* zone);
typedef void (*addSliderFn)(void* ui, const char* label, float* zone,
                            float init, float min, float max, float step);
typedef void (*addBarFn)(void* ui, const char* label, float* zone,
                         float min, float max);
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

StavePingPong* newStavePingPong(void);
void deleteStavePingPong(StavePingPong*);
void initStavePingPong(StavePingPong*, int sample_rate);
void instanceClearStavePingPong(StavePingPong*);
void buildUserInterfaceStavePingPong(StavePingPong*, UIGlue* ui);
void computeStavePingPong(StavePingPong*, int count, float** inputs, float** outputs);
""")

try:
    _lib = _ffi.dlopen(str(_LIB))
except OSError as e:
    raise RuntimeError(f"Failed to load {_LIB}: {e}. Run faust/build.sh.")


class FaustPingPong:
    """Drop-in replacement for SynthEngine's ping-pong delay state + DSP."""

    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = int(sample_rate)
        self._dsp = _lib.newStavePingPong()
        if self._dsp == _ffi.NULL:
            raise RuntimeError("newStavePingPong returned NULL")
        _lib.initStavePingPong(self._dsp, self.sample_rate)

        self._zones: dict[str, _ffi.CData] = {}
        self._keepalive = _install_ui_callbacks(self._dsp, self._zones)

        # Scratch float32 buffers for FFI boundary
        self._buf_n = 0
        self._in_l = np.empty(0, dtype=np.float32)
        self._in_r = np.empty(0, dtype=np.float32)
        self._out_l = np.empty(0, dtype=np.float32)
        self._out_r = np.empty(0, dtype=np.float32)
        self._in_ptrs = _ffi.new("float*[2]")
        self._out_ptrs = _ffi.new("float*[2]")

    def __del__(self):
        try:
            if getattr(self, "_dsp", None):
                _lib.deleteStavePingPong(self._dsp)
                self._dsp = None
        except Exception:
            pass

    def clear(self):
        """Flush delay buffers."""
        _lib.instanceClearStavePingPong(self._dsp)

    def set_params(self, delay_l_samps: int, delay_r_samps: int,
                   feedback: float, wet: float):
        """Write all delay params at once. delay_*_samps are ints; Python
        computes them from ms/BPM per block and passes here."""
        self._zones["delay_l_samps"][0] = float(max(1, min(65535, int(delay_l_samps))))
        self._zones["delay_r_samps"][0] = float(max(1, min(65535, int(delay_r_samps))))
        self._zones["feedback"][0] = float(max(0.0, min(0.85, feedback)))
        self._zones["wet"][0] = float(max(0.0, min(1.0, wet)))

    def process_inplace(self, out_l: np.ndarray, out_r: np.ndarray):
        """Replace (out_l, out_r) with the Faust ping-pong result in-place.
        Matches the semantics of SynthEngine._process_ping_pong."""
        n = out_l.shape[0]
        if n == 0:
            return

        if n != self._buf_n:
            self._in_l = np.empty(n, dtype=np.float32)
            self._in_r = np.empty(n, dtype=np.float32)
            self._out_l = np.empty(n, dtype=np.float32)
            self._out_r = np.empty(n, dtype=np.float32)
            self._in_ptrs[0] = _ffi.cast("float*", self._in_l.ctypes.data)
            self._in_ptrs[1] = _ffi.cast("float*", self._in_r.ctypes.data)
            self._out_ptrs[0] = _ffi.cast("float*", self._out_l.ctypes.data)
            self._out_ptrs[1] = _ffi.cast("float*", self._out_r.ctypes.data)
            self._buf_n = n

        np.copyto(self._in_l, out_l, casting="unsafe")
        np.copyto(self._in_r, out_r, casting="unsafe")
        _lib.computeStavePingPong(self._dsp, n, self._in_ptrs, self._out_ptrs)
        np.copyto(out_l, self._out_l, casting="unsafe")
        np.copyto(out_r, self._out_r, casting="unsafe")


# ────────────────────────────────────────────────────────────────────────
# UI callback glue — same pattern as faust_reverb.py
# ────────────────────────────────────────────────────────────────────────

def _install_ui_callbacks(dsp, zones: dict):
    keepalive = []

    @_ffi.callback("void(void*, const char*)")
    def _open(ui, label):  # noqa: ARG001
        pass

    @_ffi.callback("void(void*)")
    def _close(ui):  # noqa: ARG001
        pass

    @_ffi.callback("void(void*, const char*, float*)")
    def _button(ui, label, zone):  # noqa: ARG001
        zones[_ffi.string(label).decode()] = zone

    @_ffi.callback("void(void*, const char*, float*, float, float, float, float)")
    def _slider(ui, label, zone, init, lo, hi, step):  # noqa: ARG001
        zones[_ffi.string(label).decode()] = zone

    @_ffi.callback("void(void*, const char*, float*, float, float)")
    def _bar(ui, label, zone, lo, hi):  # noqa: ARG001
        pass

    @_ffi.callback("void(void*, const char*, const char*, void**)")
    def _sf(ui, label, url, sf):  # noqa: ARG001
        pass

    @_ffi.callback("void(void*, float*, const char*, const char*)")
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

    _lib.buildUserInterfaceStavePingPong(dsp, glue)
    keepalive.extend([_open, _close, _button, _slider, _bar, _sf, _decl, glue])
    return keepalive
