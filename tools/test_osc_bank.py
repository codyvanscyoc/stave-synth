"""Phase 3a skeleton test — verify 16-voice Faust osc bank works end-to-end.

No integration with SynthEngine yet. Just prove:
  1. Per-voice freq/gate slider routing works
  2. Multiple voices sum correctly
  3. Phase continuity across blocks (no clicks at block boundaries)
  4. Single-call overhead is sensible (< 50µs target)
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from cffi import FFI


HERE = Path(__file__).parent
SR = 48000
N = 128
NVOICES = 16


ffi = FFI()
ffi.cdef("""
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
int  getNumInputsStaveOscBank(StaveOscBank*);
int  getNumOutputsStaveOscBank(StaveOscBank*);
void buildUserInterfaceStaveOscBank(StaveOscBank*, UIGlue* ui);
void computeStaveOscBank(StaveOscBank*, int count, float** inputs, float** outputs);
""")
lib = ffi.dlopen(str(HERE / "libstave_osc_bank.so"))


def install_ui(dsp):
    zones = {}
    keep = []
    @ffi.callback("void(void*, const char*)")
    def _open(u, l): pass
    @ffi.callback("void(void*)")
    def _close(u): pass
    @ffi.callback("void(void*, const char*, float*)")
    def _btn(u, l, z): zones[ffi.string(l).decode()] = z
    @ffi.callback("void(void*, const char*, float*, float, float, float, float)")
    def _sl(u, l, z, i, lo, hi, st): zones[ffi.string(l).decode()] = z
    @ffi.callback("void(void*, const char*, float*, float, float)")
    def _bar(u, l, z, lo, hi): pass
    @ffi.callback("void(void*, const char*, const char*, void**)")
    def _sf(u, l, url, s): pass
    @ffi.callback("void(void*, float*, const char*, const char*)")
    def _dec(u, z, k, v): pass
    g = ffi.new("UIGlue*")
    g.uiInterface = ffi.NULL
    g.openTabBox = _open; g.openHorizontalBox = _open; g.openVerticalBox = _open
    g.closeBox = _close
    g.addButton = _btn; g.addCheckButton = _btn
    g.addVerticalSlider = _sl; g.addHorizontalSlider = _sl; g.addNumEntry = _sl
    g.addHorizontalBargraph = _bar; g.addVerticalBargraph = _bar
    g.addSoundfile = _sf; g.declare = _dec
    lib.buildUserInterfaceStaveOscBank(dsp, g)
    keep.extend([_open, _close, _btn, _sl, _bar, _sf, _dec, g])
    return zones, keep


def main():
    dsp = lib.newStaveOscBank()
    lib.initStaveOscBank(dsp, SR)
    zones, keep = install_ui(dsp)

    print(f"Zones discovered: {len(zones)}")
    per_voice = sum(1 for k in zones if k.startswith("freq_v")) + \
                sum(1 for k in zones if k.startswith("gate_v"))
    globals_n = len(zones) - per_voice
    print(f"  per-voice: {per_voice} ({NVOICES} freq + {NVOICES} gate)")
    print(f"  global  : {globals_n} (osc waveforms, blends, octaves, unison, pan)")
    assert per_voice == NVOICES * 2

    # Three voice chord: A4 (440), C#5 (554.37), E5 (659.26)
    zones["freq_v0"][0] = 440.0; zones["gate_v0"][0] = 1.0
    zones["freq_v1"][0] = 554.37; zones["gate_v1"][0] = 1.0
    zones["freq_v2"][0] = 659.26; zones["gate_v2"][0] = 1.0

    # Configure osc defaults: osc1=sine (0.6 blend), osc2=square (0.4 blend)
    zones["osc1_wf"][0] = 0; zones["osc2_wf"][0] = 1
    zones["osc1_blend"][0] = 0.6; zones["osc2_blend"][0] = 0.4
    zones["osc1_oct"][0] = 1.0; zones["osc2_oct"][0] = 1.0
    zones["uni_detune"][0] = 0.07; zones["uni_spread"][0] = 0.85
    zones["osc1_pan"][0] = 0.0; zones["osc2_pan"][0] = 0.0

    in_ptrs = ffi.new("float*[0]")  # no inputs
    out_l = np.zeros(N, dtype=np.float32)
    out_r = np.zeros(N, dtype=np.float32)
    out_ptrs = ffi.new("float*[2]")
    out_ptrs[0] = ffi.cast("float*", out_l.ctypes.data)
    out_ptrs[1] = ffi.cast("float*", out_r.ctypes.data)

    # Burn a block so gate smoothing settles
    lib.computeStaveOscBank(dsp, N, in_ptrs, out_ptrs)

    # Collect 1 second (8000 blocks × 128 = 1M samples ≈ 21s too much)
    # Do 200 blocks (~0.5s of audio) and FFT the result
    collected = np.zeros(200 * N, dtype=np.float32)
    for i in range(200):
        lib.computeStaveOscBank(dsp, N, in_ptrs, out_ptrs)
        collected[i * N:(i + 1) * N] = out_l

    # FFT: find peaks — should see 440, 554, 659 Hz
    from numpy.fft import rfft, rfftfreq
    spectrum = np.abs(rfft(collected))
    freqs = rfftfreq(collected.size, 1.0 / SR)
    # Peaks (ignore DC bin)
    peak_idxs = np.argsort(spectrum[1:])[-5:] + 1
    peaks_hz = sorted(freqs[peak_idxs])
    print(f"Detected spectral peaks: {[f'{p:.1f} Hz' for p in peaks_hz]}")
    # Check we hit at least the three target freqs within 3 Hz
    for expected in (440.0, 554.37, 659.26):
        found = any(abs(p - expected) < 3.0 for p in peaks_hz)
        print(f"  {expected:>7.2f} Hz: {'FOUND' if found else 'MISSING'}")

    # Check no clicks: max inter-sample delta should be moderate
    diff = np.abs(np.diff(collected))
    print(f"Max sample-to-sample delta: {diff.max():.4f} (expect < ~0.2 for smooth sines)")

    # --- Bench single-call overhead ---
    ITERS = 5000
    for _ in range(500):  # warmup
        lib.computeStaveOscBank(dsp, N, in_ptrs, out_ptrs)
    t0 = time.perf_counter()
    for _ in range(ITERS):
        lib.computeStaveOscBank(dsp, N, in_ptrs, out_ptrs)
    elapsed = time.perf_counter() - t0
    per_block_ns = elapsed / ITERS * 1e9
    budget_ns = (N / SR) * 1e9
    print(f"\nBench: {per_block_ns:.0f} ns/block   "
          f"{per_block_ns/budget_ns*100:.1f}% of real-time budget @ {N}/{SR}")

    lib.deleteStaveOscBank(dsp)


if __name__ == "__main__":
    main()
