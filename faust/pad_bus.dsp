declare name "stave_pad_bus";
declare description "Phase 1+2 render-skeleton port: Haas + filter routing + shared 12/24dB lowpass + filter comp + independent per-OSC filters + shared highpass + per-OSC reverb-send filter path + shimmer chain (HP split, mix, CLOUD multi-tap)";

// ═══════════════════════════════════════════════════════════════════════
// Phase 1 scope (details in FAUST_PORT_PLAN.md "Phase 1 implementation
// notes"). Mirrors synth_engine._render_locked ~lines 2905-3151 + the
// reverb-send slow path (~3393-3406) — the PRE-LFO section of the skeleton.
// Phase 2 adds the shimmer chain (engine lines ~3513-3535): see the
// "Phase 2" banner further down.
//
// PORTED here:
//   • OSC2 Haas delay (hard-pan WIDE), integer-sample, write gated by
//     haas_on so the line holds zeros while inactive (mirrors Python's
//     conditional ring-buffer write)
//   • Routing of osc1 / osc2(post-Haas) into shared vs independent filter
//     branches by osc*_filter_enabled, incl. the render_osc2 dry gate
//   • Shared lowpass: 12 dB (single RBJ biquad, Q = resonance) / 24 dB
//     (staggered-Q Butterworth cascade, ratios 0.5412/0.707 stage 1 and
//     1.3066/0.707 stage 2 — synth_engine._Q24_S1_RATIO/_Q24_S2_RATIO)
//   • Filter comp 10^(-15·f_pos^1.3/20) applied between stage 1 and 2
//   • Independent per-OSC 12 dB lowpass branches (Q 0.707) + their own
//     comps from osc*_f_pos
//   • Shared highpass (low cut, Q 0.707) on the dry sum
//   • Per-OSC reverb-send weighted sum → dedicated send-filter path
//     (_rev_send_filter_* mirror: stage1 → ×comp → stage2 when 24 dB)
//
// NOT ported (stays in Python for Phase 1):
//   • LFO amp/pan application — sits between the highpass and the FX
//     bypass carve; the block-ramp smoothing character is deliberate
//     (plan: revisit in Phase 4 only with A/B)
//   • FX-bypass dry carve — happens POST-LFO on a data-dependent
//     magnitude ratio, so outputs 5/6 (bypass_l/r) are reserved and
//     always 0.0 in Phase 1. NOTE (follow-the-code): the osc*_fx_bypass
//     flags do NOT reroute the wet send to the bypass bus — Python zeroes
//     that OSC's send weight (pushes send=0 here) and carves the dry bus
//     post-LFO instead.
//   • Per-OSC pan law + width — NOT part of this region at all: pan is
//     applied per-unison inside oscillator generation (osc_bank.dsp owns
//     it on the Faust path). Only the Haas half of WIDE lives here.
//   • Reverb-send FAST path (both sends == 1.0, no bypass): Python copies
//     the post-LFO/post-delay dry bus into reverb_in — the engine keeps
//     that decision and ignores send_l/r on those blocks. The
//     send_filter_active zone gates this module's send-filter INPUT so
//     its state advances only on slow-path blocks, in lockstep with
//     Python's _rev_send_filter_* instances.
// ── Phase 2 (2026-07-22): shimmer chain, ported HERE (not a separate
// module) because its input is this module's existing input 5 and its
// output lands at the same reverb-entry point the send path computes
// toward — one merged compute, no new Python↔Faust island.
//
// PORTED in Phase 2:
//   • Shimmer HP split (BiquadHighpass, 400 Hz in +12 mode / 1200 Hz in
//     +24 mode — Python pushes the cutoff; coefficients swap with STATE
//     CARRIED, mirroring set_params-without-reset). The biquad state is
//     FROZEN (not decayed) while the chain is inactive — exactly like
//     Python skipping .process() — so re-engagement is sample-exact.
//   • Mix scaling: shimmer_sig = HP_out × shimmer_mix_cur. Python OWNS
//     the ~80 ms one-pole mix smoothing (it only advances on active
//     blocks) and pushes the smoothed block-constant value. NO si.smoo.
//   • CLOUD multi-tap pre-reverb delay: mono ring, 4 stereo-offset taps
//     per channel (gains 0.65/0.50/0.36/0.22 — synth_engine's
//     _shimmer_taps_*). Implemented as ONE rwtable whose write index
//     advances only while the cloud is active — Python's ring freezes
//     (no write, no idx advance) when shimmer is off or CLOUD send is 0,
//     and this mirrors that exactly, INCLUDING the stale-content replay
//     on re-engage. Ring length wraps at int(0.6·SR) (shim_ring_len
//     zone) like Python's buffer, inside a 65536-slot table (96 kHz
//     headroom: max tap int(0.523·96000) = 50208 < 57600 = ring len).
//   • Tap offsets arrive as zones computed by the WRAPPER with the exact
//     Python expression int(sec·SR) — float truncation differs from any
//     in-Faust recompute (int(0.289·96000) = 27743, not 27744).
//   • shimmer_high (+12/+24) touches THIS region only via the HP cutoff
//     zone — the pitch multiple itself lives in the osc bank upstream.
//
// NOT ported / Phase 2 glue that stays in Python:
//   • render_shimmer gate (shimmer_enabled AND mix > 0.001) and the
//     voice_idx > 0 gate — pushed as one shimmer_active zone.
//   • The reverb_in adds: Python adds output 7 to both channels, then
//     outputs 8/9 (already ×shimmer_send) per channel, preserving the
//     exact legacy add order (shimmer, then cloud).
//   • The former inert shimmer_send_gain hook is REMOVED: shimmer joins
//     reverb_in on fast-path blocks too (where send_L/R are ignored), so
//     it gets dedicated outputs instead of riding the send bus.
//
// I/O:
//   inputs  : osc1_L, osc1_R, osc2_L, osc2_R, shimmer_mono (osc_bank order)
//   outputs : dry_L, dry_R, send_L, send_R, bypass_L, bypass_R,
//             shim_mono, cloud_L, cloud_R
//
// Built with -double + -DFAUSTFLOAT=double (see faust/build.sh): the
// pole-near-unity biquads at low cutoffs cannot hold the ~1e-6 parity bar
// in float32, and double zones/IO let the wrapper pass the engine's
// float64 blocks zero-copy.
//
// Smoothing policy: NO si.smoo anywhere in this module. Every zone is
// either already ramped per block by Python (cutoff_hz via the 80 ms
// log-space smoother, f_pos derived from it, indep cutoffs, highpass
// cutoff) or jumps instantly in the Python path too (enables, slope,
// resonance, sends, haas). Double-smoothing would break parity — see the
// gotchas list in FAUST_PORT_PLAN.md.
// ═══════════════════════════════════════════════════════════════════════

import("stdfaust.lib");

// ── Zones — one hslider per parameter, named after the Python attribute
// where one exists. All written once per block by synth_engine.
// cutoff_hz mirrors _filter_cutoff_last_set: the ">0.1 Hz changed" update
// skip lives in Python; we consume the last-SET value, not the raw
// smoothed cutoff, so coefficients match Python's exactly.
cutoff_hz         = hslider("cutoff_hz", 8000.0, 20.0, 43200.0, 0.01);
resonance         = hslider("filter_resonance", 0.707, 0.1, 10.0, 0.001);
slope24           = hslider("filter_slope24", 0.0, 0.0, 1.0, 1.0);       // filter_slope == 24
f_pos             = hslider("f_pos", 1.0, 0.0, 1.0, 0.0001);             // Python-computed log-zone position
fe1               = hslider("osc1_filter_enabled", 1.0, 0.0, 1.0, 1.0);
fe2               = hslider("osc2_filter_enabled", 1.0, 0.0, 1.0, 1.0);
osc1_indep_cutoff = hslider("osc1_indep_cutoff", 20000.0, 20.0, 43200.0, 0.01); // _osc1_indep_cutoff_cur
osc2_indep_cutoff = hslider("osc2_indep_cutoff", 20000.0, 20.0, 43200.0, 0.01); // _osc2_indep_cutoff_cur
osc1_f_pos        = hslider("osc1_f_pos", 1.0, 0.0, 1.0, 0.0001);
osc2_f_pos        = hslider("osc2_f_pos", 1.0, 0.0, 1.0, 0.0001);
hp_on             = hslider("filter_highpass_on", 0.0, 0.0, 1.0, 1.0);   // _filter_highpass_cur > 25.0
hp_cutoff         = hslider("filter_highpass_hz", 20.0, 20.0, 5000.0, 0.01); // _filter_highpass_cur (Python-smoothed)
osc1_reverb_send  = hslider("osc1_reverb_send", 1.0, 0.0, 2.0, 0.001);   // 0 when osc1_fx_bypass
osc2_reverb_send  = hslider("osc2_reverb_send", 1.0, 0.0, 2.0, 0.001);   // 0 when osc2_fx_bypass
send_filter_active = hslider("send_filter_active", 0.0, 0.0, 1.0, 1.0);  // 1 on slow-path blocks only
haas_on           = hslider("haas_on", 0.0, 0.0, 1.0, 1.0);              // haas_active AND render_osc2
haas_samps        = hslider("haas_delay_samps", 960.0, 0.0, 4095.0, 1.0); // _haas_delay_samples
osc2_audible      = hslider("osc2_audible", 1.0, 0.0, 1.0, 1.0);         // render_osc2 (dry routing gate)

// ── Phase 2 shimmer zones ──
// shimmer_active mirrors (render_shimmer and voice_idx > 0) — the exact
// Python gate on the whole chain. Everything shimmer freezes when it's 0.
shimmer_active    = hslider("shimmer_active", 0.0, 0.0, 1.0, 1.0);
shimmer_hp_hz     = hslider("shimmer_hp_hz", 1200.0, 20.0, 5000.0, 0.01); // 1200 if shimmer_high else 400
shimmer_mix_cur   = hslider("shimmer_mix_cur", 0.5, 0.0, 1.0, 0.0001);   // _shimmer_mix_cur (Python-smoothed)
shimmer_send      = hslider("shimmer_send", 1.0, 0.0, 2.0, 0.0001);      // CLOUD wet level (unsmoothed in Python too)
// Init-time zones, written ONCE by the wrapper with Python's exact ints
// (defaults below are the 48 kHz values — documentation only).
shim_ring_len     = hslider("shim_ring_len", 28800.0, 1.0, 65536.0, 1.0);   // int(0.6·SR)
shim_tap_l1       = hslider("shim_tap_l1",  6240.0, 0.0, 65535.0, 1.0);     // int(0.130·SR)
shim_tap_l2       = hslider("shim_tap_l2", 11856.0, 0.0, 65535.0, 1.0);     // int(0.247·SR)
shim_tap_l3       = hslider("shim_tap_l3", 17424.0, 0.0, 65535.0, 1.0);     // int(0.363·SR)
shim_tap_l4       = hslider("shim_tap_l4", 23088.0, 0.0, 65535.0, 1.0);     // int(0.481·SR)
shim_tap_r1       = hslider("shim_tap_r1",  8304.0, 0.0, 65535.0, 1.0);     // int(0.173·SR)
shim_tap_r2       = hslider("shim_tap_r2", 13871.0, 0.0, 65535.0, 1.0);     // int(0.289·SR) — yes, 13871: float truncation
shim_tap_r3       = hslider("shim_tap_r3", 19440.0, 0.0, 65535.0, 1.0);     // int(0.405·SR)
shim_tap_r4       = hslider("shim_tap_r4", 25104.0, 0.0, 65535.0, 1.0);     // int(0.523·SR)

// Staggered-Q Butterworth ratios — synth_engine._Q24_S1_RATIO / _Q24_S2_RATIO
Q24_S1_RATIO = 0.5412 / 0.707;
Q24_S2_RATIO = 1.3066 / 0.707;

// Haas line sized for 40 ms at 96 kHz (3840 samples) + headroom —
// same 96k-headroom policy as ping_pong.dsp's buffers.
HAAS_MAX = 4096;

// CLOUD table: ring wraps at shim_ring_len = int(0.6·SR) (57600 @ 96 kHz),
// stored in the next power of two.
SHIM_TABLE = 65536;

// ── DF2-transposed biquad — the exact scipy.signal.lfilter recurrence
// that Python's BiquadLowpass/BiquadHighpass run, so state trajectories
// match sample-for-sample. (fi.tf2 is a different direct form — same
// transfer function, different rounding order; we mirror exactly.)
//   y[n]  = b0·x[n] + s1[n-1]
//   s1[n] = b1·x[n] − a1·y[n] + s2[n-1]
//   s2[n] = b2·x[n] − a2·y[n]
// (tick lives at top level with the coefficients as explicit args — a
// with-local closure inside `~` gets lambda-lifted into phantom inputs;
// see the "named function, not lambdas" gotcha.)
biquad_tick(b0, b1, b2, a1, a2, s1p, s2p, x) = s1, s2, y
with {
    y  = b0 * x + s1p;
    s1 = b1 * x - a1 * y + s2p;
    s2 = b2 * x - a2 * y;
};
biquad_df2t(b0, b1, b2, a1, a2) =
    (biquad_tick(b0, b1, b2, a1, a2) ~ si.bus(2)) : (!, !, _);

// RBJ lowpass — coefficient formulas + clamps copied from
// BiquadLowpass.set_params (synth_engine.py:371).
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

// Frozen-state variant (Phase 2, shimmer HP only): when run <= 0.5 the
// state registers HOLD their previous values instead of ticking — the
// exact analogue of Python skipping _shimmer_hp.process() on inactive
// blocks. The y output is garbage while frozen; callers must output-gate
// (shimmer_sig is ×shimmer_active downstream). run is block-constant.
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

// RBJ highpass — BiquadHighpass.set_params (synth_engine.py:405).
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
    // subtraction (a 1-input block), which silently adds a phantom input.
    b1v   = (0.0 - (1.0 + cw)) / a0;
    b2v   = ((1.0 + cw) / 2.0) / a0;
    a1v   = (-2.0 * cw) / a0;
    a2v   = (1.0 - alpha) / a0;
};

// Frozen-state RBJ highpass for the shimmer split — same coefficient
// formulas + clamps as hpf above, biquad_df2t_frozen underneath.
hpf_frozen(run, fc, q) = biquad_df2t_frozen(run, b0v, b1v, b2v, a1v, a2v)
with {
    fcc   = max(20.0, min(fc, ma.SR * 0.45));
    qc    = max(0.1, min(q, 10.0));
    w0    = 2.0 * ma.PI * fcc / ma.SR;
    cw    = cos(w0);
    alpha = sin(w0) / (2.0 * qc);
    a0    = 1.0 + alpha;
    b0v   = ((1.0 + cw) / 2.0) / a0;
    b1v   = (0.0 - (1.0 + cw)) / a0;   // NOT `-(...)` — see phantom-input gotcha
    b2v   = ((1.0 + cw) / 2.0) / a0;
    a1v   = (-2.0 * cw) / a0;
    a2v   = (1.0 - alpha) / a0;
};

// CLOUD write-index walker: advances by `step` (0 while the cloud is
// inactive — Python's frozen _shimmer_delay_idx) modulo the ring length.
// Tick lives at top level with captured values as explicit args (the
// lambda-lifting gotcha); `mem` turns the post-increment recursion output
// into the pre-increment write position (0,1,2,... on first engagement,
// exactly Python's idx trajectory).
widx_tick(step, len_, w) = (w + step) % len_;
cloud_widx(step, len_) = (widx_tick(step, len_) ~ _) : mem;

// Brightness-vs-loudness comp — 10^(-15·fp^1.3/20) (synth_engine.py:3110).
comp_of(fp) = pow(10.0, -15.0 * pow(fp, 1.3) / 20.0);

filter_comp = comp_of(f_pos);

// Stage Qs: 12 dB runs stage 1 at the raw resonance; 24 dB re-Qs stage 1
// (same filter INSTANCE — state carries across the slope flip, exactly
// like Python reusing filter_l/r with new params) and adds stage 2.
q_stage1 = select2(slope24 > 0.5, resonance, resonance * Q24_S1_RATIO);
q_stage2 = resonance * Q24_S2_RATIO;

// Shared / rev-send lowpass chain: stage1 → ×comp → stage2-if-24dB.
// (Python applies comp between the stages too — linear either way, but
// mirrored for bit-level closeness.) Stage-2 INPUT is gated by slope24 so
// its state stays zero while in 12 dB mode — mirrors Python where
// filter2_* simply doesn't process. First 12→24 flip is exact; later
// re-flips differ only by a decayed-(Faust) vs frozen-(Python) sub-audible
// state transient (documented in the plan notes).
lp_chain(x) = select2(slope24 > 0.5, y1c, y2)
with {
    y1c = (x : lp(cutoff_hz, q_stage1)) * filter_comp;
    y2  = (y1c * slope24) : lp(cutoff_hz, q_stage2);
};

// Haas: integer-sample delay (Python uses an integer ring-buffer offset —
// no interpolation, mirrored here with de.delay). WRITE gated by haas_on:
// Python only writes its ring buffer while (haas_active and render_osc2),
// so on first engage both lines hold zeros → exact. On RE-engage within
// the window Python replays stale buffer content where we replay zeros —
// transition-only difference, ours arguably cleaner.
haas(x) = select2(haas_on > 0.5, x, delayed)
with {
    delayed = (x * haas_on) : de.delay(HAAS_MAX, int(min(haas_samps, HAAS_MAX - 1)));
};

pad_bus(o1l, o1r, o2l, o2r, shim) = dry_l, dry_r, send_l, send_r, bypass_l, bypass_r,
                                    shim_out, cloud_out_l, cloud_out_r
with {
    // OSC2 post-Haas — feeds BOTH the dry routing and the send tap
    // (Python's _osc2_pre_* snapshot is taken post-Haas, pre-filter,
    // and is NOT gated by render_osc2 — synth_engine.py:2941).
    o2hl = haas(o2l);
    o2hr = haas(o2r);
    // Dry routing drops osc2 entirely when render_osc2 is False
    // (synth_engine.py:2945 gates the routing, not the snapshot).
    o2dl = o2hl * osc2_audible;
    o2dr = o2hr * osc2_audible;

    // Branch inputs — exact mirror of the filter_buf / *_indep_buf routing.
    shared_out_l = (o1l * fe1 + o2dl * fe2) : lp_chain;
    shared_out_r = (o1r * fe1 + o2dr * fe2) : lp_chain;
    indep1_out_l = ((o1l * (1.0 - fe1)) : lp(osc1_indep_cutoff, 0.707)) * comp_of(osc1_f_pos);
    indep1_out_r = ((o1r * (1.0 - fe1)) : lp(osc1_indep_cutoff, 0.707)) * comp_of(osc1_f_pos);
    indep2_out_l = ((o2dl * (1.0 - fe2)) : lp(osc2_indep_cutoff, 0.707)) * comp_of(osc2_f_pos);
    indep2_out_r = ((o2dr * (1.0 - fe2)) : lp(osc2_indep_cutoff, 0.707)) * comp_of(osc2_f_pos);

    // Output gates mirror Python's conditional adds: when a branch is not
    // routed, Python skips the += entirely, so a just-unrouted branch's
    // ring-out must be dropped instantly here too.
    fe_any = 1.0 - (1.0 - fe1) * (1.0 - fe2);
    pre_hp_l = shared_out_l * fe_any + indep1_out_l * (1.0 - fe1) + indep2_out_l * (1.0 - fe2);
    pre_hp_r = shared_out_r * fe_any + indep1_out_r * (1.0 - fe1) + indep2_out_r * (1.0 - fe2);

    // Shared highpass (low cut). Input gated by hp_on → state stays zero
    // while off, exactly like Python's never-processed filter_hp_*.
    dry_l = select2(hp_on > 0.5, pre_hp_l, (pre_hp_l * hp_on) : hpf(hp_cutoff, 0.707));
    dry_r = select2(hp_on > 0.5, pre_hp_r, (pre_hp_r * hp_on) : hpf(hp_cutoff, 0.707));

    // Per-OSC reverb-send weighted sum → dedicated send-filter path.
    // Mirrors synth_engine.py:3394-3406 (_rev_send_filter_* slow path).
    // Input gated by send_filter_active so the filter state advances only
    // on the blocks where Python's instances process.
    send_pre_l = (o1l * osc1_reverb_send + o2hl * osc2_reverb_send) * send_filter_active;
    send_pre_r = (o1r * osc1_reverb_send + o2hr * osc2_reverb_send) * send_filter_active;
    send_l = send_pre_l : lp_chain;
    send_r = send_pre_r : lp_chain;

    // Reserved for a later phase: the FX-bypass carve is post-LFO and
    // data-dependent (magnitude ratio), so it stays in Python. Always 0.
    bypass_l = 0.0;
    bypass_r = 0.0;

    // ── Phase 2: shimmer chain (engine lines ~3513-3535) ──────────────
    // HP split with frozen-while-inactive state, then the Python-smoothed
    // mix scalar. The trailing ×shimmer_active output-gates the frozen-
    // state garbage y (and mirrors Python adding nothing when inactive);
    // it multiplies by exactly 1.0 on active blocks — no rounding impact.
    shim_hp  = (shim * shimmer_active) : hpf_frozen(shimmer_active, shimmer_hp_hz, 0.707);
    shim_sig = shim_hp * shimmer_mix_cur * shimmer_active;
    shim_out = shim_sig;

    // CLOUD: single mono ring (Python writes identical content to its L
    // and R buffers), frozen write index, 4 taps per channel. cactive
    // mirrors Python's compound gate: chain active AND send > 0.001 —
    // write, index advance, and output all stop together.
    cactive  = shimmer_active * (shimmer_send > 0.001);
    cstep    = int(cactive);
    clen     = int(shim_ring_len);
    cw       = shim_sig * cactive;
    wpos     = cloud_widx(cstep, clen);
    // All 8 rwtable calls share (size, init, widx, wsig) → the compiler
    // hash-conses them into ONE table with 8 reads (verified in the
    // generated C). Offsets < clen always, so +clen keeps the C `%`
    // non-negative (Faust `%` is C semantics, not Python's).
    ctap(off) = rwtable(SHIM_TABLE, 0.0, wpos, cw, (wpos - int(off) + clen) % clen);
    cloud_raw_l = ctap(shim_tap_l1) * 0.65 + ctap(shim_tap_l2) * 0.50
                + ctap(shim_tap_l3) * 0.36 + ctap(shim_tap_l4) * 0.22;
    cloud_raw_r = ctap(shim_tap_r1) * 0.65 + ctap(shim_tap_r2) * 0.50
                + ctap(shim_tap_r3) * 0.36 + ctap(shim_tap_r4) * 0.22;
    // ×shimmer_send here so Python's add is a bare += (zero-alloc, and
    // the multiply-then-add order matches `reverb_in += cloud * send`).
    cloud_out_l = cloud_raw_l * shimmer_send * cactive;
    cloud_out_r = cloud_raw_r * shimmer_send * cactive;
};

process = pad_bus;
