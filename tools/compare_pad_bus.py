"""Parity harness for the Faust pad bus (Phase 1 + 2 + 4 render-skeleton port).

Drives full SynthEngine instances through an IDENTICAL multi-block
scenario — same notes, same param timeline, same RNG seed:

  run A — Python render skeleton (STAVE_FAUST_PAD_BUS off)  [reference]
  run B — Faust pad bus, two-call path (PAD_BUS on, MERGED off)
  run C — Phase 4 merged path (PAD_BUS + MERGED on): ONE C call per block
          (osc_bank compute → f32→f64 convert → pad_bus compute) with the
          LFO amp/pan block ramps applied inside pad_bus.dsp on fast-path
          (all-recv) blocks, Python per-block fallback otherwise.

All runs use the Faust osc bank, so the pad-bus region receives
bit-identical 5-channel input; every delta below is attributable to the
ported region alone.

Note on "synthetic input": the region's input is produced by the Faust osc
bank itself (saw + square, unison 3, detuned — dense broadband spectrum,
noise-like top end), with staccato burst chords supplying transients. That
is the actual production signal and is bit-identical across both engines,
which is stronger than an artificial sine/noise feed injected around the
engine (the region cannot be fed directly without restructuring the Python
path, which Phase 1 forbids).

Taps (per channel, matching the module's outputs):
  dry L/R   — captured at the _process_ping_pong entry: the post-LFO,
              post-bypass-carve dry bus. LFO + bypass are Python-side and
              identical in both engines, so deltas here are pure pad-bus
              dry-path deltas.
  send L/R  — captured at reverb.process entry (reverb_in after the 0.6
              trim + tanh — a contraction, so raw send deltas are >= these).
              On fast-path blocks (sends == 1) both engines copy the dry
              bus, so slow-path segments are where the send port is tested.
  final L/R — the engine's render() output, end-to-end incl. reverb.
  bypass    — reserved channels, always 0.0 (asserted once).

The Phase 2 shimmer chain feeds reverb_in directly, so its parity shows
up on the send and final taps (on fast-path AND slow-path blocks — the
shimmer add happens after the path selection).

Scenario segments: fast-path sends → filter sweep down → slope 24 +
resonance → sweep up → pan spread (Haas via plain pans) → hard-pan WIDE →
slow-path sends → burst transients → highpass engage → independent OSC1
filter → chord change → independent OSC2 filter → osc2 blend to zero
(render_osc2 gate) → [Phase 2] shimmer on (+12, fast-path sends) → mix
sweep → +12↔+24 flips → CLOUD send to 0 and re-engage (frozen-ring
exactness) → shimmer off/on (frozen-HP exactness) → slow-path sends with
shimmer → mix-threshold gate off/on → [Phase 4] LFO1 amp → LFO1 pan +
spread → dual LFO mixed targets → one-pole smoothing on → depth-cap dual
amp → recv-flag split (merged run drops to the Python per-block fallback)
→ all-recv restore (Faust re-engage + smoother-state handoff) → poly LFO
on/off (no-double-application check) → LFO2 bus target (Python-side, state
sharing) → LFO2 pan saw + smooth → depths to 0.

Parity bar: max |delta| <= 1e-6 on every tapped channel, every run pair.

Run:  cd stave-synth && venv/bin/python tools/compare_pad_bus.py
"""
from __future__ import annotations

import gc
import os
import sys
from pathlib import Path

# Both engines need the Faust osc bank; pad bus is flipped per engine via
# the synth_engine module global (read at __init__ time).
os.environ["STAVE_FAUST_OSC_BANK"] = "1"
os.environ.setdefault("STAVE_FAUST_PAD_BUS", "0")

import numpy as np  # noqa: E402

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from stave_synth import synth_engine as se  # noqa: E402

SR = 48000
N_BLK = 256
N_BLOCKS = 2950          # 1300 Phase 1 + 800 Phase 2 shimmer + 850 Phase 4 LFO
SEED = 0xC0DE
BAR = 1e-6

CHORD_A = [48, 55, 60, 64]      # C3 G3 C4 E4
CHORD_B = [50, 57, 62, 65]      # D3 A3 D4 F4
BURSTS = [72, 76, 79]           # staccato transients
DRONE = 36                      # held throughout so skip_voices never trips
                                # (a silent gap would route blocks off the
                                # Faust bank and out of the region under test)


def configure(e):
    """Deterministic, region-exercising baseline. Identical for both."""
    e.unison_voices = 3                  # Faust bank requirement
    e.osc1_waveform = "saw"
    e.osc2_waveform = "square"
    e.osc1_blend = 0.6
    e._osc1_blend_cur = 0.6
    e.osc2_blend = 0.4
    e._osc2_blend_cur = 0.4
    e.analog_drift_cents = 0.0           # no RNG in the render path
    e.filter_drift_cents = 0.0
    e.filter_wobble_amount = 0.0
    e.sympathetic_enabled = False        # isolate the send tap
    e.shimmer_enabled = False
    e.reverb.dry_wet = 0.5               # exercise wet path in final compare


# (block index, description, callable) — applied before rendering that block.
def scenario():
    return [
        (0,    "fast-path sends, filter 8k/12dB",
               lambda e: [e.note_on(DRONE, 0.7)] + [e.note_on(n, 0.8) for n in CHORD_A]),
        (150,  "filter sweep down to 300 Hz",
               lambda e: setattr(e, "filter_cutoff", 300.0)),
        (300,  "slope 24 + resonance 2.0",
               lambda e: (setattr(e, "filter_slope", 24),
                          setattr(e, "filter_resonance", 2.0))),
        (450,  "filter sweep up to 12 kHz",
               lambda e: setattr(e, "filter_cutoff", 12000.0)),
        (520,  "pan spread (Haas engages via plain pans)",
               lambda e: (setattr(e, "osc1_pan", -0.8),
                          setattr(e, "osc2_pan", 0.75))),
        (580,  "hard-pan WIDE",
               lambda e: setattr(e, "osc_hard_pan", True)),
        (640,  "slow-path sends 0.5 / 1.2",
               lambda e: (setattr(e, "osc1_reverb_send", 0.5),
                          setattr(e, "osc2_reverb_send", 1.2))),
        (700,  "burst chord on",
               lambda e: [e.note_on(n, 0.9) for n in BURSTS]),
        (706,  "burst chord off",
               lambda e: [e.note_off(n) for n in BURSTS]),
        (760,  "highpass 180 Hz",
               lambda e: setattr(e, "filter_highpass_hz", 180.0)),
        (850,  "OSC1 independent filter 1.5 kHz",
               lambda e: (setattr(e, "osc1_filter_enabled", False),
                          setattr(e, "osc1_indep_cutoff", 1500.0))),
        (950,  "chord change",
               lambda e: ([e.note_off(n) for n in CHORD_A]
                          + [e.note_on(n, 0.75) for n in CHORD_B])),
        (1050, "burst on/off again",
               lambda e: [e.note_on(n, 0.85) for n in BURSTS]),
        (1058, "burst off",
               lambda e: [e.note_off(n) for n in BURSTS]),
        (1100, "OSC2 independent filter 900 Hz",
               lambda e: (setattr(e, "osc2_filter_enabled", False),
                          setattr(e, "osc2_indep_cutoff", 900.0))),
        (1200, "osc2 blend → 0 (render_osc2 gate)",
               lambda e: setattr(e, "osc2_blend", 0.0)),
        # ── Phase 2: shimmer chain ──
        # Two scenario constraints inherited from documented Phase-1
        # transition-only divergences (NOT shimmer behavior):
        #   1. Pans come back inside the Haas threshold BEFORE osc2
        #      re-engages — re-engaging Haas replays stale Python ring
        #      content vs Faust zeros.
        #   2. Sends stay on the (warm since block 640) slow path first,
        #      then move slow→fast ONCE — a fast→slow re-entry would pit
        #      Python's frozen-warm send-filter state against Faust's
        #      decayed state.
        (1300, "shimmer ON +12 over slow-path sends",
               lambda e: (setattr(e, "osc_hard_pan", False),
                          setattr(e, "osc1_pan", 0.0),
                          setattr(e, "osc2_pan", 0.3),
                          setattr(e, "osc2_blend", 0.4),
                          setattr(e, "shimmer_mix", 0.6),
                          setattr(e, "shimmer_enabled", True))),
        (1400, "shimmer mix sweep → 0.15",
               lambda e: setattr(e, "shimmer_mix", 0.15)),
        (1480, "shimmer +24 (HP → 1200 Hz, state carried)",
               lambda e: setattr(e, "shimmer_high", True)),
        (1560, "shimmer back to +12 (HP → 400 Hz)",
               lambda e: setattr(e, "shimmer_high", False)),
        (1640, "sends → fast path, shimmer still on",
               lambda e: (setattr(e, "osc1_reverb_send", 1.0),
                          setattr(e, "osc2_reverb_send", 1.0),
                          setattr(e, "shimmer_mix", 0.5))),
        (1720, "CLOUD send → 0 (ring freezes)",
               lambda e: setattr(e, "shimmer_send", 0.0)),
        (1800, "CLOUD send → 1.6 (frozen-ring re-engage)",
               lambda e: setattr(e, "shimmer_send", 1.6)),
        (1860, "shimmer OFF (HP state freezes)",
               lambda e: setattr(e, "shimmer_enabled", False)),
        (1920, "shimmer ON again (frozen-HP re-engage)",
               lambda e: setattr(e, "shimmer_enabled", True)),
        (2000, "mix → 0.0005 (render_shimmer threshold gate)",
               lambda e: setattr(e, "shimmer_mix", 0.0005)),
        (2040, "mix → 0.5 (gate re-engage)",
               lambda e: setattr(e, "shimmer_mix", 0.5)),
        # ── Phase 4: LFO amp/pan (merged run applies these in pad_bus.dsp
        # on all-recv blocks; runs A/B keep the Python application — every
        # run must land on the same samples). Sends stay on the fast path
        # and Haas stays disengaged throughout (Phase-1 transition rules).
        # Shapes avoid "sh" so the render path stays RNG-free.
        (2100, "LFO1 amp sine 2 Hz d0.5",
               lambda e: (setattr(e, "lfo_target", "amp"),
                          setattr(e, "lfo_rate_hz", 2.0),
                          setattr(e, "lfo_depth", 0.5))),
        (2170, "LFO1 → pan, spread 0.5",
               lambda e: (setattr(e, "lfo_target", "pan"),
                          setattr(e, "lfo_spread", 0.5))),
        (2240, "LFO2 amp tri 0.8 Hz d0.7 sprd 0.6",
               lambda e: (setattr(e, "lfo2_target", "amp"),
                          setattr(e, "lfo2_shape", "triangle"),
                          setattr(e, "lfo2_rate_hz", 0.8),
                          setattr(e, "lfo2_spread", 0.6),
                          setattr(e, "lfo2_depth", 0.7))),
        (2310, "LFO smoothing on (0.4 / 0.15)",
               lambda e: (setattr(e, "lfo_smooth", 0.4),
                          setattr(e, "lfo2_smooth", 0.15))),
        (2380, "LFO1 → amp d1.0 (0.7 cap, dual gates)",
               lambda e: (setattr(e, "lfo_target", "amp"),
                          setattr(e, "lfo_depth", 1.0))),
        (2450, "recv split: osc2 drops LFO1 (fallback)",
               lambda e: setattr(e, "osc2_recv_lfo1", False)),
        (2520, "all-recv restored (state handoff)",
               lambda e: setattr(e, "osc2_recv_lfo1", True)),
        (2590, "poly LFO1 (amp per-voice in bank)",
               lambda e: setattr(e, "lfo_poly", True)),
        (2660, "poly off (amp back on the bus)",
               lambda e: setattr(e, "lfo_poly", False)),
        (2730, "LFO2 → bus (post-reverb, Python)",
               lambda e: setattr(e, "lfo2_target", "bus")),
        (2800, "LFO2 → pan saw + smooth 0.3",
               lambda e: (setattr(e, "lfo2_target", "pan"),
                          setattr(e, "lfo2_shape", "saw"),
                          setattr(e, "lfo2_smooth", 0.3))),
        (2870, "LFO depths → 0 (idle path)",
               lambda e: (setattr(e, "lfo_depth", 0.0),
                          setattr(e, "lfo2_depth", 0.0))),
    ]


def run_engine(pad_bus: bool, merged: bool = False):
    se.USE_FAUST_PAD_BUS = pad_bus       # module global, read at __init__
    se.USE_FAUST_MERGED = merged
    np.random.seed(SEED)
    e = se.SynthEngine(sample_rate=SR)
    if pad_bus:
        assert e._faust_pad_bus is not None, (
            "STAVE_FAUST_PAD_BUS engine has no pad bus — build "
            "faust/libstave_pad_bus.so (faust/build.sh) and retry")
    if merged:
        assert e._faust_merged is not None, (
            "STAVE_FAUST_MERGED engine has no shim — build "
            "faust/libstave_merged_shim.so (faust/build.sh) and retry")
    assert e._faust_osc_bank is not None, "Faust osc bank required"
    configure(e)

    dry = np.zeros((2, N_BLOCKS * N_BLK))
    send = np.zeros((2, N_BLOCKS * N_BLK))
    final = np.zeros((2, N_BLOCKS * N_BLK))

    # Tap the dry bus at the ping-pong entry (instance-attr shadowing —
    # zero changes to engine code).
    blk_ref = {"i": 0}
    orig_pp = e._process_ping_pong

    def tapped_pp(out_l, out_r):
        i = blk_ref["i"]
        dry[0, i * N_BLK:(i + 1) * N_BLK] = out_l
        dry[1, i * N_BLK:(i + 1) * N_BLK] = out_r
        return orig_pp(out_l, out_r)

    e._process_ping_pong = tapped_pp

    # Tap the reverb input (post 0.6-trim + tanh).
    orig_rev = e.reverb.process

    def tapped_rev(x):
        i = blk_ref["i"]
        send[0, i * N_BLK:(i + 1) * N_BLK] = x[0]
        send[1, i * N_BLK:(i + 1) * N_BLK] = x[1]
        return orig_rev(x)

    e.reverb.process = tapped_rev

    events = dict((b, (d, fn)) for b, d, fn in scenario())
    for i in range(N_BLOCKS):
        blk_ref["i"] = i
        if i in events:
            events[i][1](e)
        l, r = e.render(N_BLK)
        final[0, i * N_BLK:(i + 1) * N_BLK] = l
        final[1, i * N_BLK:(i + 1) * N_BLK] = r

    # Contract: 9 outputs, bypass channels stay silent.
    if pad_bus:
        pb_out = e._faust_merged._bus_out if merged else e._faust_pad_bus._out
        assert pb_out.shape[0] == 9
        assert np.all(pb_out[4] == 0.0) and np.all(pb_out[5] == 0.0), \
            "bypass channels must be zero (reserved)"

    del e
    gc.collect()
    return dry, send, final


def report(tag, ref, run):
    """Per-segment + full-run peak |delta| table for one run pair."""
    dry_a, send_a, fin_a = ref
    dry_b, send_b, fin_b = run
    segs = scenario()
    bounds = [b for b, _, _ in segs] + [N_BLOCKS]
    channels = {
        "dry_L": (dry_a[0], dry_b[0]), "dry_R": (dry_a[1], dry_b[1]),
        "send_L": (send_a[0], send_b[0]), "send_R": (send_a[1], send_b[1]),
        "final_L": (fin_a[0], fin_b[0]), "final_R": (fin_a[1], fin_b[1]),
    }

    print(f"\n══ {tag} ══")
    print(f"{'segment':<44}" + "".join(f"{k:>10}" for k in channels))
    for si in range(len(segs)):
        lo, hi = bounds[si] * N_BLK, bounds[si + 1] * N_BLK
        row = f"[{bounds[si]:>4}:{bounds[si+1]:>4}] {segs[si][1]:<34.34}"
        for k, (a, b) in channels.items():
            row += f"{np.abs(a[lo:hi] - b[lo:hi]).max():>10.2e}"
        print(row)

    print("-" * (44 + 10 * len(channels)))
    ok = True
    row = f"{'PEAK over full run':<44}"
    for k, (a, b) in channels.items():
        p = float(np.abs(a - b).max())
        ok &= p <= BAR
        row += f"{p:>10.2e}"
    print(row)
    return ok


def main():
    print("Rendering Python-skeleton engine (STAVE_FAUST_PAD_BUS off)...")
    ref = run_engine(pad_bus=False)
    print("Rendering Faust pad-bus engine (PAD_BUS on, MERGED off)...")
    two_call = run_engine(pad_bus=True)
    print("Rendering merged engine (PAD_BUS + MERGED on, one C call)...")
    merged = run_engine(pad_bus=True, merged=True)

    ok = report("run B: two-call pad bus vs Python reference", ref, two_call)
    ok &= report("run C: MERGED shim + in-Faust LFO vs Python reference", ref, merged)

    dry_a, send_a, fin_a = ref
    print(f"\nsignal peaks (sanity): dry {np.abs(dry_a).max():.3f}  "
          f"send {np.abs(send_a).max():.3f}  final {np.abs(fin_a).max():.3f}")
    print(f"\nparity bar {BAR:.0e}: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
