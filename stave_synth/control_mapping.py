"""Pure control-message expansion shared by browser and MIDI macro gestures.

Macro assignments intentionally store the raw HTML range value.  Converting
that value here preserves existing presets while making the audio process the
only owner of macro fanout.
"""

from __future__ import annotations

import math


_SECTIONS = frozenset({"synth_pad", "piano", "organ", "master"})
_SELECT_PARAMS = frozenset({
    "soundfont", "voicing", "preset", "leslie_speed", "reverb_type",
    "haas_delay_ms", "filter_slope", "lfo_rate_mode", "lfo2_rate_mode",
    "lfo_rate_multiplier", "lfo2_rate_multiplier", "lfo_shape", "lfo2_shape",
    "lfo_target", "lfo2_target", "bus_comp_source",
    "delay_rate_multiplier", "delay_reverse_window_mode", "delay_time_mode",
    "eq_lowcut_slope", "osc1_waveform", "osc2_waveform",
})

_PERCENT_PARAMS = frozenset({
    "reverb_dry_wet", "shimmer_mix", "reverb_space", "osc1_max", "osc2_max",
    "leslie_depth", "click_level", "drive", "width", "tone_tilt",
    "reverb_damp", "reverb_shimmer_fb", "reverb_noise_mod",
    "piano_reverb_send", "piano_delay_send", "comp_wet",
    "osc1_reverb_send", "osc2_reverb_send", "filter_wobble_amount",
    "vel_bright_amount", "bus_comp_mix", "lfo_depth", "lfo_spread",
    "lfo2_depth", "lfo2_spread", "lfo_smooth", "lfo2_smooth",
    "delay_wet", "delay_feedback", "delay_drive", "delay_width",
    "delay_reverse_amount", "delay_reverse_feedback",
})
_EQ_GAIN_PARAMS = frozenset({
    "eq_low_gain", "eq_mid_gain", "eq_high_gain", "eq_band0_gain",
    "eq_band1_gain", "eq_band2_gain", "eq_band3_gain",
})
_FREQUENCY_RANGES = {
    ("synth_pad", "osc1_indep_cutoff"): (20.0, 20000.0),
    ("synth_pad", "osc2_indep_cutoff"): (20.0, 20000.0),
    ("synth_pad", "filter_range_min"): (20.0, 2000.0),
    ("synth_pad", "filter_range_max"): (1000.0, 20000.0),
    ("piano", "filter_lowcut_hz"): (20.0, 2000.0),
    ("piano", "filter_highcut_hz"): (200.0, 20000.0),
    ("piano", "tone_range_min"): (100.0, 2000.0),
    ("piano", "tone_range_max"): (1000.0, 20000.0),
    ("organ", "filter_lowcut_hz"): (20.0, 500.0),
    ("organ", "filter_highcut_hz"): (200.0, 12000.0),
    ("synth_pad", "reverb_low_cut"): (20.0, 500.0),
    ("synth_pad", "reverb_high_cut"): (1000.0, 16000.0),
    ("synth_pad", "delay_low_cut_hz"): (20.0, 1000.0),
    ("synth_pad", "delay_high_cut_hz"): (500.0, 20000.0),
    ("synth_pad", "pad_mellow_cutoff_hz"): (100.0, 8000.0),
    ("synth_pad", "pad_rise_cutoff_hz"): (500.0, 12000.0),
    ("master", "bus_comp_sc_hpf_hz"): (20.0, 500.0),
    ("master", "eq_lowcut_hz"): (20.0, 500.0),
    ("master", "eq_low_freq"): (60.0, 500.0),
    ("master", "eq_mid_freq"): (300.0, 5000.0),
    ("master", "eq_high_freq"): (2000.0, 16000.0),
}
for _band in range(4):
    _FREQUENCY_RANGES[("piano", f"eq_band{_band}_freq")] = (20.0, 20000.0)

_OSC_SLIDER_TWINS = {
    "osc1_reverb_send": "osc2_reverb_send",
    "osc2_reverb_send": "osc1_reverb_send",
    "osc1_indep_cutoff": "osc2_indep_cutoff",
    "osc2_indep_cutoff": "osc1_indep_cutoff",
}
_OSC_BOOL_TWINS = {
    "osc1_fx_bypass": "osc2_fx_bypass", "osc2_fx_bypass": "osc1_fx_bypass",
    "osc1_recv_lfo1": "osc2_recv_lfo1", "osc2_recv_lfo1": "osc1_recv_lfo1",
    "osc1_recv_lfo2": "osc2_recv_lfo2", "osc2_recv_lfo2": "osc1_recv_lfo2",
    "osc1_filter_enabled": "osc2_filter_enabled",
    "osc2_filter_enabled": "osc1_filter_enabled",
}
_LFO_SLIDER_TWINS = {
    "lfo_rate_hz": "lfo2_rate_hz", "lfo2_rate_hz": "lfo_rate_hz",
    "lfo_depth": "lfo2_depth", "lfo2_depth": "lfo_depth",
    "lfo_spread": "lfo2_spread", "lfo2_spread": "lfo_spread",
    "lfo_smooth": "lfo2_smooth", "lfo2_smooth": "lfo_smooth",
    "lfo_offset_ms": "lfo2_offset_ms", "lfo2_offset_ms": "lfo_offset_ms",
}
_LFO_BOOL_TWINS = {
    "lfo_key_sync": "lfo2_key_sync", "lfo2_key_sync": "lfo_key_sync",
    "lfo_invert": "lfo2_invert", "lfo2_invert": "lfo_invert",
    "lfo_poly": "lfo2_poly", "lfo2_poly": "lfo_poly",
    "lfo_haas_compensate": "lfo2_haas_compensate",
    "lfo2_haas_compensate": "lfo_haas_compensate",
}


def _finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _raw_slider_to_setting(section: str, param: str, raw: float):
    if param in ("osc1_pan", "osc2_pan"):
        if -3.0 <= raw <= 3.0:
            raw = 0.0
        return raw / 100.0
    if param in _PERCENT_PARAMS or param in ("reverb_wet_gain", "pre_limiter_trim", "shimmer_send"):
        return raw / 100.0
    if param in ("lfo_rate_hz", "lfo2_rate_hz", "delay_mod_rate_hz"):
        return raw / 100.0
    if param == "delay_mod_depth_ms":
        return raw / 10.0
    if param in ("lfo_offset_ms", "lfo2_offset_ms") and -3.0 <= raw <= 3.0:
        return 0.0
    if param == "sympathetic_level":
        return (raw / 1000.0) ** 3 * 0.15
    if param == "unison_detune":
        return raw / 1000.0
    if param == "reverb_decay_seconds":
        return raw / 10.0
    if param in _EQ_GAIN_PARAMS:
        return raw / 10.0
    if param == "bus_comp_ratio" and raw >= 25.0:
        return 1000.0
    hz_range = _FREQUENCY_RANGES.get((section, param))
    if hz_range:
        minimum, maximum = hz_range
        # JavaScript Math.round is floor(x + .5) for these positive values.
        return math.floor(minimum * ((maximum / minimum) ** (raw / 1000.0)) + 0.5)
    return raw


def macro_commands(macro, value, *, link_state=None):
    """Expand a macro gesture into canonical-shaped fader/setting messages.

    Invalid assignments and legacy select targets are ignored. ``link_state``
    is optional so callers can preserve the browser's OSC/LFO linked-slider
    mirroring without introducing a dependency on application state.
    """
    if not isinstance(macro, dict):
        return []
    normalized = _finite_number(value)
    if normalized is None:
        return []
    normalized = max(0.0, min(1.0, normalized))
    bipolar = bool(macro.get("bipolar", False))
    links = link_state if isinstance(link_state, dict) else {}
    commands = []

    for assignment in macro.get("assignments", []):
        if not isinstance(assignment, dict):
            continue
        minimum = _finite_number(assignment.get("min", 0.0))
        maximum = _finite_number(assignment.get("max", 1.0))
        if minimum is None or maximum is None:
            continue
        if assignment.get("is_bool"):
            target = normalized >= 0.5
        elif bipolar:
            center = _finite_number(assignment.get("center", (minimum + maximum) * 0.5))
            if center is None:
                continue
            if normalized <= 0.5:
                target = center + (minimum - center) * (1.0 - 2.0 * normalized)
            else:
                target = center + (maximum - center) * (2.0 * normalized - 1.0)
        else:
            target = minimum + normalized * (maximum - minimum)

        if assignment.get("kind", "param") == "fader":
            fader_id = _finite_number(assignment.get("fader_id"))
            fader_alt = _finite_number(assignment.get("fader_alt", 0))
            if fader_id is None or fader_alt is None or not fader_id.is_integer() or not fader_alt.is_integer():
                continue
            if not 0 <= int(fader_id) <= 4 or not 0 <= int(fader_alt) <= 2:
                continue
            commands.append({"type": "fader", "id": int(fader_id),
                             "alt": int(fader_alt),
                             "value": max(0.0, min(1.0, float(target)))})
            continue

        if assignment.get("kind", "param") != "param":
            continue
        section = assignment.get("section")
        param = assignment.get("param")
        if section not in _SECTIONS or not isinstance(param, str) or not param or len(param) > 80:
            continue
        if param in _SELECT_PARAMS:
            continue
        converted = bool(target) if assignment.get("is_bool") else _raw_slider_to_setting(section, param, target)
        commands.append({"type": "setting", "section": section,
                         "param": param, "value": converted})

        twin = None
        if links.get("osc_levels_linked"):
            twin = _OSC_SLIDER_TWINS.get(param) or _OSC_BOOL_TWINS.get(param)
            if twin is None and param.startswith("adsr_osc1."):
                twin = "adsr_osc2." + param[len("adsr_osc1."):]
            elif twin is None and param.startswith("adsr_osc2."):
                twin = "adsr_osc1." + param[len("adsr_osc2."):]
        if twin is None and links.get("lfo_link"):
            twin = _LFO_SLIDER_TWINS.get(param) or _LFO_BOOL_TWINS.get(param)
        if twin:
            commands.append({"type": "setting", "section": "synth_pad",
                             "param": twin,
                             "value": (bool(target) if assignment.get("is_bool")
                                       else _raw_slider_to_setting("synth_pad", twin, target))})
    return commands
