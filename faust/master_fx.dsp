declare name "stave_master_fx";
declare description "Master FX tail: 3-band EQ → low cut → pre-gain → saturation → soft limiter";

import("stdfaust.lib");

// ═══════════════════════════════════════════════════════════════════════
// Port of jack_engine.py:557–631 master-bus chain, MINUS:
//   - SSL shuffler (depends on reverb space param; separate system)
//   - Bus compressor (deferred — complex SSL G ballistics + sidechain modes)
//   - FX-bypass routing (Python handles)
//
// Chain: stereo in → EQ(3 bands) → HP(6/12/24) → pre-gain → sat → tanh.
// ═══════════════════════════════════════════════════════════════════════

// ─── 3-band parametric EQ ───
eq1_freq = hslider("eq1_freq", 200,  20, 24000, 0.1) : si.smoo;
eq1_gain = hslider("eq1_gain",   0, -24, 24,    0.01) : si.smoo;
eq1_q    = hslider("eq1_q",    1.5, 0.1, 10,    0.01) : si.smoo;
eq2_freq = hslider("eq2_freq",1000,  20, 24000, 0.1) : si.smoo;
eq2_gain = hslider("eq2_gain",   0, -24, 24,    0.01) : si.smoo;
eq2_q    = hslider("eq2_q",    1.5, 0.1, 10,    0.01) : si.smoo;
eq3_freq = hslider("eq3_freq",5000,  20, 24000, 0.1) : si.smoo;
eq3_gain = hslider("eq3_gain",   0, -24, 24,    0.01) : si.smoo;
eq3_q    = hslider("eq3_q",    1.5, 0.1, 10,    0.01) : si.smoo;

// ─── Master HP ───
hp_enable = hslider("hp_enable", 0,  0, 1, 1);
hp_freq   = hslider("hp_freq",  80, 20, 2000, 0.1) : si.smoo;
// Slope select: 6 / 12 / 24 dB per octave.
hp_slope  = hslider("hp_slope", 12, 6, 24, 6);

// ─── Post-comp chain ───
pre_gain   = hslider("pre_gain",   2.0, 0.1, 10.0, 0.01) : si.smoo;
sat_enable = hslider("sat_enable",   0,   0,    1,    1);
// Limiter (tanh) is always on — this is what keeps the bus from clipping.

// ═══════════════════════════════════════════════════════════════════════
// Single-channel chain — par(c, 2, ...) gives independent L/R state
// ═══════════════════════════════════════════════════════════════════════

// 3 peaking biquads in series. fi.peak_eq_cq is RBJ-cookbook constant-Q.
eq_chain = fi.peak_eq_cq(eq1_gain, eq1_freq, eq1_q)
         : fi.peak_eq_cq(eq2_gain, eq2_freq, eq2_q)
         : fi.peak_eq_cq(eq3_gain, eq3_freq, eq3_q);

// Variable-slope HP — all three run in parallel, `select2` picks one.
// Small CPU waste (~2 extra biquads); keeps topology simple, avoids
// state-clear clicks on slope changes.
// KNOWN: when slope changes, the newly-selected biquad's state has been
// running on silence-from-other-paths-output but the input is still hot
// → minor click on switch. Fix would require a state reset on the active
// chain (zi clears) which is banned by the project's filter-state rule.
hp_chain(x) =
    select2(hp_enable < 0.5,
        select2(hp_slope < 9,
            select2(hp_slope < 18,
                fi.highpass(4, hp_freq, x),    // 24 dB → 4-pole
                fi.highpass(2, hp_freq, x)),   // 12 dB → 2-pole
            fi.highpass(1, hp_freq, x)),       // 6 dB → 1-pole
        x);                                    // HP disabled → pass-through

// Saturation: Python line 623-628 — x*1.01 + |x|*0.09 (2nd-harmonic bias).
sat_apply(x) = x * 1.01 + abs(x) * 0.09;
sat_chain(x) = select2(sat_enable < 0.5, sat_apply(x), x);

// ─── Stereo pipeline ───
// par(c, 2, mono_chain) keeps per-channel biquad state independent.
process =
    par(c, 2, eq_chain : hp_chain)
    : par(c, 2, _ * pre_gain : sat_chain : ma.tanh);
