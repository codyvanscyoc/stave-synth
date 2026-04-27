"""Side-by-side perf: FeedbackDelayReverb (Python) vs FaustReverb (native).

Measures wall-clock ns per 128-sample stereo block on the current machine.
No sonic comparison here — that's the user's ears.
"""
from __future__ import annotations

import time

import numpy as np

from stave_synth.synth_engine import FeedbackDelayReverb
from stave_synth.faust_reverb import FaustReverb

SR = 48000
N = 128  # Pi JACK block
ITERS = 5000
WARMUP = 500


def bench(reverb, label):
    rng = np.random.default_rng(42)
    in_buf = rng.standard_normal((2, N)).astype(np.float64) * 0.1

    for _ in range(WARMUP):
        reverb.process(in_buf)

    t0 = time.perf_counter()
    for _ in range(ITERS):
        reverb.process(in_buf)
    elapsed = time.perf_counter() - t0

    per_block_ns = elapsed / ITERS * 1e9
    budget_ns = (N / SR) * 1e9  # real-time budget per block
    cpu_pct = per_block_ns / budget_ns * 100.0
    print(f"  {label:<22s}  {per_block_ns:>8.0f} ns/block   "
          f"{cpu_pct:>5.1f}% of real-time budget")


def main():
    py = FeedbackDelayReverb(decay_seconds=6.0, sample_rate=SR)
    fa = FaustReverb(decay_seconds=6.0, sample_rate=SR)

    print(f"\nBench: {ITERS} blocks of {N} samples @ {SR} Hz")
    print(f"  Real-time budget per block: {N/SR*1e9:.0f} ns ({N/SR*1000:.2f} ms)\n")
    bench(py, "Python FDN (numpy)")
    bench(fa, "Faust FDN (native)")


if __name__ == "__main__":
    main()
