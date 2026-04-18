declare name "stave_plate";
declare description "PLATE reverb — Dattorro topology. Denser, brighter, metallic-leaning tail vs the FDN.";

import("stdfaust.lib");

// ═══════════════════════════════════════════════════════════════════════
// Parameter zones — match reverb.dsp names where semantically equivalent
// so Python can push the same dicts into either module. Plate-specific
// Dattorro knobs (bandwidth, diffusion stages) are hardcoded to tuned
// values; exposing them would bloat the UI without a worship benefit.
// ═══════════════════════════════════════════════════════════════════════
predelay_ms  = hslider("predelay_ms",    5.0,   0.0, 150.0, 0.1) : si.smoo;
decay        = hslider("feedback",       0.65,  0.0, 0.97,  0.0001) : si.smoo;
damp         = hslider("damp",           0.30,  0.0, 0.99,  0.001) : si.smoo;
low_cut_hz   = hslider("low_cut_hz",   150.0,  20.0, 2000.0, 1.0) : si.smoo;
high_cut_hz  = hslider("high_cut_hz", 11000.0, 500.0, 20000.0, 1.0) : si.smoo;
// Freeze capture gate — kept for API parity with the FDN reverb.
freeze_in    = hslider("freeze_input",   1.0,  0.0, 1.0,   0.001) : si.smoo;
// ER scaling unused in plate but accept for param-push parity.
er_scale     = hslider("er_scale",       0.4,  0.0, 1.0,   0.001);
// Accept the shimmer/noise zones silently — Python writes them to whichever
// .so is active. Plate ignores them (its topology isn't set up for shimmer
// feedback), but having the zones prevents "zone not found" warnings.
shimmer_fb   = hslider("shimmer_fb", 0.0, 0.0, 1.0, 0.001);
noise_mod    = hslider("noise_mod",  0.0, 0.0, 1.0, 0.001);

SR_K       = ma.SR / 1000.0;
MAX_PREDEL = 8192;

// ═══════════════════════════════════════════════════════════════════════
// Pre-delay (stereo input, single delay applied to mono sum)
// ═══════════════════════════════════════════════════════════════════════
pdelay = de.fdelay(MAX_PREDEL, predelay_ms * SR_K);

// ═══════════════════════════════════════════════════════════════════════
// Input hi/lo cut — matches FDN reverb's tone shaping so type-switching
// keeps the spectrum familiar. Post-Dattorro would also work; pre-shape
// is cheaper.
// ═══════════════════════════════════════════════════════════════════════
tone_shape = fi.highpass(2, low_cut_hz) : fi.lowpass(2, high_cut_hz);

// ═══════════════════════════════════════════════════════════════════════
// Dattorro plate reverb from Faust stdlib (re.dattorro_rev).
// Signature: dattorro_rev(pre_delay_samp, bw, i_diff1, i_diff2, decay,
//                         d_diff1, d_diff2, damping)
//
// Pre-delay is already applied above, so pass 0 there.
// bw=0.9995 (classic bright bandwidth), i_diff1/2 and d_diff1/2 tuned
// from Dattorro's original paper values (Jon Dattorro, 1997).
// Our `decay` knob drives the decay param (0..0.97, safe ceiling); our
// `damp` knob drives the damping coefficient directly (higher = more
// high-freq loss in the tank recirculation).
// ═══════════════════════════════════════════════════════════════════════
plate_core = re.dattorro_rev(0, 0.9995, 0.75, 0.625, decay, 0.7, 0.5, damp);

// ═══════════════════════════════════════════════════════════════════════
// Signal flow: stereo in → tone shape per ch → pre-delay per ch → freeze
// gate → Dattorro → stereo out.
//
// Freeze gate: multiplying the input by freeze_in (0..1) at the Dattorro's
// doorstep seals the tank when Python drops it to 0 for freeze capture.
// ═══════════════════════════════════════════════════════════════════════
input_stage(l, r) = (l : tone_shape : pdelay) * freeze_in,
                    (r : tone_shape : pdelay) * freeze_in;

process = input_stage : plate_core;
