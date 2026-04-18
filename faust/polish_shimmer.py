"""Audit shimmer amplitude parity — Faust path vs Python path, same note.

Renders a single held note with shimmer enabled through BOTH paths and
compares the shimmer_sines buffer RMS. If they don't match, we know the
amplitude mismatch isn't psychoacoustic.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, "/home/codyvanscyoc/stave-synth")

from stave_synth.synth_engine import SynthEngine


def build_engine(use_faust: bool, seed: int) -> SynthEngine:
    # Force env var before any SynthEngine import-level load
    os.environ["STAVE_FAUST_OSC_BANK"] = "1" if use_faust else "0"
    # Module was already imported; rebuild the flag manually
    from stave_synth import config as cfg
    cfg.USE_FAUST_OSC_BANK = use_faust
    np.random.seed(seed)
    e = SynthEngine(sample_rate=48000)
    e.unison_voices = 3
    e.shimmer_enabled = True
    e.shimmer_mix = 1.0     # take mix out of the equation
    e.shimmer_high = False  # 2x (+12)
    e.shimmer_send = 0.0    # disable CLOUD delay → shimmer_sines goes direct
    return e


def render_shimmer_chord(use_faust: bool, seed: int = 42,
                         notes=(60, 64, 67), vel=0.8, blocks=400):
    """Trigger notes, render `blocks` at 256 samples, return the
    shimmer_filtered signal path (post-HP, pre-mix)."""
    e = build_engine(use_faust, seed)
    actual = "Faust" if e._faust_osc_bank is not None else "Python"
    print(f"    (verified path: {actual})")
    for n in notes:
        e.note_on(n, vel)

    # Render past attack
    collected = np.zeros(blocks * 256)
    for b in range(blocks):
        # e.render() runs the full pipeline; we peek at shimmer_sines via the hp-filtered pre-mix copy
        # Easier: capture self._shimmer_buf directly after each render.
        e.render(256)
        collected[b * 256:(b + 1) * 256] = e._shimmer_buf[:256].copy()
    return collected


def main():
    print("Rendering Python shimmer path...")
    py = render_shimmer_chord(use_faust=False)
    print("Rendering Faust   shimmer path...")
    fa = render_shimmer_chord(use_faust=True)

    # Use steady-state (skip first 100 blocks to let attack settle)
    py_ss = py[100 * 256:]
    fa_ss = fa[100 * 256:]

    print(f"\nSteady-state shimmer_sines RMS:")
    print(f"  Python:  {np.sqrt(np.mean(py_ss**2)):.5f}")
    print(f"  Faust:   {np.sqrt(np.mean(fa_ss**2)):.5f}")
    ratio = np.sqrt(np.mean(fa_ss**2)) / max(np.sqrt(np.mean(py_ss**2)), 1e-9)
    print(f"  ratio (F/P):  {ratio:.3f}x   ({20*np.log10(ratio):+.2f} dB)")

    print(f"\nPeak levels:")
    print(f"  Python:  {np.max(np.abs(py_ss)):.5f}")
    print(f"  Faust:   {np.max(np.abs(fa_ss)):.5f}")


if __name__ == "__main__":
    main()
