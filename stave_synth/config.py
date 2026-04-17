"""Default configuration and paths for Stave Synth."""

import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "stave-synth"
PRESETS_DIR = CONFIG_DIR / "presets"
DATA_DIR = Path.home() / ".local" / "share" / "stave-synth"
SOUNDFONT_DIR = DATA_DIR / "soundfonts"
STATE_FILE = CONFIG_DIR / "current_state.json"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Audio
SAMPLE_RATE = 48000
BIT_DEPTH = 24

# Network
WEBSOCKET_HOST = "0.0.0.0"
WEBSOCKET_PORT = 8765
HTTP_PORT = 8080

# Synth limits
MAX_SYNTH_VOICES = 16
MAX_FLUIDSYNTH_POLYPHONY = 64

# Transpose
TRANSPOSE_MIN = -12
TRANSPOSE_MAX = 12

# Auto-save interval in seconds
AUTOSAVE_INTERVAL = 30

# BTL USB audio adapter: invert right channel so headphones hear L - R.
# Set False for normal audio interfaces.
BTL_MODE = False

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
        "adsr": {
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
        "osc1_indep_cutoff": 20000,
        "osc2_indep_cutoff": 20000,
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
        "lfo_enabled": False,
        "lfo_rate_hz": 1.0,
        "lfo_depth": 0.0,
        "lfo_shape": "sine",
        "lfo_target": "filter",
        "lfo_spread": 0.0,
        "delay_enabled": False,
        "delay_time_mode": "1/4",
        "delay_time_ms": 375.0,
        "delay_offset_ms": 0.0,
        "delay_feedback": 0.35,
        "delay_wet": 0.0,
        "motion_mix": 1.0,
        "freeze_enabled": False,
        "sympathetic_enabled": False,
        "sympathetic_level": 0.035,
        "drone_enabled": False,
        "drone_key": None,       # MIDI note currently held by the pad player, or null
        "drone_level": 1.0,      # pad-player volume multiplier (0..1)
        "volume": 0.8,
        "osc1_octave": 0,
        "osc2_octave": 0,
    },
    "piano": {
        "enabled": True,
        "soundfont": "FluidR3_GM",
        "sound": "acoustic_grand_piano",
        "filter_highcut_hz": 20000,
        "filter_lowcut_hz": 20,
        "tone_range_min": 200,
        "tone_range_max": 20000,
        "volume": 0.5,
        "reverb_dry_wet": 0.4,
        "comp_enabled": False,
        "comp_threshold_db": -12,
        "comp_ratio": 3.0,
        "comp_makeup_db": 0,
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
    },
    "midi_cc_map": {},
    "macros": [
        {"name": "M1", "value": 0.0, "assignments": []},
        {"name": "M2", "value": 0.0, "assignments": []},
        {"name": "M3", "value": 0.0, "assignments": []},
        {"name": "M4", "value": 0.0, "assignments": []},
        {"name": "M5", "value": 0.0, "assignments": []},
        {"name": "M6", "value": 0.0, "assignments": []},
        {"name": "M7", "value": 0.0, "assignments": []},
        {"name": "M8", "value": 0.0, "assignments": []},
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
            state = _deep_merge(defaults, saved)
        except (json.JSONDecodeError, OSError):
            pass

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
