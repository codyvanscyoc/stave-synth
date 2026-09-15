#!/usr/bin/env python3
"""One bounded, isolated instrument/Leslie/freeze/transpose/split capture.

Only the exact loopback audition runtime is allowed. No recorder, sample,
preset, setlist, macro-library, routing, buffer, or reverb-type changes occur.
Instrument changes and the terminal panic are intentionally not gapless.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

import websockets

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.run_audition import (
    CommandSocket, DURATION_SECONDS, INSTANCE, WS_URL, _write_json_exclusive,
    counter_growth, health_failures, render_schedule, run_driver, setting,
    verify_runtime, verify_state,
)
from tools.run_feature_probe import Probe, require, validate_capture


WORK_TIMEOUT = 90.0
CLEANUP_TIMEOUT = 25.0
CASE = "performance-controls"


def build_schedule():
    events = []
    for onset in (2.0, 7.0, 12.0, 17.0, 22.0, 27.0, 32.0, 37.0, 42.0):
        events.append((onset, 0xB0, 64, 127))
        # Both sides of middle C are exercised; splits use these RAW keys.
        for index, note in enumerate((48, 55, 60, 64, 67)):
            events.append((onset, 0x90, note, 66 if index == 0 else 72))
            events.append((onset + 3.8, 0x80, note, 0))
        events.append((onset + 3.95, 0xB0, 64, 0))
    events.extend(((47.0, 0xB0, 66, 0), (47.0, 0xB0, 64, 0), (47.0, 0xB0, 123, 0)))
    return sorted(events, key=lambda event: event[0])


def setup_commands():
    commands = [setting(section, name, value) for section, name, value in (
        ("master", "instrument_mode", "piano"), ("master", "split_enabled", False),
        ("master", "transpose_semitones", 0), ("master", "piano_octave", 0),
        ("master", "volume", 0.75), ("piano", "enabled", True),
        ("piano", "soundfont", "Fluid"), ("piano", "volume", 0.7),
        ("organ", "enabled", True), ("organ", "volume", 0.5),
        ("organ", "leslie_speed", "slow"), ("organ", "leslie_depth", 0.3),
        ("synth_pad", "osc1_blend", 0.35), ("synth_pad", "osc2_blend", 0.25),
        ("synth_pad", "osc1_waveform", "sine"), ("synth_pad", "osc2_waveform", "triangle"),
        ("synth_pad", "osc1_filter_enabled", True), ("synth_pad", "osc2_filter_enabled", True),
        ("synth_pad", "filter_cutoff_hz", 3500), ("synth_pad", "reverb_dry_wet", 0.35),
        ("synth_pad", "osc1_reverb_send", 1.0), ("synth_pad", "osc2_reverb_send", 1.0),
        ("synth_pad", "osc1_octave", 0), ("synth_pad", "osc2_octave", 0),
        ("synth_pad", "shimmer_high", False),
        ("master", "instrument_split_low", 60), ("master", "instrument_split_high", 127),
        ("master", "instrument_split_xfade", 0),
    )]
    for source in ("osc1", "osc2", "shimmer"):
        for name, value in (("low", 0), ("high", 59), ("xfade", 0)):
            commands.append(setting("synth_pad", f"{source}_split_{name}", value))
    commands.append(setting("master", "split_octave_snapshot", {
        "osc1_octave": 0, "osc2_octave": 0, "piano_octave": 0, "shimmer_high": False}))
    return commands


def checked_original_value(original, command):
    section, name = command["section"], command["param"]
    require(isinstance(original.get(section), dict) and name in original[section],
            f"cannot restore missing original field {section}.{name}")
    value = original[section][name]
    template = command["value"]
    if name == "split_octave_snapshot":
        require(value is None or isinstance(value, dict), "invalid original split snapshot")
        if isinstance(value, dict):
            require(set(value) <= {"osc1_octave", "osc2_octave", "piano_octave", "shimmer_high"},
                    "unknown split-snapshot fields cannot be restored")
            for key, item in value.items():
                require(type(item) is bool if key == "shimmer_high" else type(item) is int and -3 <= item <= 3,
                        "invalid original split snapshot value")
    elif type(template) is bool:
        require(type(value) is bool, f"original {section}.{name} must be boolean")
    elif type(template) in (int, float):
        require(type(value) in (int, float) and math.isfinite(value), f"original {section}.{name} must be finite")
        if "octave" in name:
            require(type(value) is int and -3 <= value <= 3, "original octave is not restorable")
        elif name == "transpose_semitones":
            require(type(value) is int and -12 <= value <= 12, "original transpose exceeds shared handler range")
        elif "_split_" in name:
            require(type(value) is int and 0 <= value <= (24 if name.endswith("xfade") else 127),
                    "original split range is not restorable")
        else:
            require(20 <= value <= 20000 if name == "filter_cutoff_hz" else 0 <= value <= 1,
                    f"original {section}.{name} is outside its restorable range")
    else:
        choices = {"instrument_mode": {"piano", "organ", "off"},
                   "soundfont": {"Fluid", "Rhodes", "Suitcase"},
                   "leslie_speed": {"stop", "slow", "fast"},
                   "osc1_waveform": {"sine", "square", "saw", "triangle", "saturated"},
                   "osc2_waveform": {"sine", "square", "saw", "triangle", "saturated"}}
        require(isinstance(value, str) and value in choices[name], f"unknown original {section}.{name}")
    return copy.deepcopy(value)


def restoration_plan(original):
    commands = setup_commands() + [setting("synth_pad", "freeze_enabled", False)]
    result = [setting(command["section"], command["param"], checked_original_value(original, command))
              for command in commands]
    # Mode changes touch runtime enables; split changes swap four stored fields.
    # Apply those first, reassert ordinary controls, then exact stash/enables.
    def priority(command):
        key = command["section"], command["param"]
        if key == ("master", "instrument_mode"):
            return 0
        if key == ("master", "split_enabled"):
            return 1
        if key == ("master", "split_octave_snapshot"):
            return 3
        if key in (("piano", "enabled"), ("organ", "enabled")):
            return 4
        return 2
    return sorted(result, key=priority)


def validate_idle(response):
    failures = health_failures(response)
    require(not failures, "audition health failed: " + "; ".join(failures))
    require(response.get("faded_out") is False and response.get("drone_faded_out") is False,
            "baseline fade state is active or unknown")
    state = response.get("state", {})
    synth, master = state.get("synth_pad", {}), state.get("master", {})
    require(synth.get("freeze_enabled") is False and synth.get("drone_enabled") is False
            and synth.get("drone_key") is None, "freeze/bed is not idle")
    require(master.get("instrument_mode") == "piano" and state.get("piano", {}).get("enabled") is True
            and state.get("organ", {}).get("enabled") is False, "baseline is not the known piano/organ enable configuration")
    require(state["piano"].get("soundfont") == "Fluid" and synth.get("reverb_type") == "wash",
            "baseline is not the expected Fluid/wash profile")
    require(isinstance(response.get("soundfonts_available"), list)
            and {"Fluid", "Rhodes"} <= set(response["soundfonts_available"]),
            "required Fluid/Rhodes presets are not reported installed")
    recorder = response.get("record_status", {})
    require(recorder.get("recording") is False and recorder.get("writer_pending") is False,
            "recorder is active or its status is unknown")
    audio = response["health"]["audio"]
    require(audio.get("sample_rate") == 48000 and audio.get("block_frames") == 512,
            "probe requires the existing 48 kHz / 512-frame graph")
    json.dumps(state, allow_nan=False)


def validate_inactive_slots(response):
    slots = response.get("slots")
    require(isinstance(slots, list) and len(slots) == 12
            and all(isinstance(slot, dict) and type(slot.get("note")) is int for slot in slots)
            and sorted(slot["note"] for slot in slots) == list(range(60, 72)), "pad inventory is malformed")
    require(all(slot.get("active") is False and slot.get("preparing") is False and not slot.get("error")
                for slot in slots), "a pad is active/preparing or reports an error")


class PerformanceProbe(Probe):
    async def at(self, seconds, *, latest=None):
        await super().at(seconds, latest=latest)
        self.report.setdefault("event_anchors", []).append({
            "planned_seconds": seconds, "latest_seconds": latest,
            "reached_monotonic_ns": time.monotonic_ns()})

    async def change(self, label, section, param, value):
        return await self.command(label, setting(section, param, value))

    async def checkpoint(self, label, expected):
        reply = await self.state(label, faded_out=False, drone_on=False)
        require(not verify_state(reply, expected), f"{label}: acknowledged controls did not reach state")
        return reply

    async def automate(self, ws, case, anchor):
        timeline_start = len(self.report["timeline"])
        self.anchor = anchor
        self.report["automation_anchor_monotonic_ns"] = round(anchor * 1_000_000_000)
        self.report["automation_anchor_kind"] = "stdout_receive_loop_monotonic_not_exact_audio_frame"
        for seconds, label, section, param, value in (
                (4, "held_Fluid_to_Rhodes", "piano", "soundfont", "Rhodes"),
                (9, "held_Rhodes_to_Fluid", "piano", "soundfont", "Fluid"),
                (11, "intentional_piano_to_organ", "master", "instrument_mode", "organ"),
                (13, "Leslie_fast", "organ", "leslie_speed", "fast"),
                (15, "Leslie_stop", "organ", "leslie_speed", "stop"),
                (20, "Leslie_slow", "organ", "leslie_speed", "slow"),
                (21, "intentional_organ_to_piano", "master", "instrument_mode", "piano")):
            await self.at(seconds, latest=seconds + 0.5)
            await self.change(label, section, param, value)
            await self.checkpoint(label + "_state", [setting(section, param, value)])
        await self.at(23, latest=23.5)
        reply = await self.command("held_transpose_plus_two", {"type": "transpose", "semitones": 2}, "transpose_ack")
        require(reply.get("semitones") == 2, "transpose acknowledgement was not +2")
        await self.checkpoint("transposed_state", [setting("master", "transpose_semitones", 2)])
        await self.at(26, latest=26.5)
        await self.change("split_on_raw_keyboard_ranges", "master", "split_enabled", True)
        expected = [command for command in self.report["setup_commands"]
                    if "_split_" in command["param"] or "octave" in command["param"]
                    or command["param"] == "shimmer_high"]
        expected += [setting("master", "split_enabled", True), setting("master", "transpose_semitones", 2)]
        await self.checkpoint("split_on_state", expected)
        await self.at(31, latest=31.5)
        await self.change("split_off", "master", "split_enabled", False)
        reply = await self.command("transpose_back_to_zero", {"type": "transpose", "semitones": 0}, "transpose_ack")
        require(reply.get("semitones") == 0, "transpose reset acknowledgement was not zero")
        await self.checkpoint("split_off_state", [setting("master", "split_enabled", False),
                                                  setting("master", "transpose_semitones", 0)])
        await self.at(33, latest=33.5)
        reply = await self.command("freeze_capture_on", {"type": "freeze_toggle", "enabled": True}, "freeze_ack")
        require(reply.get("enabled") is True, "freeze did not enable")
        freeze_ack_ns = time.monotonic_ns()
        await self.checkpoint("freeze_on_state", [setting("synth_pad", "freeze_enabled", True)])
        await self.at(39, latest=39.5)
        require(time.monotonic_ns() - freeze_ack_ns >= 2_000_000_000, "freeze capture window was not exercised for two seconds")
        reply = await self.command("freeze_off", {"type": "freeze_toggle", "enabled": False}, "freeze_ack")
        require(reply.get("enabled") is False, "freeze did not disable")
        await self.checkpoint("freeze_off_state", [setting("synth_pad", "freeze_enabled", False)])
        await self.at(46.5, latest=47)
        self.report["pre_panic_state"] = await self.state("pre_panic_state", faded_out=False, drone_on=False)
        self.report["pre_panic_debug"] = await self.command("pre_panic_debug", {"type": "debug"})
        await self.at(48, latest=49)
        await self.command("intentional_terminal_panic", {"type": "panic"})
        await asyncio.sleep(0.2)
        after = await self.checkpoint("post_panic_state", [setting("synth_pad", "freeze_enabled", False)])
        require(after.get("faded_out") is False, "panic did not reset master fade")
        self.report["post_panic_state"] = after
        self.report["feature_sequence_complete"] = True
        return self.report["timeline"][timeline_start:]


async def cleanup(probe, restore, original):
    errors = probe.report["restore_failures"] = []
    try:
        await probe.command("cleanup_panic", {"type": "panic"})
    except Exception as exc:
        errors.append(f"panic: {type(exc).__name__}: {exc}")
    for command in restore:
        try:
            await probe.command("restore_" + command["section"] + "." + command["param"], command)
        except Exception as exc:
            errors.append(f"restore {command['section']}.{command['param']}: {type(exc).__name__}: {exc}")
    try:
        state = await probe.state("restored_state", faded_out=False, drone_on=False)
        probe.report["restored_state"] = state
        errors.extend(verify_state(state, restore))
        require(state.get("state") == original, "complete restored state differs from original snapshot")
        require(state.get("record_status", {}).get("recording") is False
                and state.get("record_status", {}).get("writer_pending") is False, "recorder is unexpectedly active/pending")
        errors.extend(health_failures(state))
    except Exception as exc:
        errors.append(f"restore verification: {type(exc).__name__}: {exc}")


async def session(driver, output_dir, report):
    report["runtime"] = await verify_runtime()
    async with websockets.connect(WS_URL, open_timeout=5, close_timeout=3,
                                  max_size=4 * 1024 * 1024, max_queue=16) as raw:
        ws = await CommandSocket(raw).start()
        probe = PerformanceProbe(report, ws)
        mutated = False
        restore = []
        original = None
        try:
            response = await probe.state("original_state")
            report["original_state"] = copy.deepcopy(response)
            _write_json_exclusive(output_dir / "00-original-state.json", response)
            validate_idle(response)
            original = copy.deepcopy(response["state"])
            restore = restoration_plan(original)
            slots = await probe.command("original_slots", {"type": "list_pad_slots"}, "pad_slots")
            validate_inactive_slots(slots)
            report["original_slots"] = slots
            report["setup_commands"], report["restore_commands"] = setup_commands(), restore
            schedule_text = render_schedule(build_schedule())
            schedule_path = output_dir / "midi_schedule.txt"
            with schedule_path.open("x", encoding="ascii") as handle:
                handle.write(schedule_text)
            report["schedule_sha256"] = hashlib.sha256(schedule_text.encode("ascii")).hexdigest()
            report["schedule_events"] = len(build_schedule())
            await verify_runtime()
            mutated = True
            await probe.command("setup_panic", {"type": "panic"})
            for command in report["setup_commands"]:
                await probe.command("setup_" + command["section"] + "." + command["param"], command)
            await asyncio.sleep(1)
            before = await probe.checkpoint("verified_setup", report["setup_commands"])
            require(not health_failures(before), "setup health failed: " + "; ".join(health_failures(before)))
            before_debug = await probe.command("before_debug", {"type": "debug"})
            report["before_state"], report["before_debug"] = before, before_debug
            report["driver"] = await run_driver(driver, schedule_path, output_dir / "performance-probe.wav",
                                                ws, CASE, automation=probe.automate)
            after = await probe.state("capture_after")
            after_debug = await probe.command("after_debug", {"type": "debug"})
            report["after_state"], report["after_debug"] = after, after_debug
            report["failures"].extend(health_failures(after))
            report["failures"].extend(counter_growth(before, after, before_debug, after_debug))
            if "pre_panic_debug" in report:
                report["counter_growth_before_intentional_panic"] = counter_growth(
                    before, report["pre_panic_state"], before_debug, report["pre_panic_debug"])
            require(report.get("feature_sequence_complete") is True, "performance sequence did not complete")
            report["capture_validation"] = validate_capture(report["driver"], output_dir / "performance-probe.wav", report["schedule_events"])
        finally:
            try:
                if mutated:
                    try:
                        await asyncio.wait_for(cleanup(probe, restore, original), CLEANUP_TIMEOUT)
                    except BaseException as exc:
                        report.setdefault("restore_failures", []).append(f"cleanup incomplete: {type(exc).__name__}: {exc}")
                        if isinstance(exc, asyncio.CancelledError):
                            raise
            finally:
                await ws.stop()


async def run(args):
    driver = Path(args.driver).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    require(driver.is_file() and os.access(driver, os.X_OK), "--driver must name an executable regular file")
    require(not output_dir.exists() and output_dir.parent.is_dir(), "--output-dir must be a fresh directory below an existing parent")
    output_dir.mkdir(mode=0o700)
    report = {"format": 1, "instance": INSTANCE, "websocket_url": WS_URL, "status": "FAIL",
              "failures": [], "timeline": [], "duration_seconds": DURATION_SECONDS,
              "piano_telemetry_required": True,
              "limits": ["No preset, setlist, macro, recording, pad-library, reverb-type, or routing writes.",
                         "Instrument-mode changes release the outgoing instrument; no gapless transition claim.",
                         "Raw counters include intentional changes/panic; none are suppressed.",
                         "Leslie acknowledgements verify app state; native 0-Hz behavior is separate checker evidence.",
                         "Finite mixed output is not per-layer spectral/note attribution or subjective quality proof.",
                         "No physical keyboard/interface, analogue latency, Safari fault, soak, or stage qualification."]}
    try:
        await asyncio.wait_for(session(driver, output_dir, report), WORK_TIMEOUT)
    except BaseException as exc:
        report["failures"].append(f"{type(exc).__name__}: {exc}")
        if not isinstance(exc, Exception):
            raise
    finally:
        if not report["failures"] and not report.get("restore_failures") and report.get("feature_sequence_complete"):
            report["status"] = "PASS"
        _write_json_exclusive(output_dir / "performance-probe.result.json", report)
    return 0 if report["status"] == "PASS" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", required=True, help="compiled exact-audition native driver")
    parser.add_argument("--output-dir", required=True, help="new non-existing evidence directory")
    try:
        return asyncio.run(run(parser.parse_args(argv)))
    except (KeyboardInterrupt, RuntimeError, OSError, TimeoutError) as exc:
        print(f"performance probe refused/failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
