"""Bench: Python _process_ping_pong vs FaustPingPong.

Same block size + SR as bench_reverb.py. Python runs only the write + tap
read (no BPM math). Faust runs the full cross-coupled delay.
"""
from __future__ import annotations

import time

import numpy as np

from stave_synth.synth_engine import SynthEngine
from stave_synth.faust_ping_pong import FaustPingPong

SR = 48000
N = 128
ITERS = 5000
WARMUP = 500


def bench_python(label):
    e = SynthEngine(sample_rate=SR)
    e.delay_enabled = True
    e.delay_wet = 0.5
    e.delay_feedback = 0.4
    e.delay_time_ms = 300
    e.delay_time_mode = "FREE"
    e.motion_mix = 1.0

    in_l = np.random.default_rng(1).standard_normal(N) * 0.1
    in_r = np.random.default_rng(2).standard_normal(N) * 0.1

    for _ in range(WARMUP):
        out_l = in_l.copy(); out_r = in_r.copy()
        e._process_ping_pong(out_l, out_r)

    t0 = time.perf_counter()
    for _ in range(ITERS):
        out_l = in_l.copy(); out_r = in_r.copy()
        e._process_ping_pong(out_l, out_r)
    elapsed = time.perf_counter() - t0

    per_block_ns = elapsed / ITERS * 1e9
    budget_ns = (N / SR) * 1e9
    print(f"  {label:<22s}  {per_block_ns:>8.0f} ns/block   "
          f"{per_block_ns / budget_ns * 100:>5.1f}% of real-time budget")


def bench_faust(label):
    pp = FaustPingPong(SR)
    pp.set_params(delay_l_samps=14400, delay_r_samps=14400, feedback=0.4, wet=0.5)

    in_l = np.random.default_rng(1).standard_normal(N) * 0.1
    in_r = np.random.default_rng(2).standard_normal(N) * 0.1

    for _ in range(WARMUP):
        out_l = in_l.copy(); out_r = in_r.copy()
        pp.process_inplace(out_l, out_r)

    t0 = time.perf_counter()
    for _ in range(ITERS):
        out_l = in_l.copy(); out_r = in_r.copy()
        pp.process_inplace(out_l, out_r)
    elapsed = time.perf_counter() - t0

    per_block_ns = elapsed / ITERS * 1e9
    budget_ns = (N / SR) * 1e9
    print(f"  {label:<22s}  {per_block_ns:>8.0f} ns/block   "
          f"{per_block_ns / budget_ns * 100:>5.1f}% of real-time budget")


def main():
    print(f"\nBench: {ITERS} blocks of {N} samples @ {SR} Hz")
    print(f"  Real-time budget per block: {N/SR*1e9:.0f} ns\n")
    bench_python("Python ping-pong")
    bench_faust("Faust ping-pong")


if __name__ == "__main__":
    main()
