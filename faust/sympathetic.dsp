declare name "stave_sympathetic";
declare description "Per-note sympathetic resonance — stereo-detuned sines with slow gain ramp";

import("stdfaust.lib");

// ═══════════════════════════════════════════════════════════════════════
// Port of SynthEngine's sympathetic_state rendering (synth_engine.py:2373).
// Python loops active notes; Faust runs a fixed bank of 16 slots. Python
// assigns slots at set_sympathetic_notes() time (same slot-pool pattern as
// the osc bank). HF rolloff (min(1, 523/max(freq, 523))) is computed in
// Python and pre-multiplied into `symp_gate` so Faust stays simple.
// ═══════════════════════════════════════════════════════════════════════

N_SLOTS = 16;
TWO_PI  = 2.0 * ma.PI;
SR      = ma.SR;
STEREO_DETUNE = 1.003;      // R phasor runs 0.3% faster for stereo width
// Beat-frequency cap in Hz: above ~1 kHz the 0.3 % ratio yields a beat
// rate that feels like tremolo instead of chorus (6 Hz at C7). Capping
// the absolute detune at 3 Hz keeps high-register resonances chorus-y.
MAX_BEAT_HZ = 3.0;

// Gain smoother with ~150 ms time constant — matches Python's
// `decay_rate = exp(-1/(0.15 * SR))` per-sample ramp toward target.
symp_smoother = si.smooth(1.0 - 1.0 / (0.15 * SR));

// Global
sym_level = hslider("sym_level", 0.035, 0.0, 1.0, 0.0001) : si.smoo;

// Per-slot params: freq (Hz) + gate (target gain × rolloff, smoothed here)
symp_freq(i) = hslider("symp_freq_s%i", 0, 0, 12000, 0.01);
symp_gate(i) = hslider("symp_gate_s%i", 0, 0, 1, 0.001) : symp_smoother;

// One slot: two phasors (L, R detuned), sine, scaled by gate × sym_level
slot_stereo(i) = sin(TWO_PI * phasor_l) * g, sin(TWO_PI * phasor_r) * g
with {
    inc_l    = symp_freq(i) / SR;
    // Detune = min(freq * ratio, freq + MAX_BEAT_HZ). Ratio dominates at
    // low registers (below ~1 kHz where 0.3 % < 3 Hz); absolute-offset
    // dominates above that, capping flutter at MAX_BEAT_HZ so high notes
    // don't turn into a fast tremolo.
    detuned  = min(symp_freq(i) * STEREO_DETUNE, symp_freq(i) + MAX_BEAT_HZ);
    inc_r    = detuned / SR;
    phasor_l = (+(inc_l) : ma.frac) ~ _;
    phasor_r = (+(inc_r) : ma.frac) ~ _;
    g        = symp_gate(i) * sym_level;
};

// Bank: sum all slots → stereo
bank = par(i, N_SLOTS, slot_stereo(i)) :> _, _;

process = bank;
