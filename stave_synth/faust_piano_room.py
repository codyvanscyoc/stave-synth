"""Piano-room reverb (libstave_piano_room.so).

Dedicated small-room reverb for the piano layer. Replaces FluidSynth's
legacy Schroeder internal reverb. Output is 100% wet — the caller is
expected to apply its own dry/wet mix.

Public API mirrors FaustPlate so it can slot into the same pipeline.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from cffi import FFI

logger = logging.getLogger(__name__)

from .audio_io.platform import LIB_SUFFIX

_HERE = Path(__file__).parent.parent / "faust"
_LIB = _HERE / f"libstave_piano_room{LIB_SUFFIX}"

_ffi = FFI()
_ffi.cdef("""
typedef struct StavePianoRoom StavePianoRoom;

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

StavePianoRoom* newStavePianoRoom(void);
void deleteStavePianoRoom(StavePianoRoom*);
void initStavePianoRoom(StavePianoRoom*, int sample_rate);
void instanceClearStavePianoRoom(StavePianoRoom*);
void buildUserInterfaceStavePianoRoom(StavePianoRoom*, UIGlue* ui);
void computeStavePianoRoom(StavePianoRoom*, int count, float** inputs, float** outputs);
""")

try:
    _lib = _ffi.dlopen(str(_LIB))
except OSError as e:
    raise RuntimeError(f"Failed to load {_LIB}: {e}. Run faust/build.sh.")


class FaustPianoRoom:
    """Dedicated piano-room reverb — stereo in / stereo wet out."""

    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = int(sample_rate)
        self._dsp = _lib.newStavePianoRoom()
        _lib.initStavePianoRoom(self._dsp, self.sample_rate)

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
                _lib.deleteStavePianoRoom(self._dsp)
                self._dsp = None
        except Exception:
            pass

    def clear(self):
        _lib.instanceClearStavePianoRoom(self._dsp)

    def set_zone(self, label: str, value: float):
        z = self._zones.get(label)
        if z is not None:
            z[0] = float(value)

    def process(self, samples: np.ndarray) -> np.ndarray:
        if samples.ndim == 2:
            in_l, in_r = samples[0], samples[1]
            n = in_l.shape[0]
        else:
            in_l = in_r = samples
            n = samples.shape[0]
        if n == 0:
            return np.zeros((2, 0), dtype=np.float64)

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

        np.copyto(self._in_l, in_l, casting="unsafe")
        np.copyto(self._in_r, in_r, casting="unsafe")
        _lib.computeStavePianoRoom(self._dsp, n, self._in_ptrs, self._out_ptrs)
        out = np.empty((2, n), dtype=np.float64)
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

    _lib.buildUserInterfaceStavePianoRoom(dsp, glue)
    keepalive.extend([_open, _close, _btn, _sl, _bar, _sf, _dec, glue])
    return keepalive
