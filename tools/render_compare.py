"""Render the same C major chord through both the Python osc bank and the
Faust osc bank. Write each to a .wav file for side-by-side listening.

Minimal: no reverb, no filter, no shimmer — just osc1+osc2 with unison=3,
pan, ADSR envelope. Pure oscillator sound comparison.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from stave_synth.synth_engine import (  # noqa: E402
    SynthEngine, ADSREnvelope, ADSRConfig, generate_waveform
)
from stave_synth.faust_osc_bank import FaustOscBank, NVOICES  # noqa: E402

SR = 48000
N_BLK = 256
SECONDS = 3.0
N_BLOCKS = int(SECONDS * SR / N_BLK)

# C major chord
CHORD = [60, 64, 67]   # C4, E4, G4
VEL = 0.8

# Match the current synth defaults
OSC1_WF, OSC2_WF = "sine", "square"
OSC1_BLEND, OSC2_BLEND = 0.6, 0.4
OSC1_OCT, OSC2_OCT = 0, 0
UNI_DETUNE, UNI_SPREAD = 0.07, 0.85
OSC1_PAN, OSC2_PAN = 0.0, 0.0

ADSR_CFG = ADSRConfig(attack_ms=200.0, decay_ms=1500.0, sustain_percent=80.0,
                      release_ms=500.0)


def midi_to_hz(n):
    return 440.0 * (2.0 ** ((n - 69) / 12.0))


def render_faust():
    bank = FaustOscBank(SR)
    bank.set_osc_params(
        OSC1_WF, OSC2_WF, OSC1_BLEND, OSC2_BLEND, OSC1_OCT, OSC2_OCT,
        UNI_DETUNE, UNI_SPREAD, OSC1_PAN, OSC2_PAN,
    )

    # Build ADSR envelope per voice
    envs = [ADSREnvelope(ADSR_CFG, SR) for _ in CHORD]
    for e in envs:
        e.trigger()

    # Assign chord notes to slots 0..2
    freqs = [midi_to_hz(n) for n in CHORD]
    for slot, f in enumerate(freqs):
        bank.set_voice(slot, f, 0.0)  # gate starts at 0, ADSR ramps

    out_l = np.zeros(N_BLOCKS * N_BLK, dtype=np.float32)
    out_r = np.zeros(N_BLOCKS * N_BLK, dtype=np.float32)

    release_block = int(N_BLOCKS * 0.66)  # release at 2/3 in

    for blk in range(N_BLOCKS):
        # Release notes 2/3 through
        if blk == release_block:
            for e in envs:
                e.release()

        # Compute ADSR per voice, write gate = env × velocity
        for slot, e in enumerate(envs):
            env = e.process(N_BLK)
            if isinstance(env, np.ndarray):
                g = float(env[-1]) * VEL  # block-final value
            else:
                g = float(env) * VEL
            bank.set_voice(slot, freqs[slot], g)

        stereo = bank.process(N_BLK)
        # output is 4 channels (osc1_l, osc1_r, osc2_l, osc2_r); mix osc1+osc2
        mixed_l = (stereo[0] + stereo[2]).astype(np.float32)  # ch0 + ch2
        mixed_r = (stereo[1] + stereo[3]).astype(np.float32)
        out_l[blk * N_BLK:(blk + 1) * N_BLK] = mixed_l
        out_r[blk * N_BLK:(blk + 1) * N_BLK] = mixed_r

    return out_l, out_r


def render_python():
    """Use SynthEngine's actual render path so we're comparing apples to apples.
    Note: this will include the shared lowpass filter at 8kHz default — which
    the Faust bank doesn't have. For a cleaner match, bypass filter by raising
    cutoff.  """
    e = SynthEngine(sample_rate=SR)
    e.osc1_waveform = OSC1_WF; e.osc2_waveform = OSC2_WF
    e.osc1_blend = OSC1_BLEND; e.osc2_blend = OSC2_BLEND
    e._osc1_blend_cur = OSC1_BLEND; e._osc2_blend_cur = OSC2_BLEND
    e.osc1_octave = OSC1_OCT; e.osc2_octave = OSC2_OCT
    e.unison_voices = 3
    e.unison_detune = UNI_DETUNE
    e.unison_spread = UNI_SPREAD
    e.osc1_pan = OSC1_PAN; e.osc2_pan = OSC2_PAN
    e.adsr_config = ADSR_CFG
    # Bypass filter so we hear raw oscillators
    e.filter_cutoff = 20000.0
    e._filter_cutoff_cur = 20000.0
    e.filter_l.set_params(20000.0, 0.707)
    e.filter_r.set_params(20000.0, 0.707)
    e.filter2_l.set_params(20000.0, 0.707)
    e.filter2_r.set_params(20000.0, 0.707)
    # Disable reverb wet (we want dry osc sound)
    e.reverb.dry_wet = 0.0
    e.reverb.wet_gain = 0.0

    # Note-on
    for n in CHORD:
        e.note_on(n, int(VEL * 127))

    out_l = np.zeros(N_BLOCKS * N_BLK, dtype=np.float32)
    out_r = np.zeros(N_BLOCKS * N_BLK, dtype=np.float32)

    release_block = int(N_BLOCKS * 0.66)

    for blk in range(N_BLOCKS):
        if blk == release_block:
            for n in CHORD:
                e.note_off(n)
        l, r = e.render(N_BLK)
        out_l[blk * N_BLK:(blk + 1) * N_BLK] = l.astype(np.float32)
        out_r[blk * N_BLK:(blk + 1) * N_BLK] = r.astype(np.float32)

    return out_l, out_r


def save_wav(path, l, r):
    stereo = np.stack([l, r], axis=1)
    # Scale to int16 with headroom
    peak = max(0.01, np.max(np.abs(stereo)))
    stereo_int = np.clip(stereo / peak * 0.9, -1, 1) * 32767
    wavfile.write(path, SR, stereo_int.astype(np.int16))
    print(f"  wrote {path}  peak={peak:.3f}")


def main():
    print("Rendering Faust bank...")
    fl, fr = render_faust()
    save_wav("/tmp/faust_chord.wav", fl, fr)

    print("Rendering Python bank...")
    pl, pr = render_python()
    save_wav("/tmp/python_chord.wav", pl, pr)

    print("\nPlay via:")
    print("  aplay /tmp/python_chord.wav")
    print("  aplay /tmp/faust_chord.wav")


if __name__ == "__main__":
    main()
