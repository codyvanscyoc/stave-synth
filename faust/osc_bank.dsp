declare name "stave_osc_bank";
declare description "16-voice polyphonic osc bank — osc1+osc2, 5 waveforms, unison=3, stereo pan";

import("stdfaust.lib");

// ═══════════════════════════════════════════════════════════════════════
// Config constants
// ═══════════════════════════════════════════════════════════════════════
NVOICES = 16;
// LIMITATION: UNI is a compile-time constant — the unison count is baked
// into the Faust topology (par(u, UNI, ...) iterates statically). The UI
// lets the user pick 1, 3, or 5 unison voices; only 3 runs on this Faust
// path. faust_osc_bank.py exposes `FaustOscBank.supports_unison(n)` so
// synth_engine.py can route 1/5 cases to the Python oscillator path.
// Changing this requires a full rebuild (faust/build.sh).
UNI     = 3;           // fixed unison count for this iteration (default)
TWO_PI  = 2.0 * ma.PI;
SR      = ma.SR;

// ═══════════════════════════════════════════════════════════════════════
// Per-voice runtime params: freq (Hz) + gates (ADSR env × velocity from Py).
// Python writes freq when note_on. Gates per block:
//   gate_osc1_v%i / gate_osc2_v%i — per-OSC envelope × velocity (independent
//     ADSR shapes; multiplies into the osc1 / osc2 output streams).
//   gate_v%i — "voice alive" signal = max(env1, env2) × velocity. Used by
//     shimmer + any voice-lifetime-gated path. Keeping this separate from
//     the per-OSC gates means per-OSC envelope divergence doesn't cut
//     shimmer prematurely.
// ═══════════════════════════════════════════════════════════════════════
voice_freq(i) = hslider("freq_v%i",   0, 0, 12000, 0.01);
// 1 ms smoothing — just enough to de-click the block-rate gate step
// (256-sample blocks = ~5.3ms, so sub-sample jitter at 1ms tau is inaudible)
// while leaving attack_ms=0 feeling truly snappy. Default si.smoo (~30ms)
// rounded off short attacks audibly; 2ms was close but not punchy.
//
// LIMITATION: Python writes block-end scalar gates (synth_engine.py
// ~line 2226), so this 1 ms one-pole IS the actual attack shape Faust
// hears. Any UI attack < ~5 ms (one block) gets rounded to this floor.
// Fix path: refactor to write per-voice attack/decay/release coefficients
// from Python rather than scalar gates — bigger change, deferred.
voice_gate(i)      = hslider("gate_v%i",      0, 0, 1, 0.001) : si.smooth(ba.tau2pole(0.001));
voice_gate_osc1(i) = hslider("gate_osc1_v%i", 0, 0, 1, 0.001) : si.smooth(ba.tau2pole(0.001));
voice_gate_osc2(i) = hslider("gate_osc2_v%i", 0, 0, 1, 0.001) : si.smooth(ba.tau2pole(0.001));

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

// ═══════════════════════════════════════════════════════════════════════
// Polyphonic LFOs — per-voice phase, shared rate/depth/shape/target.
// AMP target only on this Faust path (per-voice gate scaling). Pan/Filter
// continue to apply globally via the Python LFO mod path. Python writes
// `lfoN_active` = 1 when poly mode is on for that LFO so we know whether
// to engage per-voice mod here OR let Python handle it as a global mod.
//
// Per-voice phase offset (`lfoN_phase_v%i`) is written by Python on note_on
// when poly mode is on — random in [0, 1) — so each voice's LFO sits at
// a different point in the cycle. That's the whole sonic point of poly:
// mod across notes is decorrelated, not lockstep.
// ═══════════════════════════════════════════════════════════════════════
// Skipping si.smoo on rate/depth — Python writes block-end values every
// block (~5ms) and any audible step is masked by the per-voice phasor's
// continuous flow + the gate's own si.smooth on note transitions. Saves
// ~12 ops/sample of redundant smoothing.
lfo1_active = hslider("lfo1_active",  0,    0,    1,     1);
lfo1_rate   = hslider("lfo1_rate",    1.0,  0.05, 20.0,  0.001);
lfo1_depth  = hslider("lfo1_depth",   0.0,  0.0,  1.0,   0.001);
lfo1_shape  = hslider("lfo1_shape",   0,    0,    6,     1);
lfo2_active = hslider("lfo2_active",  0,    0,    1,     1);
lfo2_rate   = hslider("lfo2_rate",    1.0,  0.05, 20.0,  0.001);
lfo2_depth  = hslider("lfo2_depth",   0.0,  0.0,  1.0,   0.001);
lfo2_shape  = hslider("lfo2_shape",   0,    0,    6,     1);
voice_lfo1_phase_off(i) = hslider("lfo1_phase_v%i", 0, 0, 1, 0.001);
voice_lfo2_phase_off(i) = hslider("lfo2_phase_v%i", 0, 0, 1, 0.001);

// LFO shape eval — output bipolar [-1, +1] (matches Python lfoShapeEval).
// Shapes: 0=sine, 1=triangle, 2=square, 3=saw, 4=ramp, 5=peak, 6=sh.
// S&H is approximated deterministically (matches Python's static-preview
// formula); good enough on a per-voice mod where strict randomness is
// unimportant.
lfo_shape_eval(shape, ph01) =
    select7(int(shape),
        sin(2.0 * ma.PI * ph01),                                  // 0 sine
        4.0 * abs(ph01 - 0.5) - 1.0,                              // 1 triangle
        select2(ph01 < 0.5, -1.0, 1.0),                           // 2 square
        2.0 * ph01 - 1.0,                                         // 3 saw ↗
        1.0 - 2.0 * ph01,                                         // 4 ramp ↘
        select2(ph01 < 0.2,                                       // 5 peak
            (1.0 - (ph01 - 0.2) / 0.8) * 2.0 - 1.0,
            (ph01 / 0.2) * 2.0 - 1.0),
        sin(ph01 * 37.9) * cos(ph01 * 23.3))                      // 6 s&h
with {
    select7(i, a, b, c, d, e, f, g) =
        select2(i < 1, select2(i < 2, select2(i < 3, select2(i < 4,
            select2(i < 5, select2(i < 6, g, f), e), d), c), b), a);
};

// Per-voice running phase, gated by `active` so when poly is off the
// phasor ticks at zero rate (frozen, no CPU lost on fixed-cost evals).
voice_lfo1_phase(i) = (+(lfo1_rate * lfo1_active / SR) : ma.frac) ~ _
                    : +(voice_lfo1_phase_off(i)) : ma.frac;
voice_lfo2_phase(i) = (+(lfo2_rate * lfo2_active / SR) : ma.frac) ~ _
                    : +(voice_lfo2_phase_off(i)) : ma.frac;

// Tremolo-style amp gate (matches Python _amp_gate post-makeup-removal):
// peak naturally at 1.0, trough at 1-d. Inherently safe against limiter
// overshoot. `active` gates the depth so an "off" LFO contributes unity.
voice_lfo_amp_gate(lfo_val, depth, active) =
    1.0 - eff_d + eff_d * (1.0 + lfo_val) * 0.5
with {
    eff_d = depth * active;
};

voice_amp_mod(i) =
    voice_lfo_amp_gate(lfo_shape_eval(lfo1_shape, voice_lfo1_phase(i)),
                       lfo1_depth, lfo1_active) *
    voice_lfo_amp_gate(lfo_shape_eval(lfo2_shape, voice_lfo2_phase(i)),
                       lfo2_depth, lfo2_active);

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
// PolyBLEP correction at phasor wraps. dt = freq/SR (per-sample phase
// increment as fraction of cycle). Adds a 2-sample polynomial smoothing
// to the discontinuity so the saw/square loses its above-Nyquist content.
// Cheap (~6 ops per sample) and dramatically cleaner than naive math.
poly_blep(t, dt) = lo + hi
with {
    tt_lo = t / max(dt, 1e-9);
    lo = ba.if(t < dt, 2.0 * tt_lo - tt_lo * tt_lo - 1.0, 0.0);
    tt_hi = (t - 1.0) / max(dt, 1e-9);
    hi = ba.if(t > (1.0 - dt), tt_hi * tt_hi + 2.0 * tt_hi + 1.0, 0.0);
};

wave_gen(wf, ph01, dt) = select5(int(wf), w_sine, w_square, w_saw, w_tri, w_sat)
with {
    theta     = TWO_PI * ph01;         // 0..2π for math-based waves
    w_sine    = sin(theta);
    // Square has rising AND falling discontinuities — polyBLEP at both.
    w_square  = ma.signum(w_sine) + poly_blep(ph01, dt) - poly_blep(ma.frac(ph01 + 0.5), dt);
    // Saw discontinuity at the wrap — single polyBLEP.
    w_saw     = (2.0 * ph01 - 1.0) - poly_blep(ph01, dt);
    // Triangle is naturally band-limited (continuous waveform, only the
    // derivative is discontinuous); naive math is fine.
    w_tri     = 2.0 * abs(2.0 * ph01 - 1.0) - 1.0;
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
    wave            = wave_gen(wf, phased, freq_per_sample) * blend;
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
// One full voice — produces 4 channels: osc1_L, osc1_R, osc2_L, osc2_R.
// Each osc is gated by its own per-OSC envelope so OSC1 and OSC2 can have
// independent ADSR shapes within the same voice.
// ═══════════════════════════════════════════════════════════════════════
one_voice(i) =
    (one_osc_phased(i, osc1_wf, osc1_oct, osc1_blend, osc1_pan, voice_osc1_phase_off(i)) : scale_osc1),
    (one_osc_phased(i, osc2_wf, osc2_oct, osc2_blend, osc2_pan, voice_osc2_phase_off(i)) : scale_osc2)
with {
    // Per-voice LFO amp mod multiplies the gate. When neither LFO is in
    // poly+amp mode, voice_amp_mod = 1.0 (unity, no effect) since both
    // active flags are 0 → eff_depth = 0 → gate = 1.0 always.
    amp_mod = voice_amp_mod(i);
    g_osc1 = voice_gate_osc1(i) * UNI_SCALE * amp_mod;
    g_osc2 = voice_gate_osc2(i) * UNI_SCALE * amp_mod;
    scale_osc1 = *(g_osc1), *(g_osc1);
    scale_osc2 = *(g_osc2), *(g_osc2);
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
drone_osc(freq) = wave_gen(osc1_wf, phasor01, freq / SR)
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
