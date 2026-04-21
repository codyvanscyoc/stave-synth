declare name "stave_piano_room";
declare description "Small realistic piano room — short, damped Dattorro with asymmetric L/R pre-delay";

import("stdfaust.lib");

// ═══════════════════════════════════════════════════════════════════════
// Dedicated piano-layer reverb. Replaces FluidSynth's 1990s Schroeder
// reverb which was the last legacy DSP in the audio chain.
//
// Topology: tone-shape → asymmetric pre-delay (L≠R, gives stereo room
// character) → Dattorro tank tuned SHORT, DAMPED, and slightly less bright
// than plate.dsp (bandwidth 0.92 vs plate's 0.9995). Output is 100% wet;
// Python handles the dry/wet blend + enable gate.
//
// `size`  → controls decay length (0 ≈ 0.55s RT60 feel, 1 ≈ 0.82s)
// `damp`  → HF absorption in the tank recirculation (wood-room feel)
// ═══════════════════════════════════════════════════════════════════════

size = hslider("size", 0.5,  0.0, 1.0,  0.001) : si.smoo;
damp = hslider("damp", 0.6,  0.0, 0.99, 0.001) : si.smoo;

// Decay is a signal derived from size. Kept as a named binding so the
// dattorro call reads clean.
decay = 0.55 + size * 0.27;

// Warm input shaping: kill sub rumble, gentle top roll so the tank
// doesn't resonate on piano's hammer noise.
tone = fi.highpass(2, 180.0) : fi.lowpass(2, 9000.0);

// Dattorro tuned for small room:
//   bandwidth 0.92 (less bright than plate.dsp's 0.9995)
//   decay = 0.55..0.82 as size sweeps 0→1
//   damp is direct user control
//
// NOTE: no external pre-delay. The Dattorro's internal diffusers already
// decorrelate L/R; adding 7/11ms asymmetric pre-delay on top was audible
// as a discrete slap echo at low wet mixes (because the dry is ~93% but
// the wet's first reflections arrived noticeably late). Cleaner to let
// the tank start "immediately" — real small rooms have ~1-3ms ER which
// is imperceptible as timing anyway.
room_core = re.dattorro_rev(0, 0.92, 0.75, 0.625, decay, 0.7, 0.5, damp);

// 2-in → 2-out plumbing mirrors plate.dsp's pattern.
input_stage(l, r) = (l : tone), (r : tone);

process = input_stage : room_core;
