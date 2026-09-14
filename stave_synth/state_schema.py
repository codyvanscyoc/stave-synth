"""Finite, bounded control/state validation without importing audio engines.

Persisted legacy scenes are migrated into the current schema. Scene snapshots
never contain global preset libraries, device routing or controller mappings.
Validation must run BEFORE changing application or DSP state.
"""

from __future__ import annotations

import copy
import math
import re

from .config import DEFAULT_STATE, LOW_RAM_MODE


MAX_JSON_BYTES = 4 * 1024 * 1024
SCENE_SECTIONS = ("synth_pad", "piano", "organ", "master", "macros")
GLOBAL_MASTER_KEYS = {"audio_output_pref", "low_latency_mode", "show_macros", "pitch_bend_enabled", "midi_clock_enabled"}
DIVISIONS = {"FREE", "1/2", "1/4.", "1/4", "1/4T", "1/8.", "1/8", "1/8T", "1/16"}
RANGES: dict[tuple[str, str], tuple[float, float]] = {}


class ValidationError(ValueError):
    pass


def _ranges(section, lo, hi, names):
    for name in names.split():
        RANGES[section, name] = (lo, hi)


_ranges("synth_pad", 0, 1, "osc1_blend osc2_blend osc1_max osc2_max unison_spread osc1_reverb_send osc2_reverb_send reverb_dry_wet reverb_shimmer_fb reverb_noise_mod reverb_space shimmer_mix filter_wobble_amount delay_wet delay_drive delay_width delay_reverse_amount motion_mix drone_level")
_ranges("synth_pad", -1, 1, "osc1_pan osc2_pan")
_ranges("synth_pad", -3, 3, "osc1_octave osc2_octave")
_ranges("synth_pad", 1, 5, "unison_voices")
_ranges("synth_pad", 0, .1, "unison_detune")
_ranges("synth_pad", 5, 40, "haas_delay_ms")
_ranges("synth_pad", 20, 20000, "filter_cutoff_hz filter_range_min filter_range_max osc1_indep_cutoff osc2_indep_cutoff reverb_low_cut reverb_high_cut")
_ranges("synth_pad", .1, 10, "filter_resonance")
_ranges("synth_pad", 20, 2000, "filter_highpass_hz")
_ranges("synth_pad", 0, 20, "analog_drift_cents")
_ranges("synth_pad", 0, 40, "filter_drift_cents")
_ranges("synth_pad", 0, .99, "reverb_damp delay_feedback")
_ranges("synth_pad", .5, 3, "reverb_wet_gain")
_ranges("synth_pad", 0, 30, "reverb_decay_seconds")
_ranges("synth_pad", 0, 150, "reverb_predelay_ms")
_ranges("synth_pad", 0, 2, "shimmer_send")
_ranges("synth_pad", 1, 1000, "delay_time_ms")
_ranges("synth_pad", -200, 200, "delay_offset_ms")
_ranges("synth_pad", -10, 10, "delay_rate_multiplier")
_ranges("synth_pad", 20, 1000, "delay_low_cut_hz")
_ranges("synth_pad", 500, 20000, "delay_high_cut_hz")
_ranges("synth_pad", .05, 8, "delay_mod_rate_hz")
_ranges("synth_pad", 0, 15, "delay_mod_depth_ms")
_ranges("synth_pad", 50, 3000, "delay_reverse_window_ms")
_ranges("synth_pad", 0, .7, "delay_reverse_feedback")
_ranges("synth_pad", 3, 15, "delay_aurora_seconds")
_ranges("synth_pad", 0, 60, "pad_rise_seconds")
_ranges("synth_pad", 100, 20000, "pad_rise_cutoff_hz")
_ranges("synth_pad", 100, 8000, "pad_mellow_cutoff_hz")
_ranges("synth_pad", 0, .15, "sympathetic_level")
for prefix in ("lfo", "lfo2"):
    _ranges("synth_pad", .05, 20, f"{prefix}_rate_hz")
    _ranges("synth_pad", .1, 10, f"{prefix}_rate_multiplier")
    _ranges("synth_pad", 0, 1, f"{prefix}_depth {prefix}_spread {prefix}_smooth")
    _ranges("synth_pad", -500, 500, f"{prefix}_offset_ms")
for section, prefixes in (("synth_pad", ("osc1", "osc2", "shimmer")), ("master", ("instrument",))):
    for prefix in prefixes:
        _ranges(section, 0, 127, f"{prefix}_split_low {prefix}_split_high")
        _ranges(section, 0, 24, f"{prefix}_split_xfade")
_ranges("piano", 0, 1, "volume reverb_dry_wet vel_bright_amount comp_wet piano_room_size")
_ranges("piano", 0, .99, "piano_room_damp")
_ranges("piano", 20, 20000, "filter_highcut_hz filter_lowcut_hz tone_range_min tone_range_max")
_ranges("piano", 20, 2000, "filter_lowcut_hz")
_ranges("piano", -40, 0, "comp_threshold_db")
_ranges("piano", 1, 20, "comp_ratio")
_ranges("piano", 0, 24, "comp_makeup_db comp_knee_db")
_ranges("piano", -12, 12, "comp_drive_db")
_ranges("piano", .5, 200, "comp_attack_ms")
_ranges("piano", 5, 2000, "comp_release_ms")
_ranges("organ", 0, 1, "volume leslie_depth click_level drive tone_tilt width")
_ranges("organ", 0, 1000, "attack_ms release_ms")
_ranges("organ", 20, 20000, "filter_highcut_hz filter_lowcut_hz")
_ranges("organ", 20, 500, "filter_lowcut_hz")
_ranges("organ", 200, 12000, "filter_highcut_hz")
_ranges("organ", 5, 1000, "release_ms")
_ranges("master", 0, 1, "volume piano_reverb_send piano_delay_send bus_comp_mix")
_ranges("master", -24, 24, "transpose_semitones")
_ranges("master", -3, 3, "piano_octave")
_ranges("master", .5, 3, "pre_limiter_trim")
_ranges("master", 40, 300, "bpm")
_ranges("master", 20, 2000, "eq_lowcut_hz")
_ranges("master", -40, 0, "bus_comp_threshold_db")
_ranges("master", 1, 1000, "bus_comp_ratio")
_ranges("master", .1, 30, "bus_comp_attack_ms")
_ranges("master", 50, 1200, "bus_comp_release_ms")
_ranges("master", 0, 20, "bus_comp_makeup_db")
_ranges("master", 20, 500, "bus_comp_sc_hpf_hz")

ENUMS = {
    ("synth_pad", "osc1_waveform"): {"sine", "square", "saw", "triangle", "saturated"},
    ("synth_pad", "osc2_waveform"): {"sine", "square", "saw", "triangle", "saturated"},
    ("synth_pad", "filter_slope"): {12, 24},
    ("synth_pad", "reverb_type"): {"wash", "hall", "room", "plate", "bloom", "drone", "ghost"},
    ("piano", "soundfont"): {"Salamander", "Fluid", "Rhodes", "Suitcase"},
    ("piano", "voicing"): {"acoustic", "bright", "mellow", "warm", "dark", "vintage", "stage"},
    ("organ", "preset"): {"mellow", "full", "gospel", "jazz"},
    ("organ", "leslie_speed"): {"slow", "fast", "stop"},
    ("master", "instrument_mode"): {"piano", "organ", "off"},
    ("master", "eq_lowcut_slope"): {6, 12, 24},
    ("master", "bus_comp_source"): {"self", "piano", "lfo", "bpm"},
}
for prefix in ("lfo", "lfo2"):
    ENUMS["synth_pad", f"{prefix}_rate_mode"] = DIVISIONS
    ENUMS["synth_pad", f"{prefix}_shape"] = {"sine", "triangle", "square", "saw", "ramp", "peak", "sh"}
    ENUMS["synth_pad", f"{prefix}_target"] = {"filter", "amp", "pan", "bus"}
for key in ("delay_time_mode", "delay_reverse_window_mode"):
    ENUMS["synth_pad", key] = DIVISIONS
INTEGER_KEYS = {"unison_voices", "osc1_octave", "osc2_octave", "piano_octave", "transpose_semitones"}
ADSR_RANGES = {"attack_ms": (0, 10000), "decay_ms": (0, 20000), "sustain_percent": (0, 100), "release_ms": (0, 30000)}
EQ_RANGES = {"freq_hz": (20, 20000), "gain_db": (-12, 12), "q": (.1, 10)}
_PIANO_EQ = re.compile(r"eq_band(\d+)_(freq|gain|q|enabled)\Z")
_MASTER_EQ = re.compile(r"eq_band_(\d+)\Z")


def number(value, lo, hi, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValidationError("expected a finite number")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValidationError("expected a finite number") from exc
    if not math.isfinite(result):
        raise ValidationError("NaN and infinity are not allowed")
    if integer and not result.is_integer():
        raise ValidationError("expected a whole number")
    result = max(lo, min(hi, result))
    return int(result) if integer else result


def index(value, size):
    result = number(value, 0, size - 1, integer=True)
    if float(value) != result:
        raise ValidationError(f"index must be between 0 and {size - 1}")
    return result


def boolean(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    raise ValidationError("expected true or false")


def text(value, limit=128):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValidationError(f"expected text of at most {limit} characters")
    return value


def finite_json(value, *, max_depth=12, max_nodes=100000):
    """Bound already-decoded JSON before walking/migrating mutable state."""
    remaining = max_nodes
    def walk(item, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > max_depth:
            raise ValidationError("JSON structure exceeds the supported size/depth")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, (float, int)):
            if not math.isfinite(item):
                raise ValidationError("NaN and infinity are not allowed")
        elif isinstance(item, str):
            text(item, 4096)
        elif isinstance(item, dict):
            for key, child in item.items():
                text(key, 128)
                walk(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > 128:
                raise ValidationError("JSON array is too large")
            for child in item:
                walk(child, depth + 1)
        else:
            raise ValidationError("unsupported JSON value")
    try:
        walk(value, 0)
    except (OverflowError, RecursionError) as exc:
        raise ValidationError("JSON structure/number is too large") from exc


def eq_band(value, *, piano=False):
    if not isinstance(value, dict) or set(value) - (set(EQ_RANGES) | {"enabled"}):
        raise ValidationError("invalid EQ band")
    out = {}
    for key, raw in value.items():
        out[key] = boolean(raw) if key == "enabled" else number(raw, *EQ_RANGES[key])
    return out


def validate_setting(section, param, value):
    """Return canonical engine units, or raise before any mutation."""
    text(section, 16)
    text(param, 64)
    if section not in SCENE_SECTIONS[:4]:
        raise ValidationError("unknown settings section")
    if section == "synth_pad" and param.startswith(("adsr.", "adsr_osc1.", "adsr_osc2.")):
        key = param.split(".", 1)[1]
        if key not in ADSR_RANGES:
            raise ValidationError("unknown envelope parameter")
        return number(value, *ADSR_RANGES[key])
    if section == "synth_pad" and param in ("adsr", "adsr_osc1", "adsr_osc2"):
        if not isinstance(value, dict) or set(value) - set(ADSR_RANGES):
            raise ValidationError("invalid envelope")
        return {key: number(raw, *ADSR_RANGES[key]) for key, raw in value.items()}
    if param == "eq_bands" and section in ("piano", "master"):
        count = 4 if section == "piano" else 3
        if not isinstance(value, list) or len(value) != count:
            raise ValidationError(f"expected exactly {count} EQ bands")
        return [eq_band(band, piano=section == "piano") for band in value]
    match = _PIANO_EQ.fullmatch(param) if section == "piano" else None
    if match:
        index(match.group(1), 4)
        field = {"freq": "freq_hz", "gain": "gain_db", "q": "q", "enabled": "enabled"}[match.group(2)]
        return boolean(value) if field == "enabled" else number(value, *EQ_RANGES[field])
    match = _MASTER_EQ.fullmatch(param) if section == "master" else None
    if match:
        index(match.group(1), 3)
        return eq_band(value)
    if section == "master" and param in {f"eq_{band}_{kind}" for band in ("low", "mid", "high") for kind in ("freq", "gain")}:
        return number(value, *(EQ_RANGES["freq_hz"] if param.endswith("freq") else EQ_RANGES["gain_db"]))
    if section == "organ" and param == "drawbars":
        if not isinstance(value, list) or len(value) != 9:
            raise ValidationError("expected nine drawbars")
        return [number(v, 0, 8, integer=True) for v in value]
    if section == "master" and param == "split_octave_snapshot":
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) - {"osc1_octave", "osc2_octave", "piano_octave", "shimmer_high"}:
            raise ValidationError("invalid octave snapshot")
        return {k: boolean(v) if k == "shimmer_high" else number(v, -3, 3, integer=True) for k, v in value.items()}
    if section == "master" and param == "audio_output_pref":
        return None if value is None else text(value, 256)
    if section == "synth_pad" and param == "drone_key":
        return None if value is None else 60 + index(number(value, -10000, 10000, integer=True) - 60, 12)
    key = (section, param)
    if key in ENUMS:
        choices = ENUMS[key]
        if all(isinstance(v, int) for v in choices):
            result = number(value, -10000, 10000, integer=True)
        else:
            result = text(value, 32)
        if result not in choices:
            raise ValidationError(f"unsupported value for {section}.{param}")
        if LOW_RAM_MODE and key == ("piano", "soundfont") and result == "Salamander":
            return "Fluid"
        return result
    default = DEFAULT_STATE[section].get(param)
    if isinstance(default, bool):
        return boolean(value)
    if key in RANGES:
        result = number(value, *RANGES[key], integer=param in INTEGER_KEYS or "_split_" in param)
        if key == ("synth_pad", "unison_voices") and LOW_RAM_MODE:
            return 3
        if key == ("synth_pad", "delay_rate_multiplier") and result == 0:
            return 1.0
        return result
    raise ValidationError(f"unknown setting {section}.{param}")


def validate_assignment(raw):
    if not isinstance(raw, dict):
        raise ValidationError("macro assignment must be an object")
    kind = raw.get("kind", "param")
    result = {"kind": kind, "min": number(raw.get("min", 0), -30000, 30000),
              "max": number(raw.get("max", 1), -30000, 30000), "is_bool": boolean(raw.get("is_bool", False))}
    if "center" in raw:
        result["center"] = number(raw["center"], -30000, 30000)
    if kind == "fader":
        result.update(fader_id=index(raw.get("fader_id"), 5), fader_alt=index(raw.get("fader_alt", 0), 3), is_bool=False)
        result["min"] = number(result["min"], 0, 1)
        result["max"] = number(result["max"], 0, 1)
    elif kind == "param":
        section, param = text(raw.get("section"), 16), text(raw.get("param"), 64)
        validate_setting(section, param, False if result["is_bool"] else 0)
        result.update(section=section, param=param)
    else:
        raise ValidationError("unsupported macro assignment kind")
    return result


def normalize_macros(raw):
    if not isinstance(raw, list) or len(raw) > 8:
        raise ValidationError("expected at most eight macros")
    result = copy.deepcopy(DEFAULT_STATE["macros"])
    for i, macro in enumerate(raw):
        if not isinstance(macro, dict):
            raise ValidationError("macro must be an object")
        assignments = macro.get("assignments", [])
        if not isinstance(assignments, list) or len(assignments) > 32:
            raise ValidationError("macro supports at most 32 assignments")
        result[i] = {"name": text(macro.get("name", f"M{i + 1}"), 24),
                     "value": number(macro.get("value", 0), 0, 1),
                     "bipolar": boolean(macro.get("bipolar", False)),
                     "assignments": [validate_assignment(a) for a in assignments]}
    return result


def normalize_state(raw, *, scene=False):
    if not isinstance(raw, dict):
        raise ValidationError("state must be a JSON object")
    if scene:
        # Old presets embedded the entire setlist library. Ignore those global
        # branches before walking so migration cannot recursively re-embed them.
        raw = {key: raw[key] for key in SCENE_SECTIONS if key in raw}
    elif "setlists" in raw:
        # Full-state migration must make the same projection for each saved
        # scene BEFORE the bounded walk. Three historical preset/setlist saves
        # could embed enough obsolete libraries to exceed the depth limit even
        # though every retained control and the top-level library are valid.
        # Only shallow-copy bounded containers; never traverse discarded data.
        setlists = raw["setlists"]
        if not isinstance(setlists, list) or len(setlists) > 10:
            raise ValidationError("invalid setlist library")
        projected = []
        for item in setlists:
            if not isinstance(item, dict):
                raise ValidationError("setlist must be an object")
            item = dict(item)
            presets = item.get("presets")
            if presets is not None:
                if not isinstance(presets, list) or len(presets) > 10:
                    raise ValidationError("invalid setlist preset bank")
                if any(p is not None and not isinstance(p, dict) for p in presets):
                    raise ValidationError("state must be a JSON object")
                item["presets"] = [None if p is None else
                                   {key: p[key] for key in SCENE_SECTIONS if key in p}
                                   for p in presets]
            projected.append(item)
        raw = {**raw, "setlists": projected}
    finite_json(raw)
    result = copy.deepcopy(DEFAULT_STATE)
    for section in SCENE_SECTIONS[:4]:
        source = raw.get(section, {})
        if not isinstance(source, dict):
            raise ValidationError(f"{section} must be an object")
        source = copy.deepcopy(source)
        if section == "synth_pad" and isinstance(source.get("adsr"), dict):
            source.setdefault("adsr_osc1", source["adsr"])
            source.setdefault("adsr_osc2", source["adsr"])
        if section == "piano":
            renames = {"acoustic_grand": "acoustic", "bright_studio": "bright", "mellow_warm": "mellow", "electric_piano_1": "acoustic", "electric_piano_2": "acoustic"}
            if isinstance(source.get("voicing"), str):
                source["voicing"] = renames.get(source["voicing"], source["voicing"])
            if source.get("soundfont") in ("FluidR3_GM", "TimGM6mb", "Arachno", "system", "default-GM"):
                source["soundfont"] = "Fluid"
        for param, value in source.items():
            if param not in DEFAULT_STATE[section] and (section, param) not in RANGES and (section, param) != ("master", "audio_output_pref"):
                continue  # retired/unknown saved fields never become controls
            checked = validate_setting(section, param, value)
            if isinstance(checked, dict) and isinstance(result[section].get(param), dict):
                result[section][param].update(checked)
            else:
                result[section][param] = checked
        # Invalid interval order is rejected, never allowed to invert a fader.
        for low, high in (("filter_range_min", "filter_range_max"), ("tone_range_min", "tone_range_max")):
            if low in result[section] and result[section][low] > result[section][high]:
                raise ValidationError(f"{section}.{low} exceeds {high}")
    if LOW_RAM_MODE:
        result["synth_pad"]["unison_voices"] = 3
    result["macros"] = normalize_macros(raw.get("macros", []))
    if scene:
        result = {key: result[key] for key in SCENE_SECTIONS}
        for key in GLOBAL_MASTER_KEYS:
            result["master"].pop(key, None)
        # A saved scene is not an instruction to resurrect a stopped sampled bed.
        result["synth_pad"]["drone_enabled"] = False
        result["synth_pad"]["drone_key"] = None
        return result
    cc_map = raw.get("midi_cc_map", {})
    if not isinstance(cc_map, dict) or len(cc_map) > 128:
        raise ValidationError("invalid MIDI mapping")
    result["midi_cc_map"] = {str(index(key, 128)): validate_cc_target(value) for key, value in cc_map.items()}
    ui = raw.get("ui", {})
    if not isinstance(ui, dict):
        raise ValidationError("ui must be an object")
    for key, filler in (("preset_labels", ""), ("preset_saved", False)):
        items = ui.get(key, [])
        if not isinstance(items, list) or len(items) > 10:
            raise ValidationError(f"invalid {key}")
        result["ui"][key] = [text(v, 16) if key == "preset_labels" else boolean(v) for v in items] + [filler] * (10 - len(items))
    setlists = raw.get("setlists", [])
    if not isinstance(setlists, list) or len(setlists) > 10:
        raise ValidationError("invalid setlist library")
    for i, item in enumerate(setlists):
        if not isinstance(item, dict):
            raise ValidationError("setlist must be an object")
        presets = item.get("presets")
        if presets is not None and (not isinstance(presets, list) or len(presets) > 10):
            raise ValidationError("invalid setlist preset bank")
        labels = item.get("labels", [""] * 10)
        if not isinstance(labels, list) or len(labels) > 10:
            raise ValidationError("invalid setlist labels")
        result["setlists"][i] = {"name": text(item.get("name", ""), 24),
            "presets": None if presets is None else [None if p is None else normalize_state(p, scene=True) for p in presets] + [None] * (10 - len(presets)),
            "labels": [text(label, 16) for label in labels] + [""] * (10 - len(labels))}
    return result


def validate_cc_target(value):
    if not isinstance(value, dict):
        raise ValidationError("MIDI target must be an object")
    kind = value.get("kind", "fader")
    if kind == "macro":
        return {"kind": kind, "macro_idx": index(value.get("macro_idx"), 8)}
    if kind == "preset":
        return {"kind": kind, "preset_slot": index(value.get("preset_slot"), 10)}
    if kind == "fader":
        alt = value.get("alt", 0)
        return {"kind": kind, "id": index(value.get("id"), 5), "alt": index(int(alt) if isinstance(alt, bool) else alt, 3)}
    raise ValidationError("unknown MIDI target")


def filename(value):
    result = text(value, 255)
    if not result or "/" in result or "\\" in result or ".." in result or not result.lower().endswith(".wav"):
        raise ValidationError("invalid WAV filename")
    return result


def validate_message(raw):
    """Validate every public command; the dispatcher never sees arbitrary JSON."""
    if not isinstance(raw, dict):
        raise ValidationError("message must be an object")
    finite_json(raw, max_depth=6, max_nodes=4096)
    msg = copy.deepcopy(raw)
    kind = text(msg.get("type"), 40)
    no_args = {"get_state", "debug", "panic", "instrument_cycle", "midi_learn_start", "midi_learn_cancel", "get_cc_map", "get_audio_outputs", "list_recordings", "list_pad_slots", "record_toggle"}
    if kind in no_args:
        return {"type": kind}
    if kind == "setting":
        msg["value"] = validate_setting(msg.get("section"), msg.get("param"), msg.get("value"))
        if msg.get("section") == "master" and msg.get("param") == "audio_output_pref":
            raise ValidationError("select an audio output using set_audio_output")
    elif kind == "fader":
        msg["id"] = index(msg.get("id", 0), 5)
        alt = msg.get("alt", 0)
        msg["alt"] = index(int(alt) if isinstance(alt, bool) else alt, 3)
        msg["value"] = number(msg.get("value", 0), 0, 1)
    elif kind in {"preset_load", "preset_save", "preset_delete", "preset_label", "setlist_load", "setlist_save"}:
        msg["slot"] = index(msg.get("slot", 0), 10)
        if kind == "preset_label":
            msg["label"] = text(msg.get("label", ""), 16)
        if kind == "setlist_save":
            msg["name"] = text(msg.get("name", ""), 24)
    elif kind == "preset_swap":
        msg["source"] = index(msg.get("source"), 10)
        msg["target"] = index(msg.get("target"), 10)
    elif kind == "transpose":
        msg["semitones"] = number(msg.get("semitones", 0), -24, 24, integer=True)
    elif kind in {"shimmer_toggle", "shimmer_high_toggle", "freeze_toggle"}:
        if msg.get("enabled") is not None:
            msg["enabled"] = boolean(msg["enabled"])
    elif kind == "octave":
        if msg.get("instrument") not in ("osc1", "osc2", "pad", "piano"):
            raise ValidationError("invalid octave instrument")
        msg["octave"] = number(msg.get("octave", 0), -3, 3, integer=True)
    elif kind in {"fade_toggle", "drone_fade"}:
        msg["duration_s"] = number(msg.get("duration_s", 5), .02, 60)
        if msg.get("faded_out") is not None:
            msg["faded_out"] = boolean(msg["faded_out"])
    elif kind in {"drone_key", "save_to_pad_slot", "clear_pad_slot"}:
        msg["note"] = 60 + index(number(msg.get("note", 60), -10000, 10000, integer=True) - 60, 12)
        if kind == "drone_key":
            msg["rise_seconds"] = number(msg.get("rise_seconds", 0), 0, 60)
        elif kind == "save_to_pad_slot":
            msg["source"] = filename(msg.get("source"))
    elif kind in {"delete_recording", "recall_recording_params"}:
        msg["filename"] = filename(msg.get("filename"))
    elif kind == "set_audio_output":
        msg["name"] = text(msg.get("name"), 256)
        if not msg["name"]:
            raise ValidationError("an output must be selected")
    elif kind in {"bus_comp_preset", "piano_comp_preset"}:
        key = "name" if kind == "bus_comp_preset" else "preset"
        msg[key] = text(msg.get(key), 24)
    elif kind == "macro_value":
        msg["idx"] = index(msg.get("idx", 0), 8)
        msg["value"] = number(msg.get("value", 0), 0, 1)
    elif kind == "macro_assign":
        msg["idx"] = index(msg.get("idx", 0), 8)
        action = msg.get("action", "toggle")
        if action not in {"toggle", "clear", "set_bipolar"}:
            raise ValidationError("invalid macro assignment action")
        if action == "set_bipolar":
            msg["bipolar"] = boolean(msg.get("bipolar", False))
        elif action == "toggle":
            msg.update(validate_assignment(msg))
    elif kind == "midi_learn_select":
        if "macro_idx" in msg:
            msg["macro_idx"] = index(msg["macro_idx"], 8)
        elif "preset_slot" in msg:
            msg["preset_slot"] = index(msg["preset_slot"], 10)
        else:
            msg.update(validate_cc_target(msg))
    elif kind == "midi_learn_clear":
        msg["cc"] = index(msg.get("cc"), 128)
    else:
        raise ValidationError(f"unknown command: {kind}")
    return msg
