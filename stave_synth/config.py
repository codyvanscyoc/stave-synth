"""Default configuration and paths for Stave Synth."""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "stave-synth"
PRESETS_DIR = CONFIG_DIR / "presets"
DATA_DIR = Path.home() / ".local" / "share" / "stave-synth"
SOUNDFONT_DIR = DATA_DIR / "soundfonts"
STATE_FILE = CONFIG_DIR / "current_state.json"

# Audio
SAMPLE_RATE = 48000

# Network
WEBSOCKET_HOST = "0.0.0.0"
WEBSOCKET_PORT = 8765
HTTP_PORT = 8080

# Auto-save interval in seconds
AUTOSAVE_INTERVAL = 30

# BTL USB audio adapter: invert right channel so headphones hear L - R.
# Set False for normal audio interfaces.
BTL_MODE = False

# Route reverb through the Faust-compiled native DSP (faust/libstave_reverb.so)
# instead of the numpy-based FeedbackDelayReverb. 8x faster but a fresh port —
# A/B with the Python path by flipping STAVE_FAUST_REVERB in the environment.
USE_FAUST_REVERB = os.environ.get("STAVE_FAUST_REVERB", "0") not in ("0", "", "false", "False")

# Same story for the stereo ping-pong delay.
USE_FAUST_PING_PONG = os.environ.get("STAVE_FAUST_PING_PONG", "0") not in ("0", "", "false", "False")

# 16-voice Faust oscillator bank (wave gen + unison + pan + blend).
# Current iteration: unison hardcoded to 3, ADSR stays Python-side.
USE_FAUST_OSC_BANK = os.environ.get("STAVE_FAUST_OSC_BANK", "0") not in ("0", "", "false", "False")

# Sympathetic resonance rendered in Faust (stereo bank of 16 slots).
USE_FAUST_SYMPATHETIC = os.environ.get("STAVE_FAUST_SYMPATHETIC", "0") not in ("0", "", "false", "False")

# B3 organ engine rendered in Faust (tonewheel bank + Leslie).
USE_FAUST_ORGAN = os.environ.get("STAVE_FAUST_ORGAN", "0") not in ("0", "", "false", "False")

DEFAULT_STATE = {
    "synth_pad": {
        "osc1_blend": 0.6,
        "osc2_blend": 0.4,
        "osc1_max": 1.0,
        "osc2_max": 1.0,
        "osc1_waveform": "sine",
        "osc2_waveform": "square",
        "unison_voices": 1,
        "unison_detune": 0.07,
        "unison_spread": 0.85,
        "osc1_pan": 0.0,
        "osc2_pan": 0.0,
        "osc_hard_pan": False,
        "osc_levels_linked": False,
        "haas_delay_ms": 20.0,
        "adsr_osc1": {
            "attack_ms": 200,
            "decay_ms": 1500,
            "sustain_percent": 80,
            "release_ms": 500,
        },
        "adsr_osc2": {
            "attack_ms": 200,
            "decay_ms": 1500,
            "sustain_percent": 80,
            "release_ms": 500,
        },
        "filter_cutoff_hz": 8000,
        "filter_resonance": 0.707,
        "filter_slope": 12,
        "filter_highpass_hz": 20,
        "filter_range_min": 150,
        "filter_range_max": 20000,
        "osc1_filter_enabled": True,
        "osc2_filter_enabled": True,
        # Analog warmth: subtle-by-default per-voice pitch drift + global
        # filter drift. Wobble is off by default (kicks in near resonance).
        "analog_drift_cents": 3.0,
        "filter_drift_cents": 2.0,
        "filter_wobble_amount": 0.0,
        "osc1_indep_cutoff": 20000,
        "osc2_indep_cutoff": 20000,
        "reverb_type": "wash",
        "reverb_damp": 0.50,
        "reverb_shimmer_fb": 0.0,
        "reverb_noise_mod": 0.0,
        # Per-OSC reverb send (0..1). Default 1.0 matches the legacy pad sound
        # exactly (fast-path copy). fx_bypass forces the send to 0 regardless
        # of the slider, so checking the box is a one-tap "dry only" move.
        "osc1_reverb_send": 1.0,
        "osc2_reverb_send": 1.0,
        "osc1_fx_bypass": False,
        "osc2_fx_bypass": False,
        # Per-OSC LFO receive routing. Default true = both OSCs receive both
        # LFOs (legacy behaviour). Affects amp/pan targets only — filter LFO
        # always applies to both OSCs (shared filter constraint).
        "osc1_recv_lfo1": True,
        "osc1_recv_lfo2": True,
        "osc2_recv_lfo1": True,
        "osc2_recv_lfo2": True,
        "reverb_dry_wet": 0.45,
        "reverb_wet_gain": 1.0,
        "reverb_decay_seconds": 6.0,
        "reverb_low_cut": 80,
        "reverb_high_cut": 7000,
        "reverb_space": 0.0,
        "reverb_predelay_ms": 25.0,
        "reverb_filter_enabled": False,
        "shimmer_enabled": False,
        "shimmer_mix": 0.5,
        "shimmer_high": False,
        "shimmer_send": 1.0,
        "lfo_rate_hz": 1.0,
        "lfo_rate_mode": "FREE",
        "lfo_rate_multiplier": 1.0,
        "lfo_depth": 0.0,
        "lfo_shape": "sine",
        "lfo_target": "filter",
        "lfo_spread": 0.0,
        "lfo_key_sync": False,
        "lfo_invert": False,
        "lfo_offset_ms": 0.0,
        "lfo_haas_compensate": False,
        "lfo_smooth": 0.0,  # 0..1 — one-pole LP on LFO mod, kills sidebands at high rate
        "lfo_poly": False,  # per-voice phase via Faust (AMP target only)
        "lfo2_rate_hz": 1.0,
        "lfo2_rate_mode": "FREE",
        "lfo2_rate_multiplier": 1.0,
        "lfo2_depth": 0.0,
        "lfo2_shape": "sine",
        "lfo2_target": "pan",
        "lfo2_spread": 0.0,
        "lfo2_key_sync": False,
        "lfo2_invert": False,
        "lfo2_offset_ms": 0.0,
        "lfo2_haas_compensate": False,
        "lfo2_smooth": 0.0,
        "lfo2_poly": False,
        "lfo_link": False,
        "delay_enabled": False,
        "delay_time_mode": "1/4",
        "delay_time_ms": 375.0,
        "delay_offset_ms": 0.0,
        "delay_feedback": 0.35,
        "delay_oblivion": False,
        "delay_rate_multiplier": 1.0,
        "delay_wet": 0.0,
        "delay_low_cut_hz": 20.0,
        "delay_high_cut_hz": 18000.0,
        "delay_drive": 0.0,
        "delay_width": 1.0,
        "delay_mod_rate_hz": 0.5,
        "delay_mod_depth_ms": 0.0,
        "delay_reverse_amount": 0.0,
        "delay_reverse_window_ms": 500.0,
        "delay_reverse_window_mode": "FREE",
        "delay_reverse_feedback": 0.0,
        "delay_aurora_enabled": False,
        "delay_aurora_seconds": 5.0,
        "pad_rise_seconds": 5.0,
        "pad_rise_cutoff_hz": 3000.0,
        "pad_mellow_enabled": False,
        "pad_mellow_cutoff_hz": 400.0,
        "motion_mix": 1.0,
        "freeze_enabled": False,
        "sympathetic_enabled": False,
        "sympathetic_level": 0.035,
        "drone_enabled": False,
        "drone_key": None,       # MIDI note currently held by the pad player, or null
        "drone_level": 1.0,      # pad-player volume multiplier (0..1)
        # "volume": removed 2026-04-21 — no reader anywhere. Legacy state
        # from before the OSC-blend-based volume model took over. _deep_merge
        # on load harmlessly preserves saved copies but this default is gone.
        "osc1_octave": 0,
        "osc2_octave": 0,
    },
    "piano": {
        "enabled": True,
        "soundfont": "Salamander",
        # "sound": removed 2026-04-21 — superseded by soundfont + voicing.
        # SOUNDFONT_PRESETS own the GM program number directly; the old
        # sound dropdown no longer exists in the UI and no code path reads
        # state["piano"]["sound"].
        "voicing": "acoustic",
        "eq_bands": [
            {"freq_hz": 100.0,   "gain_db": 0.0, "q": 0.8, "enabled": True},
            {"freq_hz": 1000.0,  "gain_db": 0.0, "q": 0.8, "enabled": True},
            {"freq_hz": 4000.0,  "gain_db": 0.0, "q": 0.8, "enabled": True},
            {"freq_hz": 10000.0, "gain_db": 0.0, "q": 0.7, "enabled": True},
        ],
        "filter_highcut_hz": 18000,
        "filter_lowcut_hz": 40,
        "tone_range_min": 200,
        "tone_range_max": 20000,
        "volume": 0.5,
        "reverb_dry_wet": 0.4,
        # Piano-room reverb (replaces FluidSynth's legacy internal reverb).
        # Enabled by default so the piano sounds "in a room" out of the box.
        "piano_room_enabled": True,
        # Velocity-aware brightness: soft chords roll off top, hard chords
        # sparkle. Off by default so existing presets sound unchanged.
        "vel_bright_enabled": False,
        "vel_bright_amount": 0.5,
        "comp_enabled": False,
        "comp_threshold_db": -20,
        "comp_ratio": 3.0,
        "comp_makeup_db": 0,
        "comp_knee_db": 18.0,
        "comp_drive_db": 0.0,
        "comp_wet": 1.0,
        "comp_attack_ms": 10.0,
        "comp_release_ms": 80.0,
    },
    "organ": {
        "enabled": False,
        "preset": "mellow",
        "drawbars": [8, 0, 6, 4, 0, 0, 0, 0, 0],
        "leslie_speed": "slow",
        "leslie_depth": 0.3,
        "click_enabled": True,
        "click_level": 0.3,
        "attack_ms": 0.0,
        "release_ms": 10.0,
        "drive": 0.05,
        "filter_highcut_hz": 8000,
        "filter_lowcut_hz": 40,
        "volume": 0.5,
        "shared_filter_enabled": False,
        "tone_tilt": 0.5,
        "width": 0.7,
    },
    "master": {
        "volume": 0.85,
        "transpose_semitones": 0,
        "piano_octave": 0,
        "instrument_mode": "piano",
        "eq_bands": [
            {"freq_hz": 200, "gain_db": 0.0, "q": 1.5},
            {"freq_hz": 1000, "gain_db": 0.0, "q": 1.5},
            {"freq_hz": 5000, "gain_db": 0.0, "q": 1.5},
        ],
        "eq_lowcut_enabled": False,
        "eq_lowcut_hz": 80,
        "eq_lowcut_slope": 12,
        "pre_limiter_trim": 2.0,
        "saturation_enabled": False,
        "bpm": 120,
        # ── Bus compressor (SSL G-style) ──
        "bus_comp_enabled": False,
        "bus_comp_source": "self",       # self | piano | lfo | bpm
        "bus_comp_threshold_db": -10.0,
        "bus_comp_ratio": 4.0,
        "bus_comp_attack_ms": 3.0,
        "bus_comp_release_ms": 300.0,
        "bus_comp_release_auto": True,
        "bus_comp_makeup_db": 0.0,
        "bus_comp_mix": 1.0,
        "bus_comp_fx_bypass": False,
        "bus_comp_retrigger": False,
        "bus_comp_sc_hpf_hz": 100.0,
        # Reverb send from piano/organ bus into the pad reverb (0..1).
        # Default 0 = piano/organ stays dry (backward-compatible). Raising
        # routes a copy into whichever reverb type is active, so BLOOM on
        # piano or PLATE on organ is a one-knob choice.
        "piano_reverb_send": 0.0,
        "piano_delay_send": 0.0,
        # Latency mode: False = Normal (ring 16/8, ~43ms render-ahead),
        # True = Low Latency (ring 6/3, ~16ms render-ahead). Toggle from Global tab.
        "low_latency_mode": False,
    },
    "midi_cc_map": {},
    "macros": [
        {"name": "M1", "value": 0.0, "bipolar": False, "assignments": []},
        {"name": "M2", "value": 0.0, "bipolar": False, "assignments": []},
        {"name": "M3", "value": 0.0, "bipolar": False, "assignments": []},
        {"name": "M4", "value": 0.0, "bipolar": False, "assignments": []},
        {"name": "M5", "value": 0.0, "bipolar": False, "assignments": []},
        {"name": "M6", "value": 0.0, "bipolar": False, "assignments": []},
        {"name": "M7", "value": 0.0, "bipolar": False, "assignments": []},
        {"name": "M8", "value": 0.0, "bipolar": False, "assignments": []},
    ],
    "setlists": [
        {"name": "", "presets": None} for _ in range(10)
    ],
    "ui": {
        "preset_saved": [False] * 10,
        "preset_labels": [""] * 10,
        "preset_colors": [
            "#00D4AA",
            "#FFB020",
            "#B06EFF",
            "#FF4D6A",
            "#4D9EFF",
        ],
    },
}


def ensure_dirs():
    """Create config and data directories if they don't exist."""
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    SOUNDFONT_DIR.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge override into base. New keys in base are preserved."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_state():
    """Load current state from disk, merged with defaults so new keys exist."""
    defaults = json.loads(json.dumps(DEFAULT_STATE))
    state = defaults
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
            # Migration: legacy preset stored a single ADSR under
            # synth_pad.adsr; per-OSC ADSR shipped in 5278d8e splits this
            # into adsr_osc1 / adsr_osc2. Splat the legacy block to BOTH
            # before deep-merge so a saved attack/release survives upgrade.
            sp = saved.get("synth_pad")
            if isinstance(sp, dict) and "adsr" in sp and isinstance(sp["adsr"], dict):
                if "adsr_osc1" not in sp:
                    sp["adsr_osc1"] = dict(sp["adsr"])
                if "adsr_osc2" not in sp:
                    sp["adsr_osc2"] = dict(sp["adsr"])
                sp.pop("adsr", None)
            state = _deep_merge(defaults, saved)
        except (json.JSONDecodeError, OSError) as e:
            # Previously silent-swallowed — which meant a corrupted state
            # file silently reset every preset/fader to defaults with no
            # indication to the user. Log loudly so a stage-side surprise
            # is at least findable in the journal.
            logger.warning(
                "Failed to load saved state from %s: %s — falling back to defaults",
                STATE_FILE, e,
            )

    # Migration: pad preset arrays if they're shorter than current default
    # (handles users upgrading from 5-slot → 10-slot layout).
    ui = state.setdefault("ui", {})
    for key, filler in (("preset_saved", False), ("preset_labels", "")):
        arr = ui.get(key, [])
        if not isinstance(arr, list):
            arr = []
        if len(arr) < 10:
            arr = list(arr) + [filler] * (10 - len(arr))
            ui[key] = arr

    # Migration: piano voicings were renamed 2026-04-20 to short single-word
    # keys ("acoustic_grand" → "acoustic", etc). Also the old electric_piano_*
    # entries are gone — those are Sound-dropdown concerns, not voicings.
    _VOICING_RENAME = {
        "acoustic_grand":   "acoustic",
        "bright_studio":    "bright",
        "mellow_warm":      "mellow",
        "electric_piano_1": "acoustic",
        "electric_piano_2": "acoustic",
    }
    _VALID_VOICINGS = {"acoustic", "bright", "mellow", "warm", "dark", "vintage", "stage"}
    # Migration: soundfont names moved from raw file stems to preset keys
    # 2026-04-20. "FluidR3_GM" saved state → "Fluid" preset, etc.
    _SOUNDFONT_RENAME = {
        "FluidR3_GM": "Fluid",
        "TimGM6mb":   "Fluid",   # TimGM6mb removed entirely; Fluid is the closest GM bank
        "Arachno":    "Fluid",
        "system":     "Fluid",
        "default-GM": "Fluid",
    }
    _VALID_SOUNDFONTS = {"Salamander", "Fluid", "Rhodes", "Suitcase"}
    piano_state = state.get("piano")
    if isinstance(piano_state, dict):
        v = piano_state.get("voicing")
        if v in _VOICING_RENAME:
            piano_state["voicing"] = _VOICING_RENAME[v]
        elif v not in _VALID_VOICINGS:
            piano_state["voicing"] = "acoustic"
        sf = piano_state.get("soundfont")
        if sf in _SOUNDFONT_RENAME:
            piano_state["soundfont"] = _SOUNDFONT_RENAME[sf]
        elif sf not in _VALID_SOUNDFONTS:
            piano_state["soundfont"] = "Salamander"

    # Migration: extend macros array to 8 if saved state had fewer (handles
    # upgrading from the original 4-macro layout to the 4+4 A/B-layer design).
    macros = state.get("macros", [])
    if not isinstance(macros, list):
        macros = []
    if len(macros) < 8:
        for idx in range(len(macros), 8):
            macros.append({"name": f"M{idx+1}", "value": 0.0, "bipolar": False, "assignments": []})
        state["macros"] = macros
    for m in macros:
        if isinstance(m, dict) and "bipolar" not in m:
            m["bipolar"] = False
    # Migration: add setlists array if missing or short
    setlists = state.get("setlists", [])
    if not isinstance(setlists, list):
        setlists = []
    if len(setlists) < 10:
        for _ in range(10 - len(setlists)):
            setlists.append({"name": "", "presets": None})
        state["setlists"] = setlists
    return state


def save_state(state):
    """Save current state to disk atomically (temp + fsync + rename)."""
    ensure_dirs()
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    # Best-effort cleanup of any orphan .tmp left by a prior mid-write crash.
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)
