declare name "stave_piano_chain";
declare description "Phase 3 render-skeleton port: piano post-processing chain — volume gain, 24dB low-cut / high-cut cascades, 4-band parametric EQ (frozen-state per band), comp-sidechain mono tap";

// ═══════════════════════════════════════════════════════════════════════
// Phase 3 scope (details in FAUST_PORT_PLAN.md "Phase 3 implementation
// notes"). Mirrors fluidsynth_player.render_block from the volume stage
// through the 4-band EQ — the region BEFORE the LA-2A compressor.
// FluidSynth sample generation itself stays Python/C upstream.
//
// PORTED here:
//   • Volume gain — the dB-curve gain 10^((vol_cur−1)·2) is computed and
//     smoothed by PYTHON (it owns the ~10 ms one-pole on _volume_cur and
//     the ≤0.001 hard-zero branch) and arrives as one block-constant
//     zone. Multiplying by 0.0 mirrors Python's np.zeros_like exactly:
//     the downstream filters keep processing zeros, state decays.
//   • Low-cut: 2× cascaded RBJ highpass @ lowcut_hz, Q 0.707 (24 dB/oct,
//     "always running" — no bypass at the boundary, same as Python).
//   • High-cut: 2× cascaded RBJ lowpass @ highcut_hz, Q 0.707 (always
//     running; at 20 kHz it is inaudible but state stays fresh).
//   • 4-band parametric EQ (BiquadPeakingEQ mirror). Python SKIPS a
//     disabled band's .process() → its zi freezes; mirrored with the
//     Phase-2 frozen-state biquad (select2-held state registers), so
//     re-enabling a band after any pause is sample-exact. Coefficients
//     recompute per block from zones — Python's set_eq_band can retune a
//     disabled band, and the next enabled block sees the new coefficients
//     either way.
//   • Comp sidechain mono tap: output 3 = (out_L + out_R) · 0.5 post-EQ,
//     exactly the `mono` Python's comp gate computes. The LA-2A itself
//     stays in Python (see below) and consumes this output.
//
// NOT ported (stays in Python — follow-the-code rationale):
//   • The LA-2A compressor — its envelope is BLOCK-rate (one RMS per
//     block, attack/release coefficients derived from block length) and
//     the resulting gain ramp applies to the SAME block the RMS was
//     measured from. A sample-rate Faust envelope would change the
//     sonics; a block-faithful port needs the block boundary, which
//     Faust doesn't see. Python computes the gain ramp from this
//     module's mono output and applies the blend to L/R (two vector
//     multiplies) — see _comp_gain_ramp in fluidsynth_player.py, which
//     also retires the wet/dry divide-by-safe_mono hazard by applying
//     the ramp directly.
//   • Velocity-brightness filter — post-comp, per-note-event
//     parameterized (tracker updates on note_on), per-block work is two
//     scalar ops + one biquad pair; not worth an island boundary.
//   • Tremolo — post-comp, preset-gated (Suitcase only), vectorized
//     numpy sin is already cheap.
//   • Piano-room reverb — already its own Faust island (piano_room.dsp).
//   • The silence skip (_active_notes / _silent_blocks) and the
//     fs-None/disabled early-outs: those blocks never call compute(), so
//     this module's state freezes exactly like Python's untouched
//     filter zi.
//
// I/O:
//   inputs  : piano_L, piano_R  (int16-converted float, PRE-volume)
//   outputs : out_L, out_R (post-EQ), mono ((L+R)·0.5 — comp sidechain)
//
// Built with -double + -DFAUSTFLOAT=double (see faust/build.sh): the EQ
// biquads at low center frequencies (150-300 Hz bells, 40 Hz low cut)
// have poles near unity and cannot hold the ~1e-6 parity bar in float32;
// double zones/IO also let the wrapper pass float64 blocks zero-copy.
//
// Smoothing policy: NO si.smoo anywhere. piano_gain is Python-smoothed
// per block; cutoffs/EQ params jump instantly in the Python path too
// (set_params without zi reset — coefficient swap with state carried).
// ═══════════════════════════════════════════════════════════════════════

import("stdfaust.lib");

// ── Zones — written once per block by fluidsynth_player. Values arrive
// already clamped by the Python setters (set_lowcut 20..2000, set_highcut
// 200..20000, set_eq_band 20..20000 / ±18 dB / q 0.1..10); the biquad
// clamps below mirror the Biquad* classes' own set_params clamps.
piano_gain = hslider("piano_gain", 1.0, 0.0, 2.0, 0.0001);   // 0.0 when _volume_cur <= 0.001
lowcut_hz  = hslider("lowcut_hz", 20.0, 20.0, 2000.0, 0.01);
highcut_hz = hslider("highcut_hz", 20000.0, 200.0, 20000.0, 0.01);

eq0_freq = hslider("eq0_freq", 150.0,   20.0, 20000.0, 0.01);
eq0_gain = hslider("eq0_gain", 2.0,    -18.0, 18.0, 0.001);
eq0_q    = hslider("eq0_q",    0.8,      0.1, 20.0, 0.001);
eq0_on   = hslider("eq0_on",   1.0,      0.0, 1.0, 1.0);
eq1_freq = hslider("eq1_freq", 300.0,   20.0, 20000.0, 0.01);
eq1_gain = hslider("eq1_gain", -2.5,   -18.0, 18.0, 0.001);
eq1_q    = hslider("eq1_q",    1.0,      0.1, 20.0, 0.001);
eq1_on   = hslider("eq1_on",   1.0,      0.0, 1.0, 1.0);
eq2_freq = hslider("eq2_freq", 2800.0,  20.0, 20000.0, 0.01);
eq2_gain = hslider("eq2_gain", -3.0,   -18.0, 18.0, 0.001);
eq2_q    = hslider("eq2_q",    1.5,      0.1, 20.0, 0.001);
eq2_on   = hslider("eq2_on",   1.0,      0.0, 1.0, 1.0);
eq3_freq = hslider("eq3_freq", 10000.0, 20.0, 20000.0, 0.01);
eq3_gain = hslider("eq3_gain", -1.5,   -18.0, 18.0, 0.001);
eq3_q    = hslider("eq3_q",    0.7,      0.1, 20.0, 0.001);
eq3_on   = hslider("eq3_on",   1.0,      0.0, 1.0, 1.0);

// ── DF2-transposed biquad — the exact scipy.signal.lfilter /
// bridge_biquad recurrence Python's Biquad* classes run (same rounding
// order → state trajectories match sample-for-sample). Tick lives at top
// level with coefficients as explicit args (with-local closures inside
// `~` get lambda-lifted into phantom inputs — Phase 1 gotcha).
biquad_tick(b0, b1, b2, a1, a2, s1p, s2p, x) = s1, s2, y
with {
    y  = b0 * x + s1p;
    s1 = b1 * x - a1 * y + s2p;
    s2 = b2 * x - a2 * y;
};
biquad_df2t(b0, b1, b2, a1, a2) =
    (biquad_tick(b0, b1, b2, a1, a2) ~ si.bus(2)) : (!, !, _);

// Frozen-state variant (per-band EQ enable): when run <= 0.5 the state
// registers HOLD instead of ticking — the exact analogue of Python
// skipping a disabled band's .process(). The y output is garbage while
// frozen; callers select the dry input instead. run is block-constant.
biquad_tick_frozen(run, b0, b1, b2, a1, a2, s1p, s2p, x) = s1n, s2n, y
with {
    y   = b0 * x + s1p;
    s1  = b1 * x - a1 * y + s2p;
    s2  = b2 * x - a2 * y;
    s1n = select2(run > 0.5, s1p, s1);
    s2n = select2(run > 0.5, s2p, s2);
};
biquad_df2t_frozen(run, b0, b1, b2, a1, a2) =
    (biquad_tick_frozen(run, b0, b1, b2, a1, a2) ~ si.bus(2)) : (!, !, _);

// RBJ lowpass — coefficient formulas + clamps copied from
// BiquadLowpass.set_params (synth_engine.py:372).
lp(fc, q) = biquad_df2t(b0v, b1v, b2v, a1v, a2v)
with {
    fcc   = max(20.0, min(fc, ma.SR * 0.45));
    qc    = max(0.1, min(q, 10.0));
    w0    = 2.0 * ma.PI * fcc / ma.SR;
    cw    = cos(w0);
    alpha = sin(w0) / (2.0 * qc);
    a0    = 1.0 + alpha;
    b0v   = ((1.0 - cw) / 2.0) / a0;
    b1v   = (1.0 - cw) / a0;
    b2v   = ((1.0 - cw) / 2.0) / a0;
    a1v   = (-2.0 * cw) / a0;
    a2v   = (1.0 - alpha) / a0;
};

// RBJ highpass — BiquadHighpass.set_params (synth_engine.py:406).
hpf(fc, q) = biquad_df2t(b0v, b1v, b2v, a1v, a2v)
with {
    fcc   = max(20.0, min(fc, ma.SR * 0.45));
    qc    = max(0.1, min(q, 10.0));
    w0    = 2.0 * ma.PI * fcc / ma.SR;
    cw    = cos(w0);
    alpha = sin(w0) / (2.0 * qc);
    a0    = 1.0 + alpha;
    b0v   = ((1.0 + cw) / 2.0) / a0;
    // NB: NOT `-(1.0 + cw)` — in Faust `-(x)` is partial application of
    // subtraction (a phantom-input block), not unary negation.
    b1v   = (0.0 - (1.0 + cw)) / a0;
    b2v   = ((1.0 + cw) / 2.0) / a0;
    a1v   = (-2.0 * cw) / a0;
    a2v   = (1.0 - alpha) / a0;
};

// RBJ peaking EQ, frozen-state — BiquadPeakingEQ.set_params
// (synth_engine.py:440). NOTE its q clamp is 20.0, not the lowpass's 10.
peq_frozen(run, fc, g, q) = biquad_df2t_frozen(run, b0v, b1v, b2v, a1v, a2v)
with {
    fcc   = max(20.0, min(fc, ma.SR * 0.45));
    qc    = max(0.1, min(q, 20.0));
    a     = pow(10.0, g / 40.0);
    w0    = 2.0 * ma.PI * fcc / ma.SR;
    cw    = cos(w0);
    alpha = sin(w0) / (2.0 * qc);
    a0    = 1.0 + alpha / a;
    b0v   = (1.0 + alpha * a) / a0;
    b1v   = (-2.0 * cw) / a0;
    b2v   = (1.0 - alpha * a) / a0;
    a1v   = (-2.0 * cw) / a0;
    a2v   = (1.0 - alpha / a) / a0;
};

// One EQ band: enabled → process (state ticks), disabled → dry pass with
// state HELD (Python skips the += / .process() entirely).
eq_band(en, fc, g, q, x) = select2(en > 0.5, x, x : peq_frozen(en, fc, g, q));

// Full per-channel chain — each syntactic instantiation below owns its
// own state, so L and R get independent filter instances like Python's
// *_l / *_r pairs. Order mirrors render_block: volume → low-cut ×2 →
// high-cut ×2 → EQ bands 0..3.
chain(x) = x * piano_gain
    : hpf(lowcut_hz, 0.707) : hpf(lowcut_hz, 0.707)
    : lp(highcut_hz, 0.707) : lp(highcut_hz, 0.707)
    : eq_band(eq0_on, eq0_freq, eq0_gain, eq0_q)
    : eq_band(eq1_on, eq1_freq, eq1_gain, eq1_q)
    : eq_band(eq2_on, eq2_freq, eq2_gain, eq2_q)
    : eq_band(eq3_on, eq3_freq, eq3_gain, eq3_q);

piano_chain(l, r) = out_l, out_r, mono
with {
    out_l = chain(l);
    out_r = chain(r);
    // Comp sidechain — the exact `mono = (left + right) * 0.5` Python
    // computes post-EQ when the comp gate is open. Computed here every
    // block (cheap) so the Python side never re-touches L/R to build it.
    mono = (out_l + out_r) * 0.5;
};

process = piano_chain;
