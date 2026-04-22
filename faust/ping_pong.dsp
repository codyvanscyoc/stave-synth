declare name "stave_ping_pong";
declare description "Stereo cross-feedback ping-pong delay with feedback filter, drive, and BBD-style mod";

import("stdfaust.lib");

// ═══════════════════════════════════════════════════════════════════════
// Parameters
// ═══════════════════════════════════════════════════════════════════════
// Python sets delay times as sample counts (already converted from ms or
// BPM-sync). Keeps the ms↔samples math in Python where BPM/subdivision
// logic already lives.

// 50ms smoothing on the delay-line read offsets so user drags don't
// produce sample-jump scratches; renders as a small chorus-y glide
// during the change which is the musical / right behavior here.
delay_l_samps = hslider("delay_l_samps", 18000, 1, 65535, 1) : si.smooth(ba.tau2pole(0.05));
delay_r_samps = hslider("delay_r_samps", 18000, 1, 65535, 1) : si.smooth(ba.tau2pole(0.05));
fb            = hslider("feedback", 0.35, 0.0, 1.0, 0.001) : si.smoo;
wet           = hslider("wet", 0.0, 0.0, 1.0, 0.001) : si.smoo;
// Polarity: +1 normal, -1 inverts feedback signal (comb-filter delay sound).
polarity      = hslider("polarity", 1.0, -1.0, 1.0, 1.0);

// Feedback-path tone shaping (Butterworth hi/lo cut)
low_cut_hz    = hslider("low_cut_hz",  20.0, 20.0, 1000.0, 1.0) : si.smoo;
high_cut_hz   = hslider("high_cut_hz", 18000.0, 500.0, 20000.0, 1.0) : si.smoo;

// Drive in the feedback loop — tanh soft-clip; each repeat picks up grit.
drive         = hslider("drive", 0.0, 0.0, 1.0, 0.001) : si.smoo;

// Cross-feedback width: 1 = full ping-pong (L↔R), 0 = mono echo per side
// (L→L, R→R), in between = partial cross-bleed.
width         = hslider("width", 1.0, 0.0, 1.0, 0.001) : si.smoo;

// BBD-style modulation of the delay-line read position. L gets sin, R gets
// cos so the stereo image moves as well as the pitch.
mod_rate_hz   = hslider("mod_rate_hz", 0.5, 0.05, 8.0, 0.001) : si.smoo;
mod_depth_ms  = hslider("mod_depth_ms", 0.0, 0.0, 15.0, 0.01) : si.smoo;

// Reverse playback: capture input, play it back in reverse over a window.
// reverse_amount mixes the reversed wet into the output (independent of fb path).
// reverse_window_ms = chunk size (50..15000ms — long values are AURORA "rise").
// Two overlapping reverse pointers with sin² window envelopes give clickless crossfade.
reverse_amount    = hslider("reverse_amount", 0.0, 0.0, 1.0, 0.001) : si.smoo;
reverse_window_ms = hslider("reverse_window_ms", 500.0, 50.0, 15000.0, 1.0);
// Reverse feedback: recirculate reverse output back into the buffer for
// self-evolving texture. Cap at 0.7 — 1-sample-delay loop has natural windowed
// attenuation but stays comfortable below unity.
reverse_feedback  = hslider("reverse_feedback", 0.0, 0.0, 0.7, 0.001) : si.smoo;

MAX_DELAY = 65536;  // >1.3s at 48k, with headroom for mod read offsets
// Reverse-path max offset = 2 × window_ms × SR_K + 1. At 48 kHz with the
// 15 s window ceiling that's 1,440,001 samples — the old 1.5M only had 4 %
// headroom. At 96 kHz the same max window would need 2.88M, which would
// overflow and click. Bumped to 3,000,000 so the reverse engine is clean
// at any SR up to 96 kHz. Cost: ~12 MB stereo buffer. Trivial on a Pi 5.
REVERSE_MAX = 3000000;

// ═══════════════════════════════════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════════════════════════════════

ms_to_samps(ms) = ms * ma.SR / 1000.0;

// Drive: pre-gain → tanh → divide by same pre-gain. For small signals this
// is identity (linear region of tanh), so loop gain stays at `feedback`
// regardless of drive setting. For large signals tanh compresses, so the
// loop gain DROPS as the signal heats up — kills runaway. Drive only adds
// harmonic content, never net level. Range tamed (1..5) so even at full
// the loop stays well-behaved.
drive_amount = 1.0 + drive * 4.0;
soft_clip(x) = ma.tanh(x * drive_amount) / drive_amount;

// LFO pair (sin + cos, full scale ±1) at mod_rate_hz, used for L/R
// fractional delay-time modulation. Single shared phasor so they stay
// 90° out of phase regardless of rate changes.
mod_phase = os.phasor(1, mod_rate_hz);
lfo_l = sin(mod_phase * 2.0 * ma.PI);
lfo_r = cos(mod_phase * 2.0 * ma.PI);

// Effective fractional delay times in samples.
mod_l_samps = ms_to_samps(mod_depth_ms) * lfo_l;
mod_r_samps = ms_to_samps(mod_depth_ms) * lfo_r;
eff_l = max(1.0, delay_l_samps + mod_l_samps);
eff_r = max(1.0, delay_r_samps + mod_r_samps);

// Feedback path coloring (filter then drive). Keep order Filter→Drive so
// drive harmonics aren't notched by the lowpass — they stay audible.
fb_color(x) = x : fi.highpass(2, low_cut_hz)
                : fi.lowpass(2, high_cut_hz)
                : soft_clip;

// ═══════════════════════════════════════════════════════════════════════
// Reverse playback core
// ═══════════════════════════════════════════════════════════════════════
// Two phasors offset by 0.5 cycle through the window. For unity-speed
// reverse playback, the read offset must advance at 2 samples per real
// sample (so the read head walks backward through the buffer at speed 1
// while real time advances at speed 1; net audio plays in reverse).
// sin²(phase·π) windows on each pointer sum to 1 with the half-cycle
// offset (sin² + cos² = 1) → clickless constant-power crossfade.

rev_window_samps = max(50.0, reverse_window_ms) * ma.SR / 1000.0;
rev_freq_hz = 1000.0 / max(50.0, reverse_window_ms);

rev_phase_1 = os.phasor(1.0, rev_freq_hz);
rev_phase_2 = ma.frac(rev_phase_1 + 0.5);

rev_win_1 = pow(sin(rev_phase_1 * ma.PI), 2.0);
rev_win_2 = pow(sin(rev_phase_2 * ma.PI), 2.0);

// Offset = 2 × phase × W + 1: ranges 1 to 2W+1 over W output samples.
// At phase=0 we read the newest sample, at phase=1 we read 2W ago.
// Each cycle plays W samples of input (covering 2W in time) backwards.
rev_off_1 = rev_phase_1 * rev_window_samps * 2.0 + 1.0;
rev_off_2 = rev_phase_2 * rev_window_samps * 2.0 + 1.0;

reverse_core(in) = de.fdelay(REVERSE_MAX, rev_off_1, in) * rev_win_1
                 + de.fdelay(REVERSE_MAX, rev_off_2, in) * rev_win_2;

// Recursive feedback: reverse output mixes back into delayline input so each
// new chunk reads the previous reverse blended with new audio. The fdelay
// offsets provide the necessary loop delay so Faust accepts the recursion.
reverse_one = rev_step ~ _
with {
    rev_step(prev, in) = reverse_core(in + reverse_feedback * prev);
};

// ═══════════════════════════════════════════════════════════════════════
// Cross-coupled recursive core
// ═══════════════════════════════════════════════════════════════════════
// Same topology as before: each delay line reads its own length and
// receives the OPPOSITE channel's tap multiplied by feedback. Now the
// feedback signal passes through fb_color (filter+drive) before re-entry.
//
// Fractional delay (de.fdelay) is required for clean modulation — integer
// `de.delay` would zipper. Faust's de.fdelay uses Lagrange interpolation.

// Cross-feedback width: w=1 → tap from OPPOSITE side (full ping-pong),
// w=0 → tap from SAME side (mono echo). Equal-power crossfade: at w=0.5
// each side mixes 50/50, so loop gain stays constant across the sweep.
xfeed_l(tap_l_fb, tap_r_fb) = tap_l_fb * (1.0 - width) + tap_r_fb * width;
xfeed_r(tap_l_fb, tap_r_fb) = tap_r_fb * (1.0 - width) + tap_l_fb * width;
xin_l(in_l, in_r) = in_l * (1.0 - width) + in_r * width;
xin_r(in_l, in_r) = in_r * (1.0 - width) + in_l * width;

forward(tap_l_fb, tap_r_fb, in_l, in_r) =
      de.fdelay(MAX_DELAY, eff_l, xin_l(in_l, in_r) + polarity * fb * fb_color(xfeed_l(tap_l_fb, tap_r_fb))),
      de.fdelay(MAX_DELAY, eff_r, xin_r(in_l, in_r) + polarity * fb * fb_color(xfeed_r(tap_l_fb, tap_r_fb)));

pp_taps = forward ~ (_, _);

// ═══════════════════════════════════════════════════════════════════════
// Dry + wet output
// ═══════════════════════════════════════════════════════════════════════

wet_scale = *(wet), *(wet);
// Reverse pre-gain: the windowed reverse path is only audibly distinct
// from dry on transients (attacks/releases). ~3× boost gives it
// dominance over dry on a fading note so the reverse swell reads.
rev_scale = *(reverse_amount * 3.0), *(reverse_amount * 3.0);

reverse_pair(in_l, in_r) = reverse_one(in_l), reverse_one(in_r);

// Three parallel paths summed: dry passthrough, ping-pong wet, reverse wet.
ping_pong = _,_ <: (pp_taps : wet_scale),
                   (reverse_pair : rev_scale),
                   (_, _)
                   :> _, _;

process = ping_pong;
