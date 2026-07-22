"""Parity harness for the Faust piano chain (Phase 3 render-skeleton port).

Drives two FluidSynthPlayer instances through an IDENTICAL multi-block
scenario — one on the Python DSP path (STAVE_FAUST_PIANO_CHAIN off) and
one on the Faust chain (flag on) — and compares render_block output.

Input-identity choice (documented per the plan): instead of running two
live FluidSynth engines (2× soundfont RAM, and input identity would rest
on an ASSUMPTION of cross-instance determinism), ONE real FluidSynth
instance (FluidR3_GM acoustic grand, fixed note schedule, no RNG)
pre-renders the int16 stream once, and both players replay the SAME array
through a dummy `fs` stub. Input identity is then guaranteed by
construction, and every delta below is attributable to the ported region
alone. The signal is the actual production signal class (real hammered
piano samples: sharp attacks for the comp, decaying broadband tails for
the filters).

Chain coverage: volume smoothing incl. the ≤0.001 hard-zero branch and
recovery, voicing switches (lowcut/highcut/all-EQ retune), individual EQ
band retune + disable/enable (frozen-zi re-engage must be sample-exact),
LA-2A comp on/off/wet-sweep/drive (the block-rate envelope stays Python,
fed by the module's mono tap), velocity-brightness sweeps, tremolo, and
the piano-room mix + its off/on clear (Python-side, identical code both
players — included to prove end-to-end integration).

Also proves the divide-hazard resolution (CR#9 dumb-list item 6): the
legacy wet path recovers the comp gain ramp as compressed/safe_mono; the
Faust path applies the ramp directly. The harness records every
(mono, ramp) pair the PYTHON player produced and reports
max |compressed/safe_mono − ramp| over guarded samples plus the number of
epsilon-guard trips (|mono| ≤ 1e-10) — the only samples where the two
formulations can differ beyond ~1 ulp, and where the divide formulation
is the wrong one (single-sample wet dropout at anticorrelated zero
crossings).

Parity bar: max |delta| <= 1e-6 on both output channels.

Run:  cd stave-synth && venv/bin/python tools/compare_piano_chain.py
"""
from __future__ import annotations

import gc
import os
import sys
from pathlib import Path

os.environ.setdefault("STAVE_FAUST_PIANO_CHAIN", "0")

import numpy as np  # noqa: E402

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from stave_synth import fluidsynth_player as fp  # noqa: E402

SR = 48000
N_BLK = 256
N_BLOCKS = 1400          # ~7.5 s
BAR = 1e-6

SF2 = "/usr/share/sounds/sf2/FluidR3_GM.sf2"


class DummyFS:
    """Stands in for fluidsynth.Synth: replays a pre-rendered int16 stream
    so both players consume byte-identical input."""

    def __init__(self, stream: np.ndarray, blk: int):
        self._stream = stream
        self._blk = blk
        self._i = 0

    def get_samples(self, n):
        assert n == self._blk
        s = self._stream[self._i * 2 * n:(self._i + 1) * 2 * n]
        self._i += 1
        return s

    def noteon(self, *a):
        pass

    def noteoff(self, *a):
        pass

    def pitch_bend(self, *a):
        pass


def render_source() -> np.ndarray:
    """One real FluidSynth render — deterministic fixed schedule, reverb/
    chorus off (same settings the player uses)."""
    import fluidsynth
    fs = fluidsynth.Synth(samplerate=float(SR))
    fs.setting("synth.gain", 1.0)
    fs.setting("synth.reverb.active", 0)
    fs.setting("synth.chorus.active", 0)
    sfid = fs.sfload(SF2)
    assert sfid >= 0, f"could not load {SF2}"
    fs.program_select(0, sfid, 0, 0)

    events = {
        0:    [(48, 90), (60, 100), (64, 92), (67, 96)],
        180:  [(64, 0), (67, 0), (65, 105), (69, 98)],
        330:  [(48, 0), (60, 0), (65, 0), (69, 0),
               (50, 88), (62, 96), (66, 90), (69, 94)],
        470:  [(81, 120)],                       # hard accents → comp food
        480:  [(81, 0), (79, 115)],
        490:  [(79, 0), (77, 110)],
        500:  [(77, 0)],
        640:  [(50, 0), (62, 0), (66, 0), (69, 0),
               (43, 92), (55, 84), (59, 88), (62, 90)],
        830:  [(84, 38), (88, 34)],              # soft top → velbright food
        930:  [(84, 0), (88, 0)],
        1050: [(43, 0), (55, 0), (59, 0), (62, 0),
               (36, 100), (48, 96), (52, 90), (55, 94)],
        1250: [(60, 108), (64, 102), (67, 104)],
    }
    out = np.empty(N_BLOCKS * N_BLK * 2, dtype=np.int16)
    for i in range(N_BLOCKS):
        for note, vel in events.get(i, []):
            if vel > 0:
                fs.noteon(0, note, vel)
            else:
                fs.noteoff(0, note)
        out[i * N_BLK * 2:(i + 1) * N_BLK * 2] = fs.get_samples(N_BLK)
    fs.delete()
    return out


# (block index, description, callable) — applied before rendering that block.
def scenario():
    return [
        (0,    "acoustic voicing, room 0.35, vol 0.8",
               lambda p: None),
        (60,   "comp ON (thr -24, 3:1, knee 18, drive +6, wet 0.7)",
               lambda p: (setattr(p, "comp_enabled", True),
                          setattr(p, "comp_threshold_db", -24.0),
                          setattr(p, "comp_ratio", 3.0),
                          setattr(p, "comp_knee_db", 18.0),
                          setattr(p, "comp_drive_db", 6.0),
                          setattr(p, "comp_makeup_db", 2.0),
                          setattr(p, "comp_wet", 0.7))),
        (170,  "voicing → bright (lowcut/highcut/EQ retune)",
               lambda p: p.set_voicing("bright")),
        (290,  "EQ band 2 retune (-6 dB, Q 2)",
               lambda p: p.set_eq_band(2, gain_db=-6.0, q=2.0)),
        (340,  "EQ band 2 DISABLED (zi freezes)",
               lambda p: p.set_eq_band(2, enabled=False)),
        (400,  "band 2 retuned while frozen",
               lambda p: p.set_eq_band(2, freq_hz=3200.0, gain_db=-4.0)),
        (440,  "EQ band 2 re-enabled (frozen re-engage)",
               lambda p: p.set_eq_band(2, enabled=True)),
        (520,  "volume 0.8 → 0.5",
               lambda p: p.set_volume(0.5)),
        (600,  "voicing → mellow",
               lambda p: p.set_voicing("mellow")),
        (660,  "comp wet → 0.25",
               lambda p: setattr(p, "comp_wet", 0.25)),
        (720,  "comp OFF (envelope freezes)",
               lambda p: setattr(p, "comp_enabled", False)),
        (780,  "comp ON again",
               lambda p: setattr(p, "comp_enabled", True)),
        (830,  "velbright ON (amount 0.8)",
               lambda p: (setattr(p, "vel_bright_enabled", True),
                          setattr(p, "vel_bright_amount", 0.8))),
        (880,  "vel tracker → soft (0.15)",
               lambda p: setattr(p, "_vel_tracker", 0.15)),
        (960,  "vel tracker → hard (0.95)",
               lambda p: setattr(p, "_vel_tracker", 0.95)),
        (1020, "tremolo ON (Suitcase 5.5 Hz / 0.5)",
               lambda p: (setattr(p, "tremolo_hz", 5.5),
                          setattr(p, "tremolo_depth", 0.5))),
        (1100, "volume → 0 (hard-zero branch)",
               lambda p: p.set_volume(0.0)),
        (1170, "volume → 0.85 (recovery)",
               lambda p: p.set_volume(0.85)),
        (1240, "piano room OFF (tank clear)",
               lambda p: setattr(p, "piano_room_enabled", False)),
        (1300, "piano room ON",
               lambda p: setattr(p, "piano_room_enabled", True)),
    ]


def run_player(stream: np.ndarray, faust: bool):
    fp.USE_FAUST_PIANO_CHAIN = faust     # module global, read at __init__
    from stave_synth.faust_piano_room import FaustPianoRoom
    p = fp.FluidSynthPlayer(SR)
    if faust:
        assert p._faust_chain is not None, (
            "Faust engine has no piano chain — build "
            "faust/libstave_piano_chain.so (faust/build.sh) and retry")
    else:
        assert p._faust_chain is None
    p.fs = DummyFS(stream, N_BLK)
    p.enabled = True
    p._active_notes = 1                  # defeat the silence skip (its early
                                         # return sits upstream of the branch
                                         # and never touches either DSP path)
    p._piano_room = FaustPianoRoom(SR)
    p.piano_room_enabled = True
    p.reverb_dry_wet = 0.35
    p.set_volume(0.8)
    p._volume_cur = 0.8

    # Record the Python player's (sidechain, ramp) pairs for the
    # divide-equivalence proof (both players run the same envelope code;
    # recording the legacy player documents the legacy formulation).
    comp_records = []
    if not faust:
        orig_ramp = p._comp_gain_ramp

        def rec_ramp(samples):
            ramp = orig_ramp(samples)
            comp_records.append((samples.copy(), ramp.copy()))
            return ramp

        p._comp_gain_ramp = rec_ramp

    events = dict((b, fn) for b, _, fn in scenario())
    out = np.zeros((2, N_BLOCKS * N_BLK))
    for i in range(N_BLOCKS):
        if i in events:
            events[i](p)
        blk = p.render_block(N_BLK)
        out[:, i * N_BLK:(i + 1) * N_BLK] = blk

    del p
    gc.collect()
    return out, comp_records


def divide_equivalence_report(comp_records):
    """Prove ramp-direct == divide-recovered on the real material: the
    legacy path computes wet_gain = (mono·ramp)/safe_mono; the Faust path
    uses ramp directly. Outside the epsilon guard the two differ by ~1 ulp
    of relative rounding; guard trips are the pathological samples where
    the legacy formulation itself glitches (wet path drops to dry)."""
    worst = 0.0
    guard_trips = 0
    n_samps = 0
    max_ramp_at_trip = 0.0
    for mono, ramp in comp_records:
        n_samps += mono.size
        compressed = mono * ramp
        guarded = np.abs(mono) > 1e-10
        safe = np.where(guarded, mono, 1.0)
        wet_div = compressed / safe
        if guarded.any():
            worst = max(worst, float(np.abs(wet_div[guarded] - ramp[guarded]).max()))
        trips = int((~guarded).sum())
        guard_trips += trips
        if trips:
            max_ramp_at_trip = max(max_ramp_at_trip, float(np.abs(ramp[~guarded]).max()))
    print(f"\nLA-2A divide-hazard equivalence ({len(comp_records)} comp blocks, "
          f"{n_samps} sidechain samples):")
    print(f"  max |divide-recovered wet_gain - direct ramp| "
          f"(|mono| > 1e-10): {worst:.3e}")
    print(f"  epsilon-guard trips (|mono| <= 1e-10): {guard_trips}"
          + (f" (max |ramp| there {max_ramp_at_trip:.3e} — legacy would have "
             f"dropped that sample's wet path)" if guard_trips else ""))
    return worst


def main():
    print("Pre-rendering FluidSynth source (one instance, fixed schedule)...")
    stream = render_source()
    peak_in = float(np.abs(stream).max()) / 32768.0
    print(f"  {N_BLOCKS} blocks x {N_BLK}, raw peak {peak_in:.3f}")
    assert peak_in > 0.05, "source is near-silent — schedule broken"

    print("Rendering Python-chain player (STAVE_FAUST_PIANO_CHAIN off)...")
    out_a, comp_records = run_player(stream, faust=False)
    print("Rendering Faust-chain player (STAVE_FAUST_PIANO_CHAIN on)...")
    out_b, _ = run_player(stream, faust=True)

    segs = scenario()
    bounds = [b for b, _, _ in segs] + [N_BLOCKS]
    print(f"\n{'segment':<58}{'out_L':>10}{'out_R':>10}")
    for si in range(len(segs)):
        lo, hi = bounds[si] * N_BLK, bounds[si + 1] * N_BLK
        dl = np.abs(out_a[0, lo:hi] - out_b[0, lo:hi]).max()
        dr = np.abs(out_a[1, lo:hi] - out_b[1, lo:hi]).max()
        print(f"[{bounds[si]:>4}:{bounds[si+1]:>4}] {segs[si][1]:<48.48}"
              f"{dl:>10.2e}{dr:>10.2e}")

    peak_l = float(np.abs(out_a[0] - out_b[0]).max())
    peak_r = float(np.abs(out_a[1] - out_b[1]).max())
    print("-" * 78)
    print(f"{'PEAK over full run':<58}{peak_l:>10.2e}{peak_r:>10.2e}")
    print(f"\nsignal peaks (sanity): python {np.abs(out_a).max():.3f}  "
          f"faust {np.abs(out_b).max():.3f}")

    divide_equivalence_report(comp_records)

    ok = peak_l <= BAR and peak_r <= BAR
    print(f"\nparity bar {BAR:.0e}: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
