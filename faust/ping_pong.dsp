declare name "stave_ping_pong";
declare description "Stereo cross-feedback ping-pong delay — Faust port of SynthEngine._process_ping_pong";

import("stdfaust.lib");

// ═══════════════════════════════════════════════════════════════════════
// Parameters
// ═══════════════════════════════════════════════════════════════════════
// Python sets delay times as sample counts (already converted from ms or
// BPM-sync). Keeps the ms↔samples math in Python where BPM/subdivision
// logic already lives.

delay_l_samps = hslider("delay_l_samps", 18000, 1, 65535, 1);
delay_r_samps = hslider("delay_r_samps", 18000, 1, 65535, 1);
fb            = hslider("feedback", 0.35, 0.0, 0.85, 0.001) : si.smoo;
wet           = hslider("wet", 0.0, 0.0, 1.0, 0.001) : si.smoo;

MAX_DELAY = 65536;  // >1s at 48k, matches Python's 1s headroom

// ═══════════════════════════════════════════════════════════════════════
// Cross-coupled recursive core
// ═══════════════════════════════════════════════════════════════════════
// Recursive equations (matching Python's _process_ping_pong):
//   buf_l[t] = in_l[t] + fb * buf_r[t - delay_l]
//   buf_r[t] = in_r[t] + fb * buf_l[t - delay_r]
//   tap_l    = buf_r[t - delay_l]   ← wet L output
//   tap_r    = buf_l[t - delay_r]   ← wet R output
//
// Faust form using A ~ B where A is 4→2 (takes feedback taps + external
// inputs, produces new taps) and B is 2→2 identity passthrough. The
// taps emerge as the external outputs of the `~` construct.
// Note: Faust's `~` introduces a 1-sample delay in the feedback loop,
// so the effective loop length is (delay_l + delay_r + 1) samples —
// audibly identical to Python's (delay_l + delay_r) for all sane
// delay times (1 sample @ 48k = 20 µs).

forward(tap_l_fb, tap_r_fb, in_l, in_r) =
      de.delay(MAX_DELAY, delay_l_samps, in_r + fb * tap_r_fb),
      de.delay(MAX_DELAY, delay_r_samps, in_l + fb * tap_l_fb);

pp_taps = forward ~ (_, _);

// ═══════════════════════════════════════════════════════════════════════
// Dry + wet output
// ═══════════════════════════════════════════════════════════════════════
// Python: out_l += tap_l * wet  (in place, dry passes through unchanged)
// Here: (in_l + wet·tap_l, in_r + wet·tap_r) — same result, returned as new stream.
// Pipeline form (Faust can't destructure multi-output signals into named vars):
//   split inputs <: (wet-scaled taps, dry passthrough) :> sum pairwise

wet_scale = *(wet), *(wet);

ping_pong = _,_ <: (pp_taps : wet_scale), (_, _) :> _, _;

process = ping_pong;
