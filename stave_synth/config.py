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
BUFFER_SIZE = 256
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
        "unison_detune": 0.20,
        "unison_spread": 0.85,
        "osc1_pan": 0.0,
        "osc2_pan": 0.0,
        "osc_hard_pan": False,
        "adsr": {
            "attack_ms": 200,
            "decay_ms": 1500,
            "sustain_percent": 80,
            "release_ms": 500,
        },
        "filter_cutoff_hz": 8000,
        "filter_resonance": 0.707,
        "filter_slope": 12,
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
        "shimmer_enabled": False,
        "shimmer_mix": 0.5,
        "freeze_enabled": False,
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
    "master": {
        "volume": 0.85,
        "transpose_semitones": 0,
        "piano_octave": 0,
    },
    "midi_cc_map": {},
    "ui": {
        "preset_saved": [False, False, False, False, False],
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
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
            return _deep_merge(defaults, saved)
        except (json.JSONDecodeError, OSError):
            pass
    return defaults


def save_state(state):
    """Save current state to disk."""
    ensure_dirs()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
