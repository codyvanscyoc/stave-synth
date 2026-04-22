"""Faust-native port of the master FX tail (jack_engine.py:557–631).

Covers EQ + HP + pre-gain + saturation + soft limiter. Bus compressor
stays Python-side (too complex / sonically load-bearing to port safely).
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from cffi import FFI

logger = logging.getLogger(__name__)

from .audio_io.platform import LIB_SUFFIX

_HERE = Path(__file__).parent.parent / "faust"
_LIB = _HERE / f"libstave_master_fx{LIB_SUFFIX}"


_ffi = FFI()
_ffi.cdef("""
typedef struct StaveMasterFX StaveMasterFX;

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

StaveMasterFX* newStaveMasterFX(void);
void deleteStaveMasterFX(StaveMasterFX*);
void initStaveMasterFX(StaveMasterFX*, int sample_rate);
void instanceClearStaveMasterFX(StaveMasterFX*);
void buildUserInterfaceStaveMasterFX(StaveMasterFX*, UIGlue* ui);
void computeStaveMasterFX(StaveMasterFX*, int count, float** inputs, float** outputs);
""")

try:
    _lib = _ffi.dlopen(str(_LIB))
except OSError as e:
    raise RuntimeError(f"Failed to load {_LIB}: {e}. Run faust/build.sh.")


class FaustMasterFX:
    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = int(sample_rate)
        self._dsp = _lib.newStaveMasterFX()
        _lib.initStaveMasterFX(self._dsp, self.sample_rate)

        self._zones: dict[str, _ffi.CData] = {}
        self._keepalive = _install_ui_callbacks(self._dsp, self._zones)

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
                _lib.deleteStaveMasterFX(self._dsp)
                self._dsp = None
        except Exception:
            pass

    def set_eq_band(self, band: int, freq_hz: float, gain_db: float, q: float):
        """band: 1/2/3"""
        self._zones[f"eq{band}_freq"][0] = float(max(20.0, min(24000.0, freq_hz)))
        self._zones[f"eq{band}_gain"][0] = float(max(-24.0, min(24.0, gain_db)))
        self._zones[f"eq{band}_q"][0] = float(max(0.1, min(10.0, q)))

    def set_hp(self, enabled: bool, freq_hz: float, slope_db_per_oct: int):
        self._zones["hp_enable"][0] = 1.0 if enabled else 0.0
        self._zones["hp_freq"][0] = float(max(20.0, min(2000.0, freq_hz)))
        # Snap to nearest supported slope (6/12/24)
        slope = 24 if slope_db_per_oct >= 18 else (12 if slope_db_per_oct >= 9 else 6)
        self._zones["hp_slope"][0] = float(slope)

    def set_tail(self, pre_gain: float, sat_enabled: bool):
        self._zones["pre_gain"][0] = float(max(0.1, min(10.0, pre_gain)))
        self._zones["sat_enable"][0] = 1.0 if sat_enabled else 0.0

    def process_inplace(self, stereo: np.ndarray):
        """Modify stereo[0], stereo[1] in-place with the Faust master chain."""
        n = stereo.shape[1]
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

        np.copyto(self._in_l, stereo[0], casting="unsafe")
        np.copyto(self._in_r, stereo[1], casting="unsafe")
        _lib.computeStaveMasterFX(self._dsp, n, self._in_ptrs, self._out_ptrs)
        np.copyto(stereo[0], self._out_l, casting="unsafe")
        np.copyto(stereo[1], self._out_r, casting="unsafe")


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

    _lib.buildUserInterfaceStaveMasterFX(dsp, glue)
    keepalive.extend([_open, _close, _btn, _sl, _bar, _sf, _dec, glue])
    return keepalive
