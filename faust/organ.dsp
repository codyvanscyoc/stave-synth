declare name "stave_organ";
declare description "B3 Hammond organ: 16-voice tonewheel bank (9 drawbars x 9 harmonics with 2nd+3rd imperfection + crosstalk) + per-voice keyboard pan + soft drive + stereo split Leslie (horn Doppler + drum AM) + tilt EQ + volume";

import("stdfaust.lib");

// ═══════════════════════════════════════════════════════════════════════
// Config constants
// ═══════════════════════════════════════════════════════════════════════
NVOICES = 16;
NDRAWBARS = 9;
TWO_PI = 2.0 * ma.PI;
SR = ma.SR;

harm_ratio(h) = ba.take(h+1, (0.5, 1.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0));
TW_2ND = 0.02;
TW_3RD = 0.008;

// Leslie speaker
XOVER_HZ         = 800.0;
HORN_RAMP_SEC    = 0.8;
DRUM_RAMP_SEC    = 3.0;
DRUM_SPEED_RATIO = 0.85;
HORN_DOPPLER_MAX = 12.0;

// ═══════════════════════════════════════════════════════════════════════
// Per-voice runtime params
//   freq_v%i: fundamental Hz; gate_v%i: vel×env; phase_v%i: random phase;
//   pan_v%i: -1..+1 from Python (already scaled by width × MIDI position)
// ═══════════════════════════════════════════════════════════════════════
voice_freq(i)  = hslider("freq_v%i",  0, 0, 12000, 0.01);
// 1 ms smoothing matches osc_bank — de-clicks block boundaries without
// muddying the organ's percussive key attack (the click transient is part
// of the character and we don't want it rounded off).
voice_gate(i)  = hslider("gate_v%i",  0, 0, 1,     0.001) : si.smooth(ba.tau2pole(0.001));
voice_phase(i) = hslider("phase_v%i", 0, 0, 1,     0.001);
voice_pan(i)   = hslider("pan_v%i",   0, -1, 1,    0.001) : si.smoo;

amp_d(h) = hslider("amp_d%h", 0, 0, 2, 0.0001) : si.smoo;

tw_signal(i, h) = sin(theta) + TW_2ND * sin(2.0 * theta) + TW_3RD * sin(3.0 * theta)
with {
    inc      = voice_freq(i) * harm_ratio(h) / SR;
    phasor01 = (+(inc) : ma.frac) ~ _;
    phased   = ma.frac(phasor01 + voice_phase(i));
    theta    = TWO_PI * phased;
};

// Mono voice — 9 drawbar-weighted tonewheels × gate
mono_voice(i) = par(h, NDRAWBARS, tw_signal(i, h) * amp_d(h)) :> _ : *(voice_gate(i));

// ═══════════════════════════════════════════════════════════════════════
// Per-voice equal-power pan → stereo
//   angle = (p+1) × π/4   ∈ [0, π/2]
//   gL = cos(angle) × √2,  gR = sin(angle) × √2
// Constant-power law: gL²+gR² = 2, so summing unrelated signals preserves
// energy; a centered signal (p=0) has gL=gR=1.
// ═══════════════════════════════════════════════════════════════════════
stereo_voice(i) = mono_voice(i) <: *(gL), *(gR)
with {
    p     = max(-1.0, min(1.0, voice_pan(i)));
    angle = (p + 1.0) * 0.25 * ma.PI;
    gL    = cos(angle) * sqrt(2.0);
    gR    = sin(angle) * sqrt(2.0);
};

// Sum 16 stereo voices → stereo bank
stereo_bank = par(i, NVOICES, stereo_voice(i)) :> _, _;

// ═══════════════════════════════════════════════════════════════════════
// Drive — tanh saturation, crossfaded against dry (drive=0 → fully clean)
// ═══════════════════════════════════════════════════════════════════════
drive = hslider("drive", 0.05, 0, 1, 0.001) : si.smoo;
drive_stage(x) = x * (1.0 - drive) + wet * drive
with {
    drive_gain = 1.0 + drive * 0.5;
    wet = ma.tanh(x * drive_gain) / ma.tanh(drive_gain);
};

// ═══════════════════════════════════════════════════════════════════════
// Tone tilt — balanced EQ for warm ↔ bright without volume change
// ═══════════════════════════════════════════════════════════════════════
tone_tilt = hslider("tone_tilt", 0.5, 0, 1, 0.001) : si.smoo;
tilt_amount = (tone_tilt - 0.5) * 2.0;
low_gain_db  = -tilt_amount * 4.0;
high_gain_db = tilt_amount * 4.0;
tone_tilt_eq = fi.peak_eq_cq(low_gain_db, 250, 1.0)
             : fi.peak_eq_cq(high_gain_db, 3000, 1.0);

// ═══════════════════════════════════════════════════════════════════════
// Leslie — stereo split (crossover + horn AM+Doppler + drum AM per channel)
// Speeds smoothed with different time constants per rotor.
// Each textual xover_hp/_lp/doppler_* call creates its own filter state,
// so L and R have independent stateful chains (true stereo behavior).
// ═══════════════════════════════════════════════════════════════════════
leslie_depth     = hslider("leslie_depth", 0.3, 0, 1, 0.001) : si.smoo;
leslie_target_hz = hslider("leslie_target_hz", 0.8, 0.1, 10.0, 0.001);

horn_hz = leslie_target_hz                       : si.smooth(ba.tau2pole(HORN_RAMP_SEC));
drum_hz = leslie_target_hz * DRUM_SPEED_RATIO    : si.smooth(ba.tau2pole(DRUM_RAMP_SEC));

horn_phasor = (+(horn_hz / SR) : ma.frac) ~ _;
drum_phasor = (+(drum_hz / SR) : ma.frac) ~ _;

horn_sin = sin(TWO_PI * horn_phasor);
horn_cos = cos(TWO_PI * horn_phasor);
drum_sin = sin(TWO_PI * drum_phasor);
drum_cos = cos(TWO_PI * drum_phasor);

horn_l_am(x) = x * (1.0 + leslie_depth * horn_sin);
horn_r_am(x) = x * (1.0 + leslie_depth * horn_cos);
drum_l_am(x) = x * (1.0 + leslie_depth * 0.5 * drum_sin);
drum_r_am(x) = x * (1.0 + leslie_depth * 0.5 * drum_cos);

doppler_depth = HORN_DOPPLER_MAX * leslie_depth;
base_delay    = doppler_depth + 2.0;
doppler_dl    = base_delay + horn_sin * doppler_depth;

doppler_l(x) = de.fdelay4(2048, doppler_dl, x);
doppler_r(x) = de.fdelay4(2048, doppler_dl, x);

// Per-channel Leslie. Each call to xover_hp/xover_lp instantiates an
// independent 2-pole biquad, so the L and R paths don't share filter state.
leslie_L(m) = (m : fi.highpass(2, XOVER_HZ) : horn_l_am : doppler_l)
            + (m : fi.lowpass (2, XOVER_HZ) : drum_l_am);
leslie_R(m) = (m : fi.highpass(2, XOVER_HZ) : horn_r_am : doppler_r)
            + (m : fi.lowpass (2, XOVER_HZ) : drum_r_am);

stereo_leslie = leslie_L, leslie_R;

// ═══════════════════════════════════════════════════════════════════════
// Volume + tone — per channel, independent filter state
// ═══════════════════════════════════════════════════════════════════════
volume = hslider("volume", 0.5, 0, 1, 0.001) : si.smoo;
gain   = pow(10.0, (volume - 1.0) * 2.0);

highcut_hz = hslider("highcut_hz", 8000, 200, 12000, 1.0);
lowcut_hz  = hslider("lowcut_hz",    40,  20,   500, 0.5);

tone_stage_mono = tone_tilt_eq
                : *(gain)
                : fi.highpass(1, lowcut_hz)
                : fi.lowpass (1, highcut_hz);

// ═══════════════════════════════════════════════════════════════════════
// Signal flow
//   process: 1 input (click mono) → 2 outputs (stereo)
//   stereo_bank + click-broadcast → per-channel drive → stereo Leslie
//   → per-channel tone stage.
// ═══════════════════════════════════════════════════════════════════════
// combine_click(vL, vR, click) = (vL+click, vR+click)
combine_click(vL, vR, c) = vL + c, vR + c;

process = (stereo_bank, _) : combine_click
        : drive_stage, drive_stage
        : stereo_leslie
        : tone_stage_mono, tone_stage_mono;
