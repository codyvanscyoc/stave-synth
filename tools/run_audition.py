#!/usr/bin/env python3
"""Capture deterministic, isolated Stave audition takes on the Pi.

This tool deliberately has no host/port options.  It will only speak to the
``audition`` runtime on loopback and the native capture driver independently
enforces the exact JACK client/port contract.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.request import urlopen

import websockets


INSTANCE = "audition"
HTTP_URL = "http://127.0.0.1:18080/runtime-config.js"
WS_URL = "ws://127.0.0.1:18765"
WS_PORT = 18765
DURATION_SECONDS = 55.0
MUSIC_START_SECONDS = 2.0
MUSIC_END_SECONDS = 47.0
STREAM_CAPTURE_LIMIT = 64 * 1024
STREAM_LINE_LIMIT = 8192
PIANO_FAILURE_COUNTERS = ("overflows", "recoveries", "native_errors",
                          "native_render_lock_misses", "recovery_failures", "discarded")
CASES = (
    "01-piano-reference",
    "02-warm-layers",
    "03-filter-motion",
    "04-shimmer-build",
    "05-piano-open-tone",
)


def setting(section: str, param: str, value) -> dict:
    return {"type": "setting", "section": section, "param": param, "value": value}


def build_schedule() -> list[tuple[float, int, int, int]]:
    """Return the same 45-second progression for every take, plus 8s tail."""
    chords = (
        (48, 55, 60, 62, 64),       # Cadd9
        (47, 55, 59, 62, 67),       # G/B
        (45, 52, 55, 60, 64),       # Am7
        (41, 48, 53, 55, 57, 60),   # Fadd9
        (48, 55, 60, 62, 64),
        (47, 55, 59, 62, 67),
        (45, 52, 55, 60, 64),
        (41, 48, 53, 55, 57, 60),
        (40, 48, 55, 60, 64),       # C/E
        (41, 48, 53, 55, 57, 60),
    )
    events: list[tuple[float, int, int, int]] = []
    for chord_index, chord in enumerate(chords):
        onset = MUSIC_START_SECONDS + chord_index * 4.5
        events.append((onset, 0xB0, 64, 127))
        for voice, note in enumerate(chord):
            events.append((onset, 0x90, note, 66 if voice == 0 else 72))
        release = onset + 4.15
        for note in chord:
            events.append((release, 0x80, note, 0))
        events.append((onset + 4.35, 0xB0, 64, 0))

    # The capture driver requires CC64-up and CC123 as the final two events.
    # Sostenuto is released immediately before those two, all at the start of
    # the deliberate eight-second effect tail.
    events.extend(((MUSIC_END_SECONDS, 0xB0, 66, 0),
                   (MUSIC_END_SECONDS, 0xB0, 64, 0),
                   (MUSIC_END_SECONDS, 0xB0, 123, 0)))
    return sorted(events, key=lambda event: event[0])


def render_schedule(events: list[tuple[float, int, int, int]]) -> str:
    lines = ["# seconds status data1 data2"]
    lines.extend(f"{when:.3f} {status:02X} {data1:d} {data2:d}"
                 for when, status, data1, data2 in events)
    return "\n".join(lines) + "\n"


def _warm_commands() -> list[dict]:
    commands = [
        setting("synth_pad", "osc1_blend", 0.35),
        setting("synth_pad", "osc2_blend", 0.25),
        setting("synth_pad", "osc1_waveform", "sine"),
        setting("synth_pad", "osc2_waveform", "triangle"),
        setting("synth_pad", "osc_hard_pan", False),
        setting("synth_pad", "osc1_pan", -0.15),
        setting("synth_pad", "osc2_pan", 0.15),
        setting("synth_pad", "unison_voices", 3),
        setting("synth_pad", "unison_detune", 0.035),
        setting("synth_pad", "unison_spread", 0.7),
        setting("synth_pad", "osc1_filter_enabled", True),
        setting("synth_pad", "osc2_filter_enabled", True),
        setting("synth_pad", "osc1_indep_cutoff", 20000),
        setting("synth_pad", "osc2_indep_cutoff", 20000),
        setting("synth_pad", "lfo_depth", 0.0),
        setting("synth_pad", "lfo2_depth", 0.0),
        setting("synth_pad", "delay_enabled", False),
        setting("synth_pad", "osc1_fx_bypass", False),
        setting("synth_pad", "osc2_fx_bypass", False),
        setting("synth_pad", "osc1_reverb_send", 1.0),
        setting("synth_pad", "osc2_reverb_send", 1.0),
        setting("synth_pad", "filter_cutoff_hz", 3500),
        setting("synth_pad", "reverb_type", "wash"),
        setting("synth_pad", "reverb_dry_wet", 0.35),
        setting("synth_pad", "reverb_decay_seconds", 6.0),
        setting("synth_pad", "reverb_damp", 0.5),
        setting("synth_pad", "reverb_low_cut", 150),
        setting("synth_pad", "reverb_high_cut", 10000),
        setting("synth_pad", "reverb_predelay_ms", 25),
        setting("synth_pad", "reverb_wet_gain", 1.0),
        setting("master", "piano_reverb_send", 0.0),
        setting("master", "piano_delay_send", 0.0),
    ]
    for osc, values in (("adsr_osc1", (350, 1800, 78, 1600)),
                        ("adsr_osc2", (500, 2200, 70, 2200))):
        for name, value in zip(("attack_ms", "decay_ms", "sustain_percent", "release_ms"), values):
            commands.append(setting("synth_pad", f"{osc}.{name}", value))
    return commands


def common_commands(baseline: dict) -> list[dict]:
    synth = baseline["synth_pad"]
    return [
        setting("master", "instrument_mode", "piano"),
        setting("master", "split_enabled", False),
        # split_enabled can swap octave snapshots; reassert the saved OSC
        # values while intentionally pinning piano to its normal octave.
        setting("synth_pad", "osc1_octave", synth["osc1_octave"]),
        setting("synth_pad", "osc2_octave", synth["osc2_octave"]),
        setting("synth_pad", "shimmer_high", synth["shimmer_high"]),
        setting("master", "transpose_semitones", 0),
        setting("master", "piano_octave", 0),
        setting("master", "volume", 0.75),
        setting("piano", "enabled", True),
    ]


def case_commands(case: str, baseline: dict) -> list[dict]:
    if case not in CASES:
        raise ValueError(f"unknown audition case: {case}")
    commands = common_commands(baseline)
    if case in ("01-piano-reference", "05-piano-open-tone"):
        commands += [
            setting("synth_pad", "osc1_blend", 0.0),
            setting("synth_pad", "osc2_blend", 0.0),
            setting("master", "piano_reverb_send", 0.0),
            setting("master", "piano_delay_send", 0.0),
        ]
        if case == "05-piano-open-tone":
            commands.append(setting("piano", "filter_highcut_hz", 12000))
    else:
        commands += _warm_commands()
        if case == "03-filter-motion":
            commands.append(setting("synth_pad", "filter_cutoff_hz", 800))
        elif case == "04-shimmer-build":
            commands += [
                setting("synth_pad", "shimmer_enabled", True),
                setting("synth_pad", "reverb_dry_wet", 0.2),
                setting("synth_pad", "reverb_shimmer_fb", 0.0),
                setting("master", "piano_reverb_send", 0.0),
            ]
    return commands


def sweep_plan(case: str) -> list[tuple[float, dict]]:
    if case == "03-filter-motion":
        result = []
        samples = int((MUSIC_END_SECONDS - MUSIC_START_SECONDS) * 5) + 1
        peak_index = (samples - 1) // 2
        for i in range(samples):
            when = MUSIC_START_SECONDS + i / 5
            if i <= peak_index:
                progress = i / peak_index
            else:
                progress = 1 - (i - peak_index) / (samples - 1 - peak_index)
            cutoff = 800 * ((6500 / 800) ** progress)
            result.append((round(when, 6), setting("synth_pad", "filter_cutoff_hz", round(cutoff, 3))))
        return result
    if case == "04-shimmer-build":
        result = []
        samples = int((MUSIC_END_SECONDS - MUSIC_START_SECONDS) * 5) + 1
        for i in range(samples):
            when = MUSIC_START_SECONDS + i / 5
            progress = i / (samples - 1)
            wet = 0.2 + 0.3 * progress
            shimmer = 0.3 * progress
            piano_send = 0.2 * progress
            result.extend(((when, setting("synth_pad", "reverb_dry_wet", wet)),
                           (when, setting("synth_pad", "reverb_shimmer_fb", shimmer)),
                           (when, setting("master", "piano_reverb_send", piano_send))))
        return result
    return []


def _path_value(state: dict, section: str, param: str):
    value = state[section]
    for part in param.split("."):
        value = value[part]
    return copy.deepcopy(value)


def touched_controls(baseline: dict) -> list[tuple[str, str]]:
    seen = set()
    ordered = []
    for case in CASES:
        for command in case_commands(case, baseline):
            key = (command["section"], command["param"])
            if key not in seen:
                seen.add(key)
                ordered.append(key)
    # split toggles these additional persisted values as a side effect.
    # Panic also clears these three controls; the preflight below only permits
    # their normal inactive baseline, but including them makes restoration
    # explicit and auditable rather than relying on that assumption.
    for key in (("master", "split_octave_snapshot"),
                ("synth_pad", "freeze_enabled"),
                ("synth_pad", "drone_enabled"),
                ("synth_pad", "drone_key")):
        if key not in seen:
            ordered.append(key)
    return ordered


def restore_commands(baseline: dict) -> list[dict]:
    commands = []
    # Reverb type can rewrite several controls, so restore it before the rest.
    keys = touched_controls(baseline)
    keys.sort(key=lambda key: 0 if key == ("synth_pad", "reverb_type") else 1)
    for section, param in keys:
        commands.append(setting(section, param, _path_value(baseline, section, param)))
    return commands


_RUNTIME_RE = re.compile(r"^\s*window\.STAVE_RUNTIME\s*=\s*(\{.*\})\s*;\s*$", re.S)


def parse_runtime_config(source: str) -> dict:
    match = _RUNTIME_RE.fullmatch(source)
    if not match:
        raise RuntimeError("runtime-config.js has an unexpected format")
    value = json.loads(match.group(1))
    expected = {"websocket_port": WS_PORT, "instance": INSTANCE}
    if value != expected:
        raise RuntimeError(f"refusing non-audition runtime: {value!r}")
    return value


def fetch_runtime_config() -> str:
    with urlopen(HTTP_URL, timeout=3) as response:
        if response.status != 200:
            raise RuntimeError(f"runtime-config HTTP status {response.status}")
        return response.read(4097).decode("utf-8")


async def verify_runtime(fetcher=fetch_runtime_config) -> dict:
    return parse_runtime_config(await asyncio.to_thread(fetcher))


class CommandSocket:
    """Continuously drain WS traffic and dispatch one bounded command reply.

    Stave broadcasts state and acknowledgements to every connected client. A
    capture can otherwise leave more than ``max_queue`` unsolicited frames
    unread for 55 seconds, eventually starving WebSocket keepalive handling.
    This object is the sole recv owner and intentionally retains no broadcast
    backlog.
    """

    def __init__(self, websocket):
        self.websocket = websocket
        self._request_lock = asyncio.Lock()
        self._pending = None
        self._reader_task = None
        self.unsolicited_count = 0

    async def start(self):
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._reader())
        return self

    async def stop(self):
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _reader(self):
        try:
            while True:
                raw = await self.websocket.recv()
                try:
                    message = json.loads(raw)
                except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
                    self.unsolicited_count += 1
                    continue
                pending = self._pending
                if pending is not None and not pending.done():
                    predicate, future = pending.predicate, pending.future
                    if message.get("type") == "error":
                        future.set_exception(RuntimeError(
                            message.get("message", "Stave returned an error")))
                        continue
                    if predicate(message):
                        future.set_result(message)
                        continue
                self.unsolicited_count += 1
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            pending = self._pending
            if pending is not None and not pending.done():
                pending.future.set_exception(RuntimeError(f"WebSocket reader stopped: {exc}"))

    async def request(self, message: dict, predicate, *, timeout=5.0) -> dict:
        await self.start()
        async with self._request_lock:
            future = asyncio.get_running_loop().create_future()
            self._pending = _PendingResponse(predicate, future)
            try:
                await self.websocket.send(json.dumps(message, separators=(",", ":"), allow_nan=False))
                return await asyncio.wait_for(future, timeout)
            finally:
                self._pending = None


class _PendingResponse:
    __slots__ = ("predicate", "future")

    def __init__(self, predicate, future):
        self.predicate = predicate
        self.future = future

    def done(self):
        return self.future.done()


async def send_command(ws, message: dict) -> dict:
    kind = message["type"]
    if kind == "setting":
        reply = await ws.request(message, lambda item: item.get("type") == "setting_ack"
                                 and item.get("section") == message["section"]
                                 and item.get("param") == message["param"])
        if not _values_equal(reply.get("value"), message["value"]):
            raise RuntimeError(
                f"setting acknowledgement value mismatch for {message['section']}.{message['param']}: "
                f"{reply.get('value')!r} != {message['value']!r}")
        return reply
    expected = {"get_state": "state", "debug": "debug", "panic": "panic_ack"}[kind]
    if kind == "get_state":
        # Setting broadcasts also use type=state but omit authoritative health.
        # Never hydrate a qualification checkpoint from one of those frames.
        return await ws.request(message, lambda reply: reply.get("type") == "state"
                                and isinstance(reply.get("health"), dict))
    return await ws.request(message, lambda reply: reply.get("type") == expected)


def _values_equal(actual, expected) -> bool:
    if (isinstance(actual, (int, float)) and not isinstance(actual, bool)
            and isinstance(expected, (int, float)) and not isinstance(expected, bool)):
        return abs(float(actual) - float(expected)) <= 1e-6
    return actual == expected


def verify_state(response: dict, commands: list[dict]) -> list[str]:
    state = response["state"]
    failures = []
    # A case may intentionally refine a common value (warm cutoff 3500 ->
    # motion start 800, or wash wet .35 -> build start .20). Only its final
    # absolute command is the expected setup state.
    final_commands = {}
    for command in commands:
        final_commands[(command["section"], command["param"])] = command
    for command in final_commands.values():
        actual = _path_value(state, command["section"], command["param"])
        expected = command["value"]
        if not _values_equal(actual, expected):
            failures.append(f"state mismatch {command['section']}.{command['param']}: {actual!r} != {expected!r}")
    return failures


def health_failures(response: dict) -> list[str]:
    health = response.get("health") or {}
    failures = []
    if health.get("control_error"):
        failures.append(f"control_error: {health['control_error']}")
    if health.get("state_warning"):
        failures.append(f"state_warning: {health['state_warning']}")
    native = health.get("native_profile")
    if not isinstance(native, dict) or native.get("ready") is not True:
        failures.append("native profile is not ready")
    audio = health.get("audio") or {}
    for key in ("error", "graph_error"):
        if audio.get(key):
            failures.append(f"audio.{key}: {audio[key]}")
    piano = audio.get("piano_midi")
    if not isinstance(piano, dict) or piano.get("available") is False:
        failures.append("piano MIDI telemetry is unavailable")
    else:
        if type(piano.get("recovery_pending")) is not bool:
            failures.append("piano MIDI recovery status is unavailable")
        elif piano["recovery_pending"]:
            failures.append("piano MIDI recovery is pending")
        for key in ("pending", "queued"):
            value = piano.get(key)
            if type(value) is not int or value != 0:
                failures.append(f"piano MIDI {key} is not verified empty")
        for key in PIANO_FAILURE_COUNTERS:
            value = piano.get(key)
            if type(value) is not int or value < 0:
                failures.append(f"piano.{key} counter unavailable")
    ui = health.get("ui") or {}
    if ui and ui.get("healthy") is False:
        failures.append("UI listener reports unhealthy")
    return failures


def counter_growth(before_state: dict, after_state: dict,
                   before_debug: dict, after_debug: dict) -> list[str]:
    failures = []
    before_health = before_state.get("health") or {}
    after_health = after_state.get("health") or {}
    before_audio = before_health.get("audio") or {}
    after_audio = after_health.get("audio") or {}
    before_controls = before_health.get("controls") or {}
    after_controls = after_health.get("controls") or {}
    before_render = before_audio.get("render_metrics") or {}
    after_render = after_audio.get("render_metrics") or {}
    pairs = (
        ("bridge_xruns", before_debug.get("bridge_xruns", 0), after_debug.get("bridge_xruns", 0)),
        ("bridge_underruns", before_debug.get("bridge_underruns", 0), after_debug.get("bridge_underruns", 0)),
        ("audio.midi_dropped", before_audio.get("midi_dropped", 0), after_audio.get("midi_dropped", 0)),
        ("controls.dropped", before_controls.get("dropped", 0), after_controls.get("dropped", 0)),
        ("render.over_budget_count", before_render.get("over_budget_count", 0),
         after_render.get("over_budget_count", 0)),
        ("render.rejected_write_count", before_render.get("rejected_write_count", 0),
         after_render.get("rejected_write_count", 0)),
        ("render.missed_samples", before_render.get("missed_samples", 0),
         after_render.get("missed_samples", 0)),
        ("render.invalid_samples", before_render.get("invalid_samples", 0),
         after_render.get("invalid_samples", 0)),
    )
    for name, before, after in pairs:
        if isinstance(before, (int, float)) and isinstance(after, (int, float)) and after > before:
            failures.append(f"{name} grew from {before} to {after}")
    # Source-side interruptions are independent of bridge xruns. This tool
    # requires the repaired candidate's telemetry; use the saved older tool
    # for legacy-baseline captures, not implicit zero-valued missing evidence.
    before_piano = before_audio.get("piano_midi")
    after_piano = after_audio.get("piano_midi")
    for key in PIANO_FAILURE_COUNTERS:
        before = before_piano.get(key) if isinstance(before_piano, dict) else None
        after = after_piano.get(key) if isinstance(after_piano, dict) else None
        if (type(before) is not int or type(after) is not int
                or before < 0 or after < before):
            failures.append(f"piano.{key} counter unavailable or reset")
        elif after > before:
            failures.append(f"piano.{key} grew from {before} to {after}")
    return failures


async def _drain_stream(stream, anchors: dict | None = None) -> tuple[str, bool]:
    retained = bytearray()
    line = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        room = STREAM_CAPTURE_LIMIT - len(retained)
        if room > 0:
            retained.extend(chunk[:room])
        if len(chunk) > room:
            truncated = True
        if anchors is not None:
            line.extend(chunk)
            while b"\n" in line:
                raw, _, remainder = line.partition(b"\n")
                line = bytearray(remainder)
                if len(raw) <= STREAM_LINE_LIMIT:
                    try:
                        event = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    name = event.get("event")
                    if name in ("capture_started", "capture_complete", "capture_failed"):
                        anchors[name] = event
                        if name == "capture_started":
                            anchors["started_monotonic"] = asyncio.get_running_loop().time()
                            anchors["started"].set()
                else:
                    truncated = True
            if len(line) > STREAM_LINE_LIMIT:
                line.clear()
                truncated = True
    return retained.decode("utf-8", "replace"), truncated


async def terminate_owned_process(process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 3)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def run_sweeps(ws, case: str, anchor: float) -> list[dict]:
    timeline = []
    loop = asyncio.get_running_loop()
    for desired, command in sweep_plan(case):
        await asyncio.sleep(max(0.0, anchor + desired - loop.time()))
        sent = loop.time() - anchor
        await send_command(ws, command)  # one and only one command is outstanding
        timeline.append({"desired_seconds": desired, "sent_seconds": round(sent, 6),
                         "ack_seconds": round(loop.time() - anchor, 6), "command": command})
    return timeline


async def run_driver(driver: Path, schedule_path: Path, wav_path: Path, ws, case: str) -> dict:
    process = await asyncio.create_subprocess_exec(
        str(driver), str(schedule_path), str(wav_path), f"{DURATION_SECONDS:.1f}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=65536)
    anchors = {"started": asyncio.Event()}
    stdout_task = asyncio.create_task(_drain_stream(process.stdout, anchors))
    stderr_task = asyncio.create_task(_drain_stream(process.stderr))
    sweeps = []
    failure = None
    deadline = asyncio.get_running_loop().time() + DURATION_SECONDS + 15
    try:
        await asyncio.wait_for(anchors["started"].wait(),
                               min(10, max(0.0, deadline - asyncio.get_running_loop().time())))
        anchor = anchors["started_monotonic"]
        sweep_task = asyncio.create_task(run_sweeps(ws, case, anchor))
        wait_task = asyncio.create_task(process.wait())
        try:
            done, _ = await asyncio.wait(
                (wait_task, sweep_task), return_when=asyncio.FIRST_COMPLETED,
                timeout=max(0.0, deadline - asyncio.get_running_loop().time()))
            if not done:
                raise TimeoutError("native capture exceeded its absolute deadline")
            if wait_task in done and not sweep_task.done():
                raise RuntimeError("native driver exited before automation completed")
            sweeps = await sweep_task
            await asyncio.wait_for(wait_task, max(0.0, deadline - asyncio.get_running_loop().time()))
        except BaseException:
            sweep_task.cancel()
            wait_task.cancel()
            await asyncio.gather(sweep_task, wait_task, return_exceptions=True)
            raise
    except BaseException as exc:
        failure = f"{type(exc).__name__}: {exc}"
        await terminate_owned_process(process)
        if isinstance(exc, asyncio.CancelledError):
            raise
    stdout, stdout_truncated = await stdout_task
    stderr, stderr_truncated = await stderr_task
    report = {"returncode": process.returncode, "stdout": stdout,
              "stdout_truncated": stdout_truncated, "stderr": stderr,
              "stderr_truncated": stderr_truncated, "events": {
                  key: value for key, value in anchors.items()
                  if key in ("capture_started", "capture_complete", "capture_failed")},
              "sweep_timeline": sweeps}
    if failure:
        report["failure"] = failure
    return report


def _write_json_exclusive(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


async def run_case(ws, driver: Path, output_dir: Path, schedule_path: Path,
                   case: str, original: dict, restore: list[dict]) -> dict:
    print(f"Starting {case}: 55-second capture...", flush=True)
    result = {"case": case, "status": "FAIL", "failures": []}
    try:
        await send_command(ws, {"type": "panic"})
        for command in restore:
            await send_command(ws, command)
        initial = await send_command(ws, {"type": "get_state"})
        initial_debug = await send_command(ws, {"type": "debug"})
        initial_name = f"{case}.initial.json"
        _write_json_exclusive(output_dir / initial_name, initial)
        result["initial_state_file"] = initial_name
        result["setup_baseline_health"] = initial.get("health")
        result["setup_baseline_debug"] = initial_debug
        result["failures"].extend(health_failures(initial))

        setup = case_commands(case, original)
        result["setup_commands"] = setup
        for command in setup:
            await send_command(ws, command)
        await asyncio.sleep(1.0)
        verified = await send_command(ws, {"type": "get_state"})
        capture_before_debug = await send_command(ws, {"type": "debug"})
        result["verified_state"] = verified
        result["health_before"] = verified.get("health")
        result["debug_before"] = capture_before_debug
        result["failures"].extend(verify_state(verified, setup))
        result["failures"].extend(health_failures(verified))

        wav_name = f"{case}.wav"
        result["wav"] = wav_name
        driver_report = await run_driver(driver, schedule_path, output_dir / wav_name, ws, case)
        result["driver"] = driver_report

        after = await send_command(ws, {"type": "get_state"})
        after_debug = await send_command(ws, {"type": "debug"})
        result["health_after"] = after.get("health")
        result["debug_after"] = after_debug
        result["failures"].extend(health_failures(after))
        result["failures"].extend(counter_growth(verified, after, capture_before_debug, after_debug))
        complete = driver_report["events"].get("capture_complete")
        if driver_report["returncode"] != 0 or not complete:
            result["failures"].append("native capture did not complete successfully")
        elif any(complete.get(key, 0) for key in ("nonfinite", "xruns", "errors")):
            result["failures"].append("native capture reported nonfinite/xrun/error evidence")
        if driver_report["stdout_truncated"] or driver_report["stderr_truncated"]:
            result["failures"].append("native driver report output exceeded capture limit")
        if not result["failures"]:
            result["status"] = "PASS"
    except Exception as exc:
        result["failures"].append(f"{type(exc).__name__}: {exc}")
    _write_json_exclusive(output_dir / f"{case}.result.json", result)
    print(f"Finished {case}: {result['status']}", flush=True)
    return result


async def run(args) -> int:
    driver = Path(args.driver).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    if not driver.is_file() or not os.access(driver, os.X_OK):
        raise RuntimeError("--driver must name an executable regular file")
    if output_dir.exists() or not output_dir.parent.is_dir():
        raise RuntimeError("--output-dir must be a new directory below an existing parent")

    runtime = await verify_runtime()  # identity gate before any control mutation
    selected = (args.case,) if args.case else CASES
    events = build_schedule()
    schedule_text = render_schedule(events)

    async with websockets.connect(WS_URL, open_timeout=5, close_timeout=3,
                                  max_size=4 * 1024 * 1024, max_queue=16) as raw_ws:
        ws = await CommandSocket(raw_ws).start()
        original_response = await send_command(ws, {"type": "get_state"})
        initial_failures = health_failures(original_response)
        if initial_failures:
            await ws.stop()
            raise RuntimeError("audition preflight failed: " + "; ".join(initial_failures))
        original = copy.deepcopy(original_response["state"])
        # Validate all state paths and plan commands before the first panic.
        restore = restore_commands(original)
        if original["piano"].get("soundfont") != "Fluid":
            raise RuntimeError("audition baseline is not the expected saved Fluid piano; refusing tonal substitution")
        if original["synth_pad"].get("reverb_type") != "wash":
            raise RuntimeError("audition baseline is not the expected saved wash reverb; refusing algorithm substitution")
        if original["synth_pad"].get("freeze_enabled") or original["synth_pad"].get("drone_enabled"):
            raise RuntimeError("audition baseline has freeze/drone active; refusing a non-repeatable capture")

        output_dir.mkdir(mode=0o700)
        schedule_path = output_dir / "midi_schedule.txt"
        with schedule_path.open("x", encoding="ascii") as handle:
            handle.write(schedule_text)
        _write_json_exclusive(output_dir / "00-original-state.json", original_response)

        manifest = {"format": 2, "instance": INSTANCE, "runtime": runtime,
                    "piano_telemetry_required": True,
                    "websocket_url": WS_URL, "duration_seconds": DURATION_SECONDS,
                    "music_seconds": [MUSIC_START_SECONDS, MUSIC_END_SECONDS],
                    "schedule_sha256": hashlib.sha256(schedule_text.encode("ascii")).hexdigest(),
                    "schedule_events": len(events), "cases": []}

        try:
            for case in selected:
                manifest["cases"].append(
                    await run_case(ws, driver, output_dir, schedule_path, case, original, restore))
        finally:
            # Cleanup also runs on cancellation/interrupt. It is intentionally
            # limited to this already identity-verified isolated instance.
            try:
                await asyncio.shield(send_command(ws, {"type": "panic"}))
                for command in restore:
                    await asyncio.shield(send_command(ws, command))
                manifest["restored_state"] = await asyncio.shield(
                    send_command(ws, {"type": "get_state"}))
                restore_mismatches = verify_state(manifest["restored_state"], restore)
                if restore_mismatches:
                    manifest["restore_failure"] = "; ".join(restore_mismatches)
            except Exception as exc:
                manifest["restore_failure"] = f"{type(exc).__name__}: {exc}"
            try:
                _write_json_exclusive(output_dir / "manifest.json", manifest)
            finally:
                await ws.stop()
        if manifest.get("restore_failure"):
            return 1
        return 0 if all(item["status"] == "PASS" for item in manifest["cases"]) else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", required=True, help="compiled native_audition executable")
    parser.add_argument("--output-dir", required=True, help="new, non-existing evidence directory")
    parser.add_argument("--case", choices=CASES, help="capture only one named comparison")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    try:
        return asyncio.run(run(parse_args(argv)))
    except (KeyboardInterrupt, RuntimeError, OSError, TimeoutError) as exc:
        print(f"audition refused/failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
