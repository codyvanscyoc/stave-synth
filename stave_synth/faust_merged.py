"""Phase 4 merged shim wrapper (FAUST_PORT_PLAN.md, Design A).

ONE cffi call per block runs: osc-bank compute → f32→f64 widen (exact,
in C) → pad-bus compute. Replaces the per-block Python sandwich (two cffi
calls + five np.copyto casts + buffer shuttling) that showed up as the
full-blast CPU spikes.

The shim library (faust/libstave_merged_shim.so) links against nothing:
the compute-function pointers and both dsp instance pointers are captured
here from whatever the existing wrappers loaded — so the 12-slot lite osc
bank (LOW_RAM_MODE) works unchanged, and each module keeps its own build
precision (bank float32, pad bus double) and its existing parity proofs.

Buffer ownership: this class owns the bank's f32 output rows, the f64
conversion block (returned to the engine as the block's osc_out — the
pre-filter taps read it), and the 9-row pad-bus output. All persistent,
zero-alloc on the hot path (realloc only on block-size change).

Zone pushing stays with the existing wrappers (FaustOscBank setters,
FaustPadBus.set_block_params / push_lfo_block) — zones are written before
the merged call exactly as before; only the computes are fused.

Stateless: panic needs nothing here (FaustOscBank.panic() and
FaustPadBus.clear() already flush the real state).
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from cffi import FFI

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent.parent / "faust"
_LIB = _HERE / "libstave_merged_shim.so"

_ffi = FFI()
_ffi.cdef("""
void stave_merged_compute(void* bank_fn, void* bank_dsp,
                          void* bus_fn, void* bus_dsp,
                          int count, float** bank_out,
                          double** bus_in, double** bus_out);
""")

try:
    _lib = _ffi.dlopen(str(_LIB))
except OSError as e:
    raise RuntimeError(f"Failed to load {_LIB}: {e}. Run faust/build.sh.")

_N_BANK_CH = 5
_N_BUS_OUT = 9


def _voidp(owner_ffi, cdata):
    """Re-home a cdata pointer from another FFI instance as our void*."""
    return _ffi.cast("void*", int(owner_ffi.cast("uintptr_t", cdata)))


class FaustMergedShim:
    """Single-call osc_bank → pad_bus compute for the merged render path."""

    def __init__(self, bank, pad_bus):
        # Import the wrapper modules (already loaded — both instances
        # exist) to reach their FFI objects + dlopen'd libraries.
        from . import faust_osc_bank as _fob
        from . import faust_pad_bus as _fpb

        # Keep the wrappers alive: their __del__ frees the dsp instances
        # whose raw pointers we hold below.
        self._bank = bank
        self._pad_bus = pad_bus
        self._bank_fn = _voidp(_fob._ffi, _fob._lib.computeStaveOscBank)
        self._bank_dsp = _voidp(_fob._ffi, bank._dsp)
        self._bus_fn = _voidp(_fpb._ffi, _fpb._lib.computeStavePadBus)
        self._bus_dsp = _voidp(_fpb._ffi, pad_bus._dsp)

        self._buf_n = 0
        self._bank_out = np.empty((_N_BANK_CH, 0), dtype=np.float32)
        self._bus_in = np.empty((_N_BANK_CH, 0), dtype=np.float64)
        self._bus_out = np.empty((_N_BUS_OUT, 0), dtype=np.float64)
        self._bank_out_ptrs = _ffi.new(f"float*[{_N_BANK_CH}]")
        self._bus_in_ptrs = _ffi.new(f"double*[{_N_BANK_CH}]")
        self._bus_out_ptrs = _ffi.new(f"double*[{_N_BUS_OUT}]")

    def process(self, n_samples: int):
        """Run one merged block. Returns (pad_out, osc_out):
             pad_out — persistent (9, n) float64 pad-bus output
             osc_out — persistent (5, n) float64 bank block (the widened
                       conversion target; feeds the engine's pre-filter
                       magnitude taps)
        Both are consumed within the block (zero-alloc rule)."""
        n = int(n_samples)
        if n == 0:
            return (np.zeros((_N_BUS_OUT, 0), dtype=np.float64),
                    np.zeros((_N_BANK_CH, 0), dtype=np.float64))
        if n != self._buf_n:
            self._bank_out = np.empty((_N_BANK_CH, n), dtype=np.float32)
            self._bus_in = np.empty((_N_BANK_CH, n), dtype=np.float64)
            self._bus_out = np.empty((_N_BUS_OUT, n), dtype=np.float64)
            for i in range(_N_BANK_CH):
                self._bank_out_ptrs[i] = _ffi.cast(
                    "float*", self._bank_out[i].ctypes.data)
                self._bus_in_ptrs[i] = _ffi.cast(
                    "double*", self._bus_in[i].ctypes.data)
            for i in range(_N_BUS_OUT):
                self._bus_out_ptrs[i] = _ffi.cast(
                    "double*", self._bus_out[i].ctypes.data)
            self._buf_n = n

        _lib.stave_merged_compute(
            self._bank_fn, self._bank_dsp,
            self._bus_fn, self._bus_dsp,
            n, self._bank_out_ptrs, self._bus_in_ptrs, self._bus_out_ptrs)
        return self._bus_out, self._bus_in
