"""Faust-native bus compressor (simplified SSL G-style).

Self-sidechain only, per-sample detection (vs Python's block-average),
no auto-release. Opt-in via STAVE_FAUST_BUS_COMP=1. Python BusCompressor
remains the default fallback when the flag is off or external sidechain
modes (piano/lfo/bpm) are selected.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from cffi import FFI

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent.parent / "faust"
_LIB = _HERE / "libstave_bus_comp.so"


_ffi = FFI()
_ffi.cdef("""
typedef struct StaveBusComp StaveBusComp;

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

StaveBusComp* newStaveBusComp(void);
void deleteStaveBusComp(StaveBusComp*);
void initStaveBusComp(StaveBusComp*, int sample_rate);
void instanceClearStaveBusComp(StaveBusComp*);
void buildUserInterfaceStaveBusComp(StaveBusComp*, UIGlue* ui);
void computeStaveBusComp(StaveBusComp*, int count, float** inputs, float** outputs);
""")

try:
    _lib = _ffi.dlopen(str(_LIB))
except OSError as e:
    raise RuntimeError(f"Failed to load {_LIB}: {e}. Run faust/build.sh.")


class FaustBusComp:
    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = int(sample_rate)
        self._dsp = _lib.newStaveBusComp()
        _lib.initStaveBusComp(self._dsp, self.sample_rate)

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
                _lib.deleteStaveBusComp(self._dsp)
                self._dsp = None
        except Exception:
            pass

    def set_params(self, enabled: bool, threshold_db: float, ratio: float,
                   attack_ms: float, release_ms: float, knee_db: float,
                   makeup_db: float, mix: float, sc_hpf_hz: float):
        self._zones["enabled"][0] = 1.0 if enabled else 0.0
        self._zones["threshold_db"][0] = float(max(-60.0, min(0.0, threshold_db)))
        self._zones["ratio"][0] = float(max(1.0, min(20.0, ratio)))
        self._zones["attack_ms"][0] = float(max(0.1, min(100.0, attack_ms)))
        self._zones["release_ms"][0] = float(max(10.0, min(2000.0, release_ms)))
        self._zones["knee_db"][0] = float(max(0.0, min(12.0, knee_db)))
        self._zones["makeup_db"][0] = float(max(-24.0, min(24.0, makeup_db)))
        self._zones["mix"][0] = float(max(0.0, min(1.0, mix)))
        self._zones["sc_hpf_hz"][0] = float(max(20.0, min(500.0, sc_hpf_hz)))

    def process_inplace(self, stereo: np.ndarray):
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
        _lib.computeStaveBusComp(self._dsp, n, self._in_ptrs, self._out_ptrs)
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

    _lib.buildUserInterfaceStaveBusComp(dsp, glue)
    keepalive.extend([_open, _close, _btn, _sl, _bar, _sf, _dec, glue])
    return keepalive
