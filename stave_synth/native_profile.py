"""Pure readiness checks for requested production-native audio backends."""

from __future__ import annotations

import os
from typing import Mapping


_FALSE_VALUES = {"", "0", "false", "False"}


def _enabled(environ: Mapping[str, str], name: str) -> bool:
    return environ.get(name, "0") not in _FALSE_VALUES


def _is_wrapper(instance, module_name: str, class_name: str) -> bool:
    cls = type(instance)
    return cls.__module__ == module_name and cls.__name__ == class_name


def native_profile_failures(
    *, synth, piano, organ, jack, instrument_mode: str,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return requested native paths that fell back or are unavailable.

    This function imports no engine or wrapper module and performs no I/O. The
    caller invokes it only after constructing the components and before JACK
    starts, while their backend-selection attributes are stable.
    """
    env = os.environ if environ is None else environ
    failures: list[str] = []

    reverb = getattr(synth, "reverb", None)
    if _enabled(env, "STAVE_FAUST_REVERB"):
        if not _is_wrapper(reverb, "stave_synth.faust_reverb", "FaustReverb"):
            failures.append("reverb requested but FaustReverb did not load")
        else:
            if not getattr(reverb, "plate_available", False):
                failures.append("plate reverb native backend is unavailable")
            if not getattr(reverb, "drone_available", False):
                failures.append("drone reverb native backend is unavailable")

    synth_paths = (
        ("STAVE_FAUST_PING_PONG", "_faust_ping_pong", "ping-pong delay"),
        ("STAVE_FAUST_OSC_BANK", "_faust_osc_bank", "oscillator bank"),
        ("STAVE_FAUST_SYMPATHETIC", "_faust_sympathetic", "sympathetic resonance"),
        ("STAVE_FAUST_PAD_BUS", "_faust_pad_bus", "pad bus"),
    )
    for flag, attribute, label in synth_paths:
        if _enabled(env, flag) and getattr(synth, attribute, None) is None:
            failures.append(f"{label} requested but native backend did not load")

    if _enabled(env, "STAVE_FAUST_MERGED"):
        if getattr(synth, "_faust_merged", None) is None:
            failures.append("merged render path requested but native shim did not load")
        if (getattr(synth, "_faust_osc_bank", None) is None
                or getattr(synth, "_faust_pad_bus", None) is None):
            failures.append("merged render path is missing its osc-bank/pad-bus prerequisite")

    for flag, attribute, label in (
        ("STAVE_FAUST_MASTER_FX", "_faust_master_fx", "master FX"),
        ("STAVE_FAUST_BUS_COMP", "_faust_bus_comp", "bus compressor"),
    ):
        if _enabled(env, flag) and getattr(jack, attribute, None) is None:
            failures.append(f"{label} requested but native backend did not load")

    if _enabled(env, "STAVE_FAUST_ORGAN") and not _is_wrapper(
            organ, "stave_synth.faust_organ", "FaustOrganEngine"):
        failures.append("organ requested but FaustOrganEngine did not load")

    if instrument_mode == "piano" and piano is None:
        failures.append("piano mode is enabled but FluidSynth is unavailable")
    elif instrument_mode == "piano":
        if getattr(piano, "fs", None) is None:
            failures.append("piano mode is enabled but FluidSynth is not loaded")
        sfid = getattr(piano, "sfid", None)
        if (isinstance(sfid, bool) or not isinstance(sfid, int) or sfid < 0):
            failures.append("piano mode is enabled but no valid soundfont is selected")
    if _enabled(env, "STAVE_FAUST_PIANO_CHAIN") and (
            piano is None or getattr(piano, "_faust_chain", None) is None):
        failures.append("piano chain requested but native backend did not load")
    if piano is not None and getattr(piano, "_piano_room", None) is None:
        failures.append("piano room native backend is unavailable")

    return tuple(failures)


def require_native_profile(
    *, synth, piano, organ, jack, instrument_mode: str,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Fail closed when STAVE_REQUIRE_NATIVE requests production readiness."""
    env = os.environ if environ is None else environ
    failures = native_profile_failures(
        synth=synth, piano=piano, organ=organ, jack=jack,
        instrument_mode=instrument_mode, environ=env,
    )
    if _enabled(env, "STAVE_REQUIRE_NATIVE") and failures:
        raise RuntimeError("Native audio readiness failed: " + "; ".join(failures))
    return failures
