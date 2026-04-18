declare name "stave_osc_bank";
declare description "16-voice polyphonic osc bank — osc1+osc2, 5 waveforms, unison=3, stereo pan";

import("stdfaust.lib");

// ═══════════════════════════════════════════════════════════════════════
// Config constants
// ═══════════════════════════════════════════════════════════════════════
NVOICES = 16;
UNI     = 3;           // fixed unison count for this iteration (default)
TWO_PI  = 2.0 * ma.PI;
SR      = ma.SR;

// ═══════════════════════════════════════════════════════════════════════
// Per-voice runtime params: freq (Hz) + gate (ADSR env × velocity from Py)
// Python writes freq when note_on, gate every block from its ADSR path.
// ═══════════════════════════════════════════════════════════════════════
voice_freq(i) = hslider("freq_v%i",   0, 0, 12000, 0.01);
voice_gate(i) = hslider("gate_v%i",   0, 0, 1,     0.001) : si.smoo;

// ═══════════════════════════════════════════════════════════════════════
// Global oscillator params (apply to all voices)
// Octave multipliers are sent pre-computed (2^octave) so Python owns the
// octave enum. Waveforms indexed 0..4: sine, square, saw, triangle, saturated.
// ═══════════════════════════════════════════════════════════════════════
osc1_wf    = hslider("osc1_wf",       0,  0, 4, 1);
osc2_wf    = hslider("osc2_wf",       1,  0, 4, 1);
osc1_blend = hslider("osc1_blend",  0.6,  0, 1, 0.001) : si.smoo;
osc2_blend = hslider("osc2_blend",  0.4,  0, 1, 0.001) : si.smoo;
osc1_oct   = hslider("osc1_oct",      1, 0.125, 8, 0.125);
osc2_oct   = hslider("osc2_oct",      1, 0.125, 8, 0.125);
uni_det    = hslider("uni_detune", 0.07,  0, 1, 0.001);
uni_sprd   = hslider("uni_spread", 0.85,  0, 1, 0.001);
osc1_pan   = hslider("osc1_pan",      0, -1, 1, 0.001);
osc2_pan   = hslider("osc2_pan",      0, -1, 1, 0.001);

// Shimmer — octave-up sine per voice, summed mono, sent to reverb by Python.
// `shimmer_mult`: 2.0 = +12 st, 4.0 = +24 st (Python picks one based on shimmer_high).
// `shimmer_enable`: 0/1 flag — Faust always computes shimmer; this masks output.
shimmer_mult   = hslider("shimmer_mult", 2.0, 1.0, 4.0, 0.001);
shimmer_enable = hslider("shimmer_enable", 0.0, 0.0, 1.0, 1.0);

// Chord drone — two sines (root + fifth) one octave below the played note,
// using osc1's waveform shape. Python writes the already-smoothed freqs
// + the combined `gain × level × fade_scale` every block.
// Gain coefficients (0.30 root, 0.22 fifth) match synth_engine.py:2257-2261.
drone_root_freq  = hslider("drone_root_freq",  0, 0, 12000, 0.01);
drone_fifth_freq = hslider("drone_fifth_freq", 0, 0, 12000, 0.01);
drone_gain_lvl   = hslider("drone_gain_lvl",   0, 0, 2,     0.001) : si.smoo;

// ═══════════════════════════════════════════════════════════════════════
// Waveform generator — takes a wrapping phasor (0..1), returns wave.
// 5 shapes selected by wf index (0..4). Using phasor avoids float32
// precision drift that an unbounded `+(inc) ~ _` accumulator suffers:
// unbounded phase grows by ~inc/sample, eating mantissa bits over time
// → audible pitch drift after a few seconds of sustain.
// ═══════════════════════════════════════════════════════════════════════
wave_gen(wf, ph01) = select5(int(wf), w_sine, w_square, w_saw, w_tri, w_sat)
with {
    theta     = TWO_PI * ph01;         // 0..2π for math-based waves
    w_sine    = sin(theta);
    w_square  = ma.signum(w_sine);
    w_saw     = 2.0 * ph01 - 1.0;
    w_tri     = 2.0 * abs(w_saw) - 1.0;
    w_sat     = ma.tanh(4.0 * w_sine);
    select5(i, a, b, c, d, e) =
        select2(i < 1, select2(i < 2,
            select2(i < 3, select2(i < 4, e, d), c), b), a);
};

// ═══════════════════════════════════════════════════════════════════════
// Unison helpers — detune factor and pan offset per unison copy
// UNI=3 → factor(u) ∈ { -1, 0, +1 }
// ═══════════════════════════════════════════════════════════════════════
uni_factor(u)     = 2.0 * u / (UNI - 1) - 1.0;
uni_det_mult(u)   = pow(2.0, uni_det * uni_factor(u) / 12.0);
uni_pan_off(u)    = uni_factor(u) * uni_sprd;

// Equal-power pan law — matches Python (line 1930–1940)
//   pan_shaped = sign(p) · |p|^0.7
//   angle = (pan_shaped + 1) · π/4
//   gL = cos(angle) · √2,  gR = sin(angle) · √2
pan_angle(base_pan, u) = (p_shape + 1.0) * 0.25 * ma.PI
with {
    p_clip  = max(-1.0, min(1.0, base_pan + uni_pan_off(u)));
    p_shape = ma.signum(p_clip) * pow(abs(p_clip), 0.7);
};
pan_gL(base_pan, u) = cos(pan_angle(base_pan, u)) * sqrt(2.0);
pan_gR(base_pan, u) = sin(pan_angle(base_pan, u)) * sqrt(2.0);

// ═══════════════════════════════════════════════════════════════════════
// Per-unison deterministic phase seed — decorrelates the 3 unison copies
// from each other so the "supersaw" onset has natural beating instead of
// everything constructive-summing at t=0 (Python's comment: "Aligned
// phases (all 0) cause audible beating/LFO-phasing when detune is small").
// Fixed per unison index → same every note; random per-note fidelity is
// addressed by the per-voice osc phase offsets above.
// ═══════════════════════════════════════════════════════════════════════
uni_phase_seed(u) = ba.take(u+1, (0.137, 0.532, 0.826));

// ═══════════════════════════════════════════════════════════════════════
// One unison copy: wrapping phasor + wave gen + pan → stereo.
// `phasor01` uses `ma.frac` each sample to stay in [0, 1) — precision
// stays constant regardless of how long the note is held.
// `phase_off` is added at wave-gen time (not into the accumulator) so
// adjusting the offset doesn't disturb the phasor's continuous flow.
// ═══════════════════════════════════════════════════════════════════════
unison_copy_phased(i, wf, oct_mult, blend, base_pan, u, phase_off) =
    wave * pan_gL(base_pan, u), wave * pan_gR(base_pan, u)
with {
    freq_per_sample = voice_freq(i) * oct_mult * uni_det_mult(u) / SR;
    phasor01        = (+(freq_per_sample) : ma.frac) ~ _;
    phased          = ma.frac(phasor01 + phase_off + uni_phase_seed(u));
    wave            = wave_gen(wf, phased) * blend;
};

// ═══════════════════════════════════════════════════════════════════════
// Sum all unison copies for one osc of one voice → stereo
// ═══════════════════════════════════════════════════════════════════════
one_osc_phased(i, wf, oct_mult, blend, base_pan, phase_off) =
    par(u, UNI, unison_copy_phased(i, wf, oct_mult, blend, base_pan, u, phase_off)) :> _, _;

// ═══════════════════════════════════════════════════════════════════════
// Unison gain normalization (matches Python line 2009):
//   scale = (1 + 0.15·(UNI-1)) / UNI
// UNI=3 → 0.4333. Applied once per osc output (not per unison copy).
// ═══════════════════════════════════════════════════════════════════════
UNI_SCALE = (1.0 + 0.15 * (UNI - 1)) / UNI;

// ═══════════════════════════════════════════════════════════════════════
// Per-voice phase offsets — Python writes a random value in [0, 1) at
// note_on so osc1 vs osc2 of the same voice are decorrelated (otherwise
// their phasors, starting from 0 with identical rates, stay perfectly
// in-phase forever → fundamentals sum coherently → ~2× gain overshoot).
// Default 0 for inactive slots; these sliders are only meaningful when
// the voice is active.
// ═══════════════════════════════════════════════════════════════════════
voice_osc1_phase_off(i) = hslider("osc1_phase_v%i", 0, 0, 1, 0.001);
voice_osc2_phase_off(i) = hslider("osc2_phase_v%i", 0, 0, 1, 0.001);

// ═══════════════════════════════════════════════════════════════════════
// One full voice — produces 4 channels: osc1_L, osc1_R, osc2_L, osc2_R
// ═══════════════════════════════════════════════════════════════════════
one_voice(i) =
    (one_osc_phased(i, osc1_wf, osc1_oct, osc1_blend, osc1_pan, voice_osc1_phase_off(i)) : scale_2ch),
    (one_osc_phased(i, osc2_wf, osc2_oct, osc2_blend, osc2_pan, voice_osc2_phase_off(i)) : scale_2ch)
with {
    g = voice_gate(i) * UNI_SCALE;
    scale_2ch = *(g), *(g);
};

// ═══════════════════════════════════════════════════════════════════════
// Shimmer — per-voice, sine-only, octave up, summed across unison then
// scaled by gate × UNI_SCALE × 0.30 (matches Python line 2074 exactly).
// Python still owns the post-shimmer pipeline (HP filter, mix fader,
// CLOUD multi-tap delay, reverb send).
// ═══════════════════════════════════════════════════════════════════════
SHIMMER_SEND = 0.30;

shimmer_copy(i, u) = sin(2.0 * ma.PI * phasor01)
with {
    freq_per_sample = voice_freq(i) * shimmer_mult * uni_det_mult(u) / SR;
    phasor01        = (+(freq_per_sample) : ma.frac) ~ _;
};

shimmer_voice(i) =
    par(u, UNI, shimmer_copy(i, u)) :> _
    : *(voice_gate(i) * UNI_SCALE * SHIMMER_SEND * shimmer_enable);

shimmer_bank = par(i, NVOICES, shimmer_voice(i)) :> _;

// ═══════════════════════════════════════════════════════════════════════
// Chord drone — two wrapping-phasor oscs using osc1's waveform shape.
// Separate `drone_osc` calls instantiate independent phasor state.
// ═══════════════════════════════════════════════════════════════════════
drone_osc(freq) = wave_gen(osc1_wf, phasor01)
with {
    phasor01 = (+(freq / SR) : ma.frac) ~ _;
};

drone_mono =
    (drone_osc(drone_root_freq)  * 0.30 +
     drone_osc(drone_fifth_freq) * 0.22) * drone_gain_lvl;

// ═══════════════════════════════════════════════════════════════════════
// Bank output:
//   [0] osc1_L  [1] osc1_R  [2] osc2_L  [3] osc2_R
//   [4] shimmer_mono  [5] drone_mono
// ═══════════════════════════════════════════════════════════════════════
osc_bank = par(i, NVOICES, one_voice(i)) :> _, _, _, _;

process = osc_bank, shimmer_bank, drone_mono;
