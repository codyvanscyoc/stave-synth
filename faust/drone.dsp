declare name "stave_drone";
declare description "DRONE reverb — parallel tuned resonators. 4 high-Q bandpasses inside delay feedback loops, tuned to a major-chord shape, produce a singing pitched drone that sustains with or without input. No modulation, no cross-feedback, no washiness.";

import("stdfaust.lib");

// ═══════════════════════════════════════════════════════════════════════
// Parameters — zone names parallel the FDN reverb so Python can push the
// same dict into either backend. drone_key is the one drone-specific zone.
// ═══════════════════════════════════════════════════════════════════════
predelay_ms  = hslider("predelay_ms",  15.0,   0.0, 150.0,    0.1) : si.smoo;
// `feedback` — direct 0..1 value; set_decay converts decay_seconds into this.
// For comb/resonator topology the loop gain needs to be much closer to unity
// than an FDN to get multi-second sustain (combs cycle many times per second).
feedback     = hslider("feedback",     0.98,   0.0,   0.9995, 0.0001) : si.smoo;
damp         = hslider("damp",         0.30,   0.0,   0.99,   0.001) : si.smoo;
low_cut_hz   = hslider("low_cut_hz",   120.0, 20.0, 2000.0,   1.0)   : si.smoo;
high_cut_hz  = hslider("high_cut_hz",  4000.0, 500.0, 20000.0, 1.0)  : si.smoo;
freeze_in    = hslider("freeze_input",  1.0,   0.0,   1.0,    0.001) : si.smoo;
er_scale     = hslider("er_scale",      0.1,   0.0,   1.0,    0.001);

// Key offset — shifts the chord by ±12 semitones from A major (A/C#/E/A).
// Integer step so user gets discrete key choices.
drone_key    = hslider("drone_key",     0,   -12,    12,      1);

// Accept these to match Python's param-push dict (unused in drone topology).
shimmer_fb = hslider("shimmer_fb", 0.0, 0.0, 1.0, 0.001);
noise_mod  = hslider("noise_mod",  0.0, 0.0, 1.0, 0.001);

SR_K    = ma.SR / 1000.0;
MAX_DEL = 16384;

// ═══════════════════════════════════════════════════════════════════════
// Chord-tone frequencies. Root = 110 Hz (A2) transposed by drone_key —
// pedal-bass territory, not mid-range. Mid-range resonant peaks (220 Hz+)
// sound whistley with high-Q filters; dropping an octave puts the drone
// under the playing content rather than fighting with it.
// Ratios: 1, 5/4 (≈maj 3rd), 3/2 (≈perf 5th), 2 (octave).
// ═══════════════════════════════════════════════════════════════════════
root_freq   = 110.0 * pow(2.0, drone_key / 12.0);
tone_ratio(i) = ba.take(i+1, (1.0, pow(2.0, 4.0/12.0), pow(2.0, 7.0/12.0), 2.0));
tone_freq(i) = root_freq * tone_ratio(i);

// ═══════════════════════════════════════════════════════════════════════
// Tuned resonator — input + (delayed+bandpassed+scaled feedback).
// Delay time = one period at `freq`, so each bounce phase-locks to that
// pitch, reinforcing it. The inline high-Q bandpass rejects everything
// off-pitch, giving the characteristic "singing" tone.
//
// Stability: at resonance, bandpass has unity gain, so loop gain = feedback.
// With feedback=0.998 the pole sits on a razor's edge of ~6 s decay on an
// impulse, but can't blow up because the bandpass's off-resonance gain
// (<<1) attenuates any runaway harmonics.
// ═══════════════════════════════════════════════════════════════════════
// fi.resonbp peak gain at resonance is Q × gain (virtual-analog prototype
// b1·s / (s² + s/Q + 1)). Passing gain=1/Q normalises the peak to unity so
// the loop gain equals `feedback` cleanly — without this, Q=20 implies 20×
// boost per pass and the loop blows up to NaN inside a few hundred samples.
// Extra 0.9995 safety factor inside the loop: fi.resonbp's bilinear-
// transformed peak at Q=14 can measure fractionally above unity due to
// frequency warping, so "feedback = 0.9995" at the slider max could
// still leave effective loop gain ≥ 1.0 on certain sample-rate/freq
// combinations — silent-runaway NaN risk during long FREEZE. The extra
// 0.9995 guarantees strict sub-unity regardless of setting; the decay-
// time cost is undetectable (0.05 % per loop iteration).
resonator(freq, Q) = + ~ loop
with {
    period_samples = ma.SR / max(freq, 20.0);
    bp             = fi.resonbp(freq, Q, 1.0 / Q);
    loop           = de.fdelay(MAX_DEL, period_samples) : bp : *(feedback * 0.9995);
};

// Q=14 — narrow enough to produce pitched tail, broad enough to avoid the
// whistley/tinnitus character that Q>18 produces in the bass register.
RES_Q = 14.0;

// ═══════════════════════════════════════════════════════════════════════
// Input pipeline — stereo → mono sum → predelay → tone shape → freeze gate
// ═══════════════════════════════════════════════════════════════════════
pdelay     = de.fdelay(MAX_DEL, predelay_ms * SR_K);
tone_shape = fi.highpass(2, low_cut_hz) : fi.lowpass(2, high_cut_hz);
mono_in(L, R) = (L + R) * 0.5;

// Drive gain before the resonator bank. Higher = more immediate presence on
// note-onset (the "girth" before the drone builds); lower = cleaner sustain
// without early tanh saturation. 0.5 feels right — the initial hit is audible
// as the resonators are already ~half-full, and a few seconds of hold still
// fills out the full resonance without heavy clipping.
DRONE_INPUT_GAIN = 0.5;

pre_drone(L, R) = (mono_in(L, R) * DRONE_INPUT_GAIN : pdelay : tone_shape) * freeze_in;

// ═══════════════════════════════════════════════════════════════════════
// 4 resonators in parallel → stereo pair. Alternate tones to the L and R
// channels so the chord spreads across the stereo field naturally rather
// than all chord tones from both sides equally.
//   L = (root + fifth) * 0.5
//   R = (third + octave) * 0.5
// ═══════════════════════════════════════════════════════════════════════
drone_bank(mono) = mono <: resonator(tone_freq(0), RES_Q),
                            resonator(tone_freq(1), RES_Q),
                            resonator(tone_freq(2), RES_Q),
                            resonator(tone_freq(3), RES_Q),
                            broad_resonator
                         : stereo_tap;

// Per-resonator equal-power pan positions. Tuned so each chord tone has
// audible stereo placement (~11 dB L/R ratio when only one tone rings) but
// the gain sums across all 4 resonators are equal → the full chord sits
// balanced in the stereo field.
//   root  p=-0.65 (far L)     fifth p=+0.25 (moderate R)
//   third p=+0.65 (far R)     oct   p=-0.25 (moderate L)
pan_pos(i) = ba.take(i+1, (-0.65, 0.65, 0.25, -0.25));
pan_gL(i)  = cos((pan_pos(i) + 1.0) * 0.25 * ma.PI);
pan_gR(i)  = sin((pan_pos(i) + 1.0) * 0.25 * ma.PI);

// ═══════════════════════════════════════════════════════════════════════
// Broadband "catch" resonator — independent of the chord tones. Low-Q
// bandpass at 160 Hz gives ~80–320 Hz coverage, so out-of-zone notes still
// produce a subtle drone response instead of silence. Fixed short feedback
// (~0.8 s sustain at 160 Hz) so it doesn't smear long tails. Scaled down
// and centered in stereo — sits UNDER the chord tones, never competes.
// ═══════════════════════════════════════════════════════════════════════
BROAD_FREQ   = 160.0;
BROAD_Q      = 2.5;
BROAD_FB     = 0.96;
BROAD_LEVEL  = 0.15;

broad_resonator = + ~ loop
with {
    period_samples = ma.SR / BROAD_FREQ;
    bp             = fi.resonbp(BROAD_FREQ, BROAD_Q, 1.0 / BROAD_Q);
    // Same sub-unity safety as the tuned resonators (see comment above).
    loop           = de.fdelay(MAX_DEL, period_samples) : bp : *(BROAD_FB * 0.9995);
};

// Stereo tap: 4 panned chord tones + 1 centered broadband. tanh soft-limit
// bounds peaks so accumulated chord energy can't slam the master limiter.
stereo_tap(r0, r1, r2, r3, rb) =
    ma.tanh((r0*pan_gL(0) + r1*pan_gL(1) + r2*pan_gL(2) + r3*pan_gL(3) + rb*BROAD_LEVEL) * 0.5),
    ma.tanh((r0*pan_gR(0) + r1*pan_gR(1) + r2*pan_gR(2) + r3*pan_gR(3) + rb*BROAD_LEVEL) * 0.5);

process = pre_drone : drone_bank;
