declare name "stave_reverb";
declare description "Cathedral FDN reverb — faithful Faust port of FeedbackDelayReverb";

import("stdfaust.lib");

// ═══════════════════════════════════════════════════════════════════════
// Parameters
// ═══════════════════════════════════════════════════════════════════════
// Exposed as hslider zones; Python writes them each block (no UI on Pi).

predelay_ms  = hslider("predelay_ms",   25.0,    0.0,   150.0,    0.1) : si.smoo;
feedback     = hslider("feedback",       0.90,   0.0,     0.9995, 0.0001) : si.smoo;
damp         = hslider("damp",           0.50,   0.0,     0.99,   0.001) : si.smoo;
low_cut_hz   = hslider("low_cut_hz",    80.0,   20.0,  2000.0,    1.0)  : si.smoo;
high_cut_hz  = hslider("high_cut_hz", 7000.0,  500.0, 20000.0,    1.0)  : si.smoo;
freeze_in    = hslider("freeze_input",   1.0,   0.0,     1.0,   0.001) : si.smoo;
er_scale     = hslider("er_scale",       0.4,   0.0,     1.0,   0.001);

SR_K        = ma.SR / 1000.0;   // samples per ms
MAX_PREDEL  = 8192;             // >150 ms at 48k
MAX_ER      = 4096;             // >80 ms at 48k
MAX_AP      = 4096;             // >80 ms at 48k
MAX_FDN     = 16384;            // >300 ms at 48k + modulation headroom

// ═══════════════════════════════════════════════════════════════════════
// Pre-delay (mono sum path)
// ═══════════════════════════════════════════════════════════════════════
predelay = de.fdelay(MAX_PREDEL, predelay_ms * SR_K);

// ═══════════════════════════════════════════════════════════════════════
// Early reflections — 4 stereo-separated taps each
// ═══════════════════════════════════════════════════════════════════════
er_tap(ms, g) = de.delay(MAX_ER, int(ms * SR_K)) * g;

er_l = _ <: er_tap(11.3, 0.72), er_tap(23.7, 0.55),
             er_tap(37.1, 0.38), er_tap(53.9, 0.22) :> _;
er_r = _ <: er_tap(13.9, 0.68), er_tap(29.3, 0.48),
             er_tap(43.7, 0.30), er_tap(61.3, 0.18) :> _;

// ═══════════════════════════════════════════════════════════════════════
// Diffusion — 8-stage Schroeder allpass chain
// ═══════════════════════════════════════════════════════════════════════
diffusion =
      fi.allpass_comb(MAX_AP, int(11.7 * SR_K), 0.50)
    : fi.allpass_comb(MAX_AP, int(19.3 * SR_K), 0.55)
    : fi.allpass_comb(MAX_AP, int(27.1 * SR_K), 0.45)
    : fi.allpass_comb(MAX_AP, int(33.7 * SR_K), 0.50)
    : fi.allpass_comb(MAX_AP, int(41.3 * SR_K), 0.45)
    : fi.allpass_comb(MAX_AP, int(51.9 * SR_K), 0.55)
    : fi.allpass_comb(MAX_AP, int(63.7 * SR_K), 0.50)
    : fi.allpass_comb(MAX_AP, int(79.3 * SR_K), 0.45);

// ═══════════════════════════════════════════════════════════════════════
// 8-line FDN with per-line LFO modulation and Hadamard cross-feedback
// ═══════════════════════════════════════════════════════════════════════
NLINES = 8;

// Delay times (ms), LFO rates (Hz), modulation depths (samples)
line_base_ms(i)  = ba.take(i+1, (63.7, 79.3, 95.3, 111.7, 131.9, 153.1, 177.7, 200.9));
line_mod_rate(i) = ba.take(i+1, (0.23, 0.37, 0.47, 0.61, 0.73, 0.89, 0.31, 0.53));
line_mod_depth(i)= ba.take(i+1, (28.0, 32.0, 24.0, 36.0, 26.0, 30.0, 34.0, 25.0));

// Per-line modulated fractional delay (Lagrange-3 interp via de.fdelayltv)
line_delay(i) = de.fdelayltv(3, MAX_FDN, delay_samp)
  with {
    base_samp  = line_base_ms(i) * SR_K;
    lfo        = os.osc(line_mod_rate(i));
    delay_samp = base_samp + line_mod_depth(i) * lfo;
  };

// Per-line feedback filter: one-pole damping LP → Butterworth hi/lo cut → fb gain
// Python form: y[n] = (1-damp)*x[n] + damp*y[n-1]  ⇔  ((1-damp)*x) : fi.pole(damp)
damp_lp = _ * (1.0 - damp) : + ~ *(damp);

line_fb =
      damp_lp
    : fi.highpass(2, low_cut_hz)
    : fi.lowpass(2,  high_cut_hz)
    * feedback;

// FDN recursion: (si.bus(2N) :> bus(N) : delays) ~ (filters : hadamard : /sqrt(N))
// External N inputs on first half, feedback N on second half.
// ro.hadamard is an un-normalized butterfly (gain = sqrt(N) per pass) — the
// Python version used a 1/sqrt(N)-scaled matrix. We normalize here so the
// loop gain matches and the tail doesn't diverge.
fdn_scale = 1.0 / sqrt(NLINES);
fdn_core =
    (si.bus(2 * NLINES) :> si.bus(NLINES) : par(i, NLINES, line_delay(i)))
    ~ (par(i, NLINES, line_fb) : ro.hadamard(NLINES) : par(i, NLINES, *(fdn_scale)));

// ═══════════════════════════════════════════════════════════════════════
// Stereo output tap: even lines (0,2,4,6) → L, odd (1,3,5,7) → R
// Matches the Python `taps_matrix[0::2].sum * inv_sqrt_half` output.
// ═══════════════════════════════════════════════════════════════════════
wet_stereo_tap(a, b, c, d, e, f, g, h) =
      ma.tanh((a + c + e + g) * 0.5),
      ma.tanh((b + d + f + h) * 0.5);

// ═══════════════════════════════════════════════════════════════════════
// FDN wet path — takes stereo (L,R), returns stereo wet (not including ER).
// Internally runs pre-delay, diffusion, and the 8-line FDN exactly once.
// ═══════════════════════════════════════════════════════════════════════
fdn_wet(in_l, in_r) = fdn_inputs : fdn_core : wet_stereo_tap
with {
    mono     = (in_l + in_r) * 0.5;
    pdelayed = mono : predelay;
    diffused = pdelayed : diffusion;
    in_l_mix = (diffused * 0.5 + in_l * 0.5) * freeze_in;
    in_r_mix = (diffused * 0.5 + in_r * 0.5) * freeze_in;
    fdn_inputs = in_l_mix, in_r_mix, in_l_mix, in_r_mix,
                 in_l_mix, in_r_mix, in_l_mix, in_r_mix;
};

// ═══════════════════════════════════════════════════════════════════════
// ER wet path — takes stereo (L,R), returns stereo ER.
// Operates on mono sum after pre-delay.
// ═══════════════════════════════════════════════════════════════════════
er_wet(in_l, in_r) = (pdelayed : er_l) * er_scale,
                      (pdelayed : er_r) * er_scale
with {
    mono     = (in_l + in_r) * 0.5;
    pdelayed = mono : predelay;
};

// ═══════════════════════════════════════════════════════════════════════
// Full wet path: stereo (L,R) → stereo wet (ER + FDN tail).
// Dry/wet mix is handled in Python (same as current behaviour).
// ═══════════════════════════════════════════════════════════════════════
stave_reverb = _,_ <: fdn_wet, er_wet :> _,_;

process = stave_reverb;
