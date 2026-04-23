declare name "stave_bus_comp";
declare description "SSL G-style bus compressor — self-sidechain only";

import("stdfaust.lib");

// ═══════════════════════════════════════════════════════════════════════
// Simplified port of BusCompressor (synth_engine.py:1034–1176).
//
// SUPPORTED:
//   - Self-sidechain (L+R mono sum, HPF'd)
//   - Soft-knee downward compression
//   - Attack/release ballistics (asymmetric smoothing via si.lag_ud)
//   - Makeup gain
//   - Parallel (dry/wet) mix
//
// NOT PORTED (use Python path if you need these):
//   - External sidechain sources (piano/lfo/bpm)
//   - Auto-release mode (dual-stage release scaled by GR)
//   - Block-average detection (this version is per-sample)
//   - FX-bypass routing (Python wrapper handles that)
//
// Per-sample detection differs from Python's block-average detection but
// on slow bus-comp attack/release times (≥3 ms attack, ≥100 ms release)
// both give audibly similar results.
// ═══════════════════════════════════════════════════════════════════════

enabled      = hslider("enabled",      0,   0,  1,      1);
threshold_db = hslider("threshold_db",-10, -60, 0,      0.1) : si.smoo;
ratio        = hslider("ratio",        4,   1, 20,      0.1) : si.smoo;
attack_ms    = hslider("attack_ms",    3, 0.1, 100,     0.1);
release_ms   = hslider("release_ms", 300,  10, 2000,    1);
knee_db      = hslider("knee_db",      2,   0, 12,      0.1) : si.smoo;
makeup_db    = hslider("makeup_db",    0, -24, 24,      0.1) : si.smoo;
mix          = hslider("mix",          1,   0,  1,   0.001) : si.smoo;
sc_hpf_hz    = hslider("sc_hpf_hz",  100,  20, 500,     1)   : si.smoo;

SR  = ma.SR;
EPS = 1e-12;

// One-pole leaky integrator for RMS detection: y = α·x + (1-α)·y[-1].
// 5 ms window matches Python (synth_engine.py:1066).
rms_alpha = 1.0 - exp(-1.0 / (0.005 * SR));
leaky_int = *(rms_alpha) : + ~ *(1.0 - rms_alpha);

process(l, r) = l_out, r_out
with {
    // ── Detection ──
    sc        = (l + r) * 0.5 : fi.highpass(2, sc_hpf_hz);
    env_pow   = sc * sc : leaky_int;
    env_db    = 10.0 * log10(env_pow + EPS);

    // ── Soft-knee gain-reduction target (dB, positive = reduction) ──
    over       = env_db - threshold_db;
    half_knee  = knee_db * 0.5;
    ratio_gain = 1.0 - 1.0 / max(ratio, 1.0);
    gr_above   = over * ratio_gain;
    x_knee     = (over + half_knee) / max(knee_db, EPS);
    gr_knee    = x_knee * x_knee * knee_db * ratio_gain;
    gr_target  = select2(over < (0.0 - half_knee),
                    select2(over > half_knee, gr_knee, gr_above),
                    0.0);

    // ── Ballistic smoothing: attack when rising, release when falling ──
    // Routed through hbargraph("gr_db") so Python can read it for the UI's
    // GR LED without affecting the audio chain.
    gr_db = gr_target : si.lag_ud(attack_ms * 0.001, release_ms * 0.001)
                      : hbargraph("gr_db", 0, 24);

    // ── Gain in linear amplitude ──
    gain_db = 0.0 - gr_db + makeup_db;
    gain    = pow(10.0, gain_db / 20.0);

    // ── Apply with dry/wet mix × enable flag ──
    mix_eff = mix * enabled;
    dry_w   = 1.0 - mix_eff;
    l_out   = l * dry_w + l * gain * mix_eff;
    r_out   = r * dry_w + r * gain * mix_eff;
};
