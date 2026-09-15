#!/usr/bin/env python3
"""Bounded full-core soak of one externally supervised private audition PID.

No service, device, routing, recorder, preset or library writes. The accepted
existing C sample is mandatory. Short runs are not eight-hour qualification.
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
import re
import signal
import statistics
import sys
import time

import websockets

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.run_audition import (
    CommandSocket, WS_URL, _path_value, case_commands, setting, send_command,
    terminate_owned_process, verify_runtime, verify_state,
)
from tools.run_feature_probe import pad_slot, require

MAX_SECONDS = 28800
CHECKPOINT_SECONDS = 60
MAX_CHECKPOINTS = 485
MAX_JSON_LINE = 512 * 1024
MAX_EVIDENCE_BYTES = 60 * 1024 * 1024  # Plus a separately capped4MiB final report.
PIANO_COUNTERS = ("discarded", "native_errors", "native_render_lock_misses",
                  "overflows", "recoveries", "recovery_failures")
RENDER_COUNTERS = ("over_budget_count", "rejected_write_count", "missed_samples", "invalid_samples")


class Evidence:
    """Exclusive, bounded NDJSON; never silently truncate qualification data."""
    def __init__(self, root):
        self.root, self.bytes, self.handles = root, 0, {}

    def write(self, name, value):
        require(name in {"checkpoints.ndjson", "controls.ndjson", "native.ndjson", "session.ndjson"},
                "unknown evidence stream")
        raw = json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n"
        size = len(raw.encode("utf-8"))
        require(size <= MAX_JSON_LINE and self.bytes + size <= MAX_EVIDENCE_BYTES,
                "evidence size cap exceeded; refusing incomplete evidence")
        if name not in self.handles:
            self.handles[name] = exclusive_text(self.root / name)
        self.handles[name].write(raw)
        self.handles[name].flush()
        self.bytes += size

    def close(self):
        for handle in self.handles.values():
            handle.close()


def integer(value, label, minimum=0):
    require(type(value) is int and value >= minimum, f"{label}: missing/invalid integer")
    return value


def exclusive_text(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8")


def file_bytes(path, limit):
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    require(len(raw) <= limit, f"oversized probe input: {path.name}")
    return raw


def sha256_file(path):
    require(path.is_file() and not path.is_symlink(), "expected regular owned fixture")
    require(path.stat().st_size <= 64 * 1024 * 1024, "fixture exceeds bounded hash size")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(256 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_paths(args):
    require(type(args.seconds) is int and 20 <= args.seconds <= MAX_SECONDS,
            "--seconds must explicitly be 20..28800")
    integer(args.pid, "PID", 2)
    require(args.pid != os.getpid(), "target PID must not be coordinator")
    require(re.fullmatch(r"[a-fA-F0-9]{64}", args.pad_sha256 or "") is not None,
            "--pad-sha256 must pin the accepted existing C sample")
    private = Path(args.instance_root)
    require(private.is_absolute() and private.is_dir() and not private.is_symlink(),
            "--instance-root must be an existing absolute private directory")
    private = private.resolve()
    backup_base = (Path.home() / "stave-synth-pi4-backups").resolve()
    require(backup_base in private.parents and private != backup_base,
            "instance root must be beneath the private Pi4 backup directory")
    for folder in ("config", "data", "source"):
        path = private / folder
        require(path.is_dir() and not path.is_symlink() and private in path.resolve().parents,
                f"private {folder} directory missing or escaped")
    pad = private / "data" / "pad_samples" / "pad_C.wav"
    require(private / "data" in pad.resolve().parents and not pad.parent.is_symlink(),
            "C fixture escaped private data")
    require(sha256_file(pad) == args.pad_sha256.lower(), "accepted C fixture hash mismatch")
    output = Path(args.output_dir)
    require(output.is_absolute() and not output.exists() and output.parent.is_dir(),
            "output must be a new absolute directory beneath private root")
    require(private in output.parent.resolve().parents or output.parent.resolve() == private,
            "output escaped private root")
    require(not any(root == output.parent.resolve() or root in output.parent.resolve().parents
                    for root in (private / "config", private / "data", private / "source")),
            "evidence must not be written into runtime/source directories")
    driver = Path(args.driver).resolve()
    require(private in driver.parents and driver.is_file() and os.access(driver, os.X_OK),
            "driver must be an executable inside the explicit private root")
    return private, output, pad, driver


def process_snapshot(pid, private, *, expected_start=None, proc_root=Path("/proc")):
    folder = proc_root / str(pid)
    require(folder.stat().st_uid == os.geteuid(), "target process has another owner")
    stat = file_bytes(folder / "stat", 8192).decode("ascii")
    tail = stat[stat.rfind(")") + 2:].split()
    require(len(tail) > 19, "malformed /proc PID stat")
    start = int(tail[19])  # field22, tail begins with field3
    require(expected_start is None or start == expected_start, "audition PID was reused/restarted")
    command = file_bytes(folder / "cmdline", 65536).split(b"\0")
    require(any(command[i:i+2] == [b"-m", b"stave_synth.main"] for i in range(len(command)-1)),
            "PID is not the explicit stave_synth.main module")
    env = {}
    for item in file_bytes(folder / "environ", 256 * 1024).split(b"\0"):
        if b"=" in item:
            key, value = item.split(b"=", 1)
            if key.startswith(b"STAVE_"):
                env[key.decode("ascii")] = value.decode("utf-8")
    expected = {"STAVE_INSTANCE": "audition", "STAVE_INSTANCE_ROOT": str(private),
                "STAVE_HTTP_PORT": "18080", "STAVE_WEBSOCKET_PORT": "18765",
                "STAVE_REQUIRE_NATIVE": "1"}
    require(all(env.get(key) == value for key, value in expected.items()),
            "PID environment is not the exact isolated native audition")
    require((folder / "cwd").resolve() == private / "source", "audition source directory changed")
    values = {}
    for line in file_bytes(folder / "status", 65536).decode("ascii").splitlines():
        name, _, value = line.partition(":")
        if name in {"VmRSS", "VmHWM", "VmSwap", "Threads"}:
            pieces = value.split()
            require(pieces and pieces[0].isdigit(), f"invalid process {name}")
            if name != "Threads":
                require(pieces[1:] == ["kB"], f"invalid process {name} units")
            values[name] = int(pieces[0])
    require(set(values) == {"VmRSS", "VmHWM", "VmSwap", "Threads"}, "process memory telemetry incomplete")
    require(values["VmSwap"] == 0, "audition process is swapped")
    return {"pid": pid, "start_ticks": start, **values}


async def system_snapshot(pid, private, expected_start=None):
    process = await asyncio.to_thread(process_snapshot, pid, private, expected_start=expected_start)
    memory = file_bytes(Path("/proc/meminfo"), 65536).decode("ascii")
    match = re.search(r"^MemAvailable:\s+(\d+) kB$", memory, re.M)
    require(match is not None, "MemAvailable probe unavailable")
    available = int(match.group(1))
    require(available >= 128 * 1024, "provisional 128MiB system-memory reserve breached")
    temperature = int(file_bytes(Path("/sys/class/thermal/thermal_zone0/temp"), 128)) / 1000
    require(math.isfinite(temperature) and 0 < temperature < 75,
            "temperature unavailable or provisional 75C engineering ceiling breached")
    child = await asyncio.create_subprocess_exec("/usr/bin/vcgencmd", "get_throttled",
                                               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(child.communicate(), 3)
    except BaseException:
        await terminate_owned_process(child)
        raise
    require(child.returncode == 0 and len(stdout) < 128 and len(stderr) < 1024,
            "throttle probe failed")
    match = re.fullmatch(rb"throttled=0x([0-9a-fA-F]+)\s*", stdout)
    require(match is not None and int(match.group(1), 16) == 0,
            "nonzero/unknown throttle flags (including historical flags)")
    return {**process, "mem_available_kib": available, "temperature_c": temperature, "throttled": 0}


def strict_snapshot(state, debug):
    health = state.get("health", {})
    require(health.get("native_profile", {}).get("ready") is True, "native readiness missing")
    require(not health.get("control_error") and not health.get("state_warning"), "control/state health error")
    audio, controls, ui = (health.get(key, {}) for key in ("audio", "controls", "ui"))
    require(audio.get("sample_rate") == 48000 and audio.get("block_frames") == 512,
            "audio graph changed")
    require(audio.get("ring_slots") == 6, "declared six-slot profile changed")
    require(not audio.get("error") and audio.get("graph_error") == 0, "audio health error")
    require(controls.get("alive") is True and controls.get("closed") is False, "control worker unavailable")
    integer(controls.get("pending"), "controls.pending")
    for key in ("healthy", "http_listener", "websocket_listener", "event_loop_progressing",
                "handler_worker_available", "handler_worker_progressing"):
        require(ui.get(key) is True, f"UI {key} unavailable")
    require(integer(ui.get("clients"), "UI clients") >= 2, "two responsive peers are not connected")
    piano, render = audio.get("piano_midi", {}), audio.get("render_metrics", {})
    require(piano.get("recovery_pending") is False, "piano recovery pending/unknown")
    queued = integer(piano.get("queued"), "piano queued")
    require(queued <= integer(piano.get("queue_capacity"), "piano capacity", 1), "piano queue exceeds bound")
    require(integer(piano.get("pending"), "piano pending") == queued, "unknown piano pending work")
    counters = {}
    for prefix, source, keys in (
        ("bridge", debug, ("bridge_underruns", "bridge_xruns", "bridge_callbacks")),
        ("audio", audio, ("midi_dropped", "midi_recoveries")),
        ("controls", controls, ("dropped",)),
        ("ui", ui, ("producer_overflows", "slow_client_disconnects")),
        ("piano", piano, PIANO_COUNTERS + ("enqueued", "applied")),
        ("render", render, RENDER_COUNTERS + ("duration_count",))):
        for key in keys:
            counters[f"{prefix}.{key}"] = integer(source.get(key), f"{prefix}.{key}")
    require(type(render.get("duration_sum_seconds")) in (int, float)
            and math.isfinite(render["duration_sum_seconds"])
            and render["duration_sum_seconds"] >= 0, "render timing sum unavailable")
    require(type(render.get("duration_max_seconds")) in (int, float)
            and math.isfinite(render["duration_max_seconds"])
            and render["duration_max_seconds"] >= 0, "render maximum unavailable")
    require(integer(render.get("min_ring_fill_blocks"), "minimum ring fill") <= 6,
            "minimum ring fill outside profile")
    histogram = render.get("histogram", {})
    require(isinstance(histogram, dict), "render histogram unavailable")
    cumulative = histogram.get("cumulative_counts")
    require(isinstance(cumulative, list) and len(cumulative) == 161,
            "render histogram count bins unavailable")
    require(all(type(value) is int and value >= 0 for value in cumulative)
            and cumulative == sorted(cumulative)
            and cumulative[-1] == counters["render.duration_count"],
            "render histogram counts inconsistent")
    width = histogram.get("bin_width_seconds")
    require(type(width) in (int, float) and math.isfinite(width)
            and abs(width-(512/48000/100)) < 1e-12, "render histogram period changed")
    return counters


PROGRESS = {"bridge.bridge_callbacks", "piano.enqueued", "piano.applied", "render.duration_count"}


def compare_counters(before, after):
    require(before.keys() == after.keys(), "counter identity changed")
    delta = {}
    for key in before:
        require(after[key] >= before[key], f"{key} reset")
        delta[key] = after[key] - before[key]
    require(delta["bridge.bridge_callbacks"] > 0 and delta["render.duration_count"] > 0,
            "audio/render progress stopped")
    fatal = [f"{key} grew by {value}" for key, value in delta.items()
             if value and key not in PROGRESS | {"render.over_budget_count"}]
    return delta, fatal


def phrase_text():
    # Cadd9 voicing, 60s loop; independent C bed continues through rests.
    events = []
    for start, end in ((2, 9), (12, 19), (22, 29), (32, 39), (42, 49)):
        events.append((start, 0xB0, 64, 127))
        for index, note in enumerate((48, 55, 60, 62, 64)):
            events += [(start, 0x90, note, 66 if index == 0 else 72), (end, 0x80, note, 0)]
        events.append((end + 0.2, 0xB0, 64, 0))
    return "loop_seconds 60\n" + "".join(f"{s:.3f} {status:02X} {a} {b}\n"
                                         for s, status, a, b in sorted(events))


def expected_midi_events(seconds):
    loops, remainder = divmod(seconds, 60)
    timestamps = [float(line.split()[0]) for line in phrase_text().splitlines()[1:]]
    return loops*len(timestamps) + sum(when < remainder for when in timestamps)


def plans(original):
    require(original["master"]["instrument_mode"] == "piano"
            and original["piano"]["enabled"] is True and original["organ"]["enabled"] is False,
            "known baseline piano/organ mode required")
    require(original["piano"]["soundfont"] == "Fluid"
            and original["synth_pad"]["reverb_type"] == "wash"
            and original["synth_pad"]["unison_voices"] == 3, "declared Fluid/wash/three-unison baseline required")
    require(original["synth_pad"]["freeze_enabled"] is False
            and original["synth_pad"]["drone_enabled"] is False
            and original["synth_pad"]["drone_key"] is None, "baseline freeze/bed must be idle")
    setup = [item for item in case_commands("02-warm-layers", original) if item["param"] != "reverb_type"]
    setup.append(setting("synth_pad", "drone_level", 0.4))
    keys = list(dict.fromkeys((x["section"], x["param"]) for x in setup))
    keys += [("master", "split_octave_snapshot"), ("organ", "enabled")]
    def priority(key):
        if key == ("master", "instrument_mode"): return 0
        if key == ("master", "split_enabled"): return 1
        if key == ("master", "split_octave_snapshot"): return 3
        if key in (("piano", "enabled"), ("organ", "enabled")): return 4
        return 2
    restore = [setting(s, p, _path_value(original, s, p)) for s, p in sorted(keys, key=priority)]
    json.dumps(restore, allow_nan=False)
    return setup, restore


def memory_gate(samples, seconds):
    result = {"provisional": True, "sufficient": False, "passed": False,
              "warmup_seconds": 3600, "max_growth_kib": 32768, "max_slope_kib_per_hour": 1024}
    if seconds < MAX_SECONDS:
        return result
    buckets = [[x["VmRSS"] for x in samples if 3600 + hour*3600 <= x["elapsed_seconds"] < 7200 + hour*3600]
               for hour in range(7)]
    if any(len(bucket) < 50 for bucket in buckets):
        return result
    medians = [statistics.median(bucket) for bucket in buckets]
    slope = sum((i-3)*(value-statistics.mean(medians)) for i, value in enumerate(medians)) / 28
    result.update(sufficient=True, hourly_median_rss_kib=medians, slope_kib_per_hour=slope,
                  passed=medians[-1] <= medians[0] + 32768 and slope <= 1024)
    return result


class Soak:
    def __init__(self, args, private, output, pad, driver, report, evidence):
        self.args, self.private, self.output, self.pad, self.driver = args, private, output, pad, driver
        self.report, self.evidence = report, evidence
        self.ws = []
        self.process = None
        self.started = asyncio.Event()
        self.anchor = None
        self.native_final = None
        self.start_ticks = None
        self.previous = None
        self.baseline = None
        self.system_samples = []
        self.command_count = 0
        self.max_ack_ms = 0.0
        self.max_curve_lateness_ms = 0.0
        self.native_checkpoints = 0
        self.native_frames = 0
        self.native_midi_events = 0
        self.music_complete = False
        self.grace_seconds = None
        self.control_barrier = asyncio.Lock()

    async def command(self, peer, label, message, reply_type=None, *, scheduled_ns=None):
        sent = time.monotonic_ns()
        if reply_type:
            reply = await self.ws[peer].request(message, lambda item: item.get("type") == reply_type)
            require(not reply.get("error"), f"{label} rejected")
        else:
            reply = await send_command(self.ws[peer], message)
        finished = time.monotonic_ns()
        self.command_count += 1
        self.max_ack_ms = max(self.max_ack_ms, (finished-sent)/1e6)
        self.evidence.write("controls.ndjson", {"peer": peer, "label": label, "command": message,
                         "scheduled_monotonic_ns": scheduled_ns,
                         "sent_monotonic_ns": sent, "ack_monotonic_ns": finished})
        return reply

    async def checkpoint(self, phase, elapsed=0.0, *, compare=True):
        require(len(self.system_samples) < MAX_CHECKPOINTS, "checkpoint capacity exhausted")
        async with self.control_barrier:
            states = [await self.command(i, phase, {"type": "get_state"}) for i in (0, 1)]
        require(states[0]["state"] == states[1]["state"]
                and all(states[0].get(key) == states[1].get(key) for key in ("faded_out", "drone_faded_out")),
                "two peer states did not converge across control barrier")
        debug = await self.command(0, phase + "_debug", {"type": "debug"})
        counters = strict_snapshot(states[0], debug)
        strict_snapshot(states[1], debug)
        system = await system_snapshot(self.args.pid, self.private, self.start_ticks)
        if self.start_ticks is None:
            self.start_ticks = system["start_ticks"]
        system["elapsed_seconds"] = elapsed
        self.system_samples.append(system)
        delta, fatal = ({}, []) if self.previous is None or not compare else compare_counters(self.previous, counters)
        self.evidence.write("checkpoints.ndjson", {"phase": phase, "elapsed_seconds": elapsed,
                                                   "primary_state": states[0], "peer_health": states[1]["health"],
                                                   "debug": debug, "system": system, "delta": delta, "failures": fatal})
        if compare:
            self.previous = counters
        require(not fatal, "; ".join(fatal))
        return states[0], counters

    async def controls(self):
        await self.started.wait()
        tick = 0
        last_sent = None
        while True:
            desired = self.anchor + tick * 0.2
            # Never burst overdue commands to catch up with the ideal curve.
            eligible = desired if last_sent is None else max(desired, last_sent+0.2)
            await asyncio.sleep(max(0, eligible-time.monotonic()))
            elapsed = time.monotonic()-self.anchor
            if elapsed >= self.args.seconds-2:
                return
            require(elapsed-tick*0.2 < 1, "control scheduler more than one second late")
            phase = (tick*0.2) % 60
            if 10 <= phase < 40:
                movement = 0.5 - 0.5*math.cos(2*math.pi*(phase-10)/30)
                if tick % 2 == 0:
                    command = setting("synth_pad", "filter_cutoff_hz", round(800*(6000/800)**movement, 3))
                else:
                    command = setting("synth_pad", "reverb_dry_wet", round(0.2+0.25*movement, 6))
                async with self.control_barrier:
                    last_sent = time.monotonic()
                    self.max_curve_lateness_ms = max(self.max_curve_lateness_ms, (last_sent-desired)*1000)
                    require(last_sent-desired < 1, "control barrier delayed curve more than one second")
                    await self.command(tick % 2, "gentle_live_curve", command, scheduled_ns=int(desired*1e9))
            tick += 1

    async def monitor(self):
        await self.started.wait()
        # For arbitrary short CLI durations, do not start a slow system probe
        # less than ten seconds before the explicit terminal-marker snapshot.
        moments = range(60, self.args.seconds-10, 60)
        for elapsed in moments:
            await asyncio.sleep(max(0, self.anchor+elapsed-time.monotonic()))
            require(time.monotonic() < self.anchor+self.args.seconds-0.5,
                    "musical checkpoint missed preterminal window")
            state, counters = await self.checkpoint("musical", time.monotonic()-self.anchor)
            slot = await self.command(1, "bed_inventory", {"type": "list_pad_slots"}, "pad_slots")
            pad_slot(slot, active=True)
            require(state.get("drone_faded_out") is False and state["state"]["synth_pad"]["drone_key"] == 60,
                    "independent bed state changed")
            self.report["last_musical_counters"] = counters
            self.report["last_musical_snapshot_seconds"] = time.monotonic()-self.anchor

    async def end_checkpoint(self, event):
        """Use the driver's explicit grace window; no slow OS probe here."""
        async with self.control_barrier:
            states = [await self.command(i, "music_complete_state", {"type": "get_state"}) for i in (0, 1)]
            debug = await self.command(0, "music_complete_debug", {"type": "debug"})
        require(states[0]["state"] == states[1]["state"]
                and all(states[0].get(key) == states[1].get(key) for key in ("faded_out", "drone_faded_out")),
                "terminal musical peer states did not converge")
        state = states[0]
        counters = strict_snapshot(state, debug)
        strict_snapshot(states[1], debug)
        delta, fatal = compare_counters(self.previous, counters)
        finished = time.monotonic_ns()
        require(finished < event["monotonic_ns"] + int((self.grace_seconds-0.1)*1e9),
                "preterminal health snapshot overlapped native release window")
        self.evidence.write("checkpoints.ndjson", {"phase": "music_complete_before_native_release",
                  "primary_state": state, "peer_health": states[1]["health"],
                  "debug": debug, "delta": delta, "failures": fatal,
                  "marker_monotonic_ns": event["monotonic_ns"], "finished_monotonic_ns": finished})
        self.report["last_musical_counters"] = counters
        self.report["last_musical_snapshot_seconds"] = finished/1e9-self.anchor
        self.report["music_complete_marker"] = event
        self.previous = counters
        require(not fatal, "; ".join(fatal))

    def validate_audio_event(self, event):
        for key in ("nonfinite", "xruns", "errors", "zero_active_blocks", "clipped"):
            require(integer(event.get(key), "native "+key) == 0, "native "+key+" grew")
        for key in ("peak", "rms"):
            value = event.get(key)
            require(type(value) in (int, float) and math.isfinite(value) and 0 <= value < 1,
                    "native "+key+" unavailable/nonfinite/full-scale")

    async def native(self):
        prefix = ["--smoke", "--expect-bed"] if self.args.seconds < 600 else ["--expect-bed"]
        self.process = await asyncio.create_subprocess_exec(str(self.driver), *prefix,
                            str(self.output / "phrase.txt"), str(self.args.seconds),
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=8192)
        async def stderr_reader():
            retained = bytearray()
            while chunk := await self.process.stderr.read(4096):
                require(len(retained)+len(chunk) <= 65536, "native stderr cap exceeded")
                retained.extend(chunk)
            return retained.decode("utf-8", "replace")
        stderr_task = asyncio.create_task(stderr_reader())
        try:
            while True:
                line = await asyncio.wait_for(self.process.stdout.readline(), 90 if self.started.is_set() else 10)
                if not line:
                    break
                require(len(line) <= 8192, "native JSON line too large")
                event = json.loads(line)
                self.evidence.write("native.ndjson", event)
                kind = event.get("event")
                if kind == "soak_started":
                    require(not self.started.is_set() and event.get("requested_seconds") == self.args.seconds,
                            "native start identity/duration mismatch")
                    require(event.get("sample_rate_hz") == 48000 and event.get("block_frames") == 512,
                            "native graph mismatch")
                    require(event.get("expect_bed") is True
                            and event.get("qualification") is (self.args.seconds == MAX_SECONDS),
                            "native bed/duration qualification mode mismatch")
                    require(event.get("loop_seconds") == 60 and event.get("phrase_events") == 60
                            and event.get("tail_seconds") == 2, "native phrase/tail contract mismatch")
                    self.grace_seconds = event.get("terminal_grace_seconds")
                    require(self.grace_seconds == 5, "native preterminal grace contract missing")
                    self.anchor = integer(event.get("started_monotonic_ns"), "native start timestamp", 1)/1e9
                    require(abs(time.monotonic()-self.anchor) < 2, "stale native start marker")
                    self.started.set()
                elif kind == "soak_checkpoint":
                    require(self.started.is_set(), "native checkpoint before start")
                    require(self.native_final is None, "native checkpoint after completion")
                    require(event.get("index") == self.native_checkpoints and event.get("start_frame") == self.native_frames,
                            "native checkpoint identity/gap/reset")
                    frames = integer(event.get("frames"), "native interval frames", 1)
                    require(frames <= 60*48000+512 and self.native_checkpoints < MAX_CHECKPOINTS,
                            "native interval count/size invalid")
                    self.validate_audio_event(event)
                    self.native_frames += frames
                    self.native_midi_events += integer(event.get("midi_events"), "native interval MIDI")
                    self.native_checkpoints += 1
                elif kind == "soak_music_complete":
                    require(self.started.is_set() and not self.music_complete and self.native_final is None,
                            "native musical boundary duplicate/out of order")
                    require(event.get("requested_frames") == self.args.seconds*48000
                            and event.get("phrase_midi_events") == expected_midi_events(self.args.seconds),
                            "native musical duration/event count incomplete")
                    integer(event.get("monotonic_ns"), "native musical boundary timestamp", 1)
                    self.music_complete = True
                    await asyncio.wait_for(self.end_checkpoint(event), self.grace_seconds-0.2)
                elif kind in {"soak_complete", "soak_failed"}:
                    require(self.native_final is None, "duplicate native completion")
                    self.native_final = event
                else:
                    raise RuntimeError("unrecognized native evidence event")
            code = await asyncio.wait_for(self.process.wait(), 5)
            self.report["native_stderr"] = await stderr_task
            require(code == 0 and self.native_final is not None
                    and self.native_final.get("event") == "soak_complete", "native soak incomplete/failed")
            final = self.native_final
            self.validate_audio_event(final)
            require(self.music_complete and final.get("interrupted") is False
                    and final.get("qualification") is (self.args.seconds == MAX_SECONDS),
                    "native interrupted/qualification/boundary evidence invalid")
            require(integer(final.get("zero_music_blocks"), "music silent blocks") == 0
                    and final.get("music_blocks") == math.ceil(self.args.seconds*48000/512),
                    "continuous-bed musical audio missing")
            require(final.get("requested_frames") == self.args.seconds*48000
                    and final.get("terminal_release_events") == 3, "native duration/release incomplete")
            require(final.get("phrase_midi_events") == expected_midi_events(self.args.seconds)
                    and final.get("midi_events") == expected_midi_events(self.args.seconds)+3
                    and final.get("midi_events") == self.native_midi_events,
                    "native MIDI frame accounting incomplete")
            require(final.get("captured_frames") == self.native_frames
                    and final.get("checkpoints") == self.native_checkpoints
                    and (self.args.seconds+2)*48000 <= self.native_frames <= (self.args.seconds+8)*48000,
                    "native capture interval accounting incomplete")
            for key in ("midi_first_callback_monotonic_ns", "capture_first_callback_monotonic_ns"):
                integer(final.get(key), key, 1)
            self.report["native_final"] = final
        finally:
            if self.process.returncode is None:
                await terminate_owned_process(self.process)
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)

    async def capture(self):
        tasks = [asyncio.create_task(fn()) for fn in (self.native, self.controls, self.monitor)]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), self.args.seconds+20)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def cleanup(self, restore, original):
        errors = self.report["restore_failures"] = []
        # Never target a reused/new process with cleanup commands.
        try:
            await asyncio.to_thread(process_snapshot, self.args.pid, self.private, expected_start=self.start_ticks)
        except Exception as exc:
            errors.append(f"cleanup refused changed identity: {exc}")
            return
        try:
            await self.command(0, "intentional_cleanup_panic", {"type": "panic"})
        except Exception as exc:
            errors.append(f"panic: {exc}")
        for command in restore:
            try:
                await self.command(0, "restore", command)
            except Exception as exc:
                errors.append(f"restore {command['section']}.{command['param']}: {exc}")
        try:
            state = await self.command(0, "restored_state", {"type": "get_state"})
            self.report["restored_state"] = state
            require(state["state"] == original and not verify_state(state, restore), "complete state restoration mismatch")
            require(state.get("drone_faded_out") is False and state.get("faded_out") is False,
                    "fade targets not reset")
            require(sha256_file(self.pad) == self.args.pad_sha256.lower(), "accepted pad file changed")
        except Exception as exc:
            errors.append(f"restoration verification: {exc}")

    async def session(self):
        await verify_runtime()
        identity = await system_snapshot(self.args.pid, self.private)
        self.start_ticks = identity["start_ticks"]
        self.report["initial_system"] = identity
        async with websockets.connect(WS_URL, open_timeout=5, close_timeout=2, max_size=MAX_JSON_LINE, max_queue=16) as a, \
                   websockets.connect(WS_URL, open_timeout=5, close_timeout=2, max_size=MAX_JSON_LINE, max_queue=16) as b:
            self.ws = [await CommandSocket(a).start(), await CommandSocket(b).start()]
            mutated, restore, original = False, [], None
            try:
                state = await self.command(0, "original_state", {"type": "get_state"})
                original = copy.deepcopy(state["state"])
                self.report["original_state"] = state
                require(state.get("faded_out") is False and state.get("drone_faded_out") is False, "baseline fade active")
                rec = state.get("record_status", {})
                require(rec.get("recording") is False and rec.get("writer_pending") is False, "recorder not idle")
                setup, restore = plans(original)
                self.report.update(setup_commands=setup, restore_commands=restore)
                inventory = await self.command(0, "accepted_C_slot", {"type": "list_pad_slots"}, "pad_slots")
                slot = pad_slot(inventory, active=False)
                require(slot.get("filename") == "pad_C.wav", "C slot has unexpected file identity")
                require(all(x.get("active") is False and x.get("preparing") is False for x in inventory["slots"]),
                        "another pad is active/preparing")
                self.report["accepted_C_slot"] = slot
                self.evidence.write("session.ndjson", self.report)
                await verify_runtime()
                await asyncio.to_thread(process_snapshot, self.args.pid, self.private, expected_start=self.start_ticks)
                require(sha256_file(self.pad) == self.args.pad_sha256.lower(), "C sample changed before mutation")
                mutated = True
                await self.command(0, "setup_panic", {"type": "panic"})
                for command in setup:
                    await self.command(0, "setup", command)
                triggered = await self.command(0, "start_accepted_C_bed", {"type": "drone_key", "note": 60}, "drone_key_ack")
                require(triggered.get("source") == "sample" and triggered.get("enabled") is True
                        and triggered.get("note") == 60, "accepted sampled bed did not trigger")
                await asyncio.sleep(4.5)  # Existing pad attack is four seconds.
                ready, self.baseline = await self.checkpoint("post_ready_baseline", compare=False)
                require(not verify_state(ready, setup), "post-ready patch differs from declaration")
                self.previous = self.baseline
                self.report["baseline_counters"] = self.baseline
                self.evidence.write("session.ndjson", self.report)
                await self.capture()
                after, terminal = await self.checkpoint("after_native_terminal_release", time.monotonic()-self.anchor, compare=False)
                self.report["after_native_terminal_counters"] = terminal
                self.report["raw_total_delta"], _ = compare_counters(self.baseline, terminal)
                last = self.report["last_musical_counters"]
                self.report["musical_delta"], _ = compare_counters(self.baseline, last)
                self.report["terminal_window_delta"] = {key: terminal[key]-last[key] for key in last}
                require(all(value >= 0 for value in self.report["terminal_window_delta"].values()), "terminal counter reset")
            finally:
                try:
                    if mutated:
                        try:
                            await asyncio.wait_for(self.cleanup(restore, original), 30)
                        except BaseException as exc:
                            self.report.setdefault("restore_failures", []).append(f"cleanup incomplete: {type(exc).__name__}: {exc}")
                            if isinstance(exc, asyncio.CancelledError):
                                raise
                finally:
                    await asyncio.gather(*(ws.stop() for ws in self.ws), return_exceptions=True)


async def run(args):
    private, output, pad, driver = validate_paths(args)
    output.mkdir(mode=0o700)
    phrase = phrase_text()
    with exclusive_text(output / "phrase.txt") as handle:
        handle.write(phrase)
    report = {"format": 1, "status": "FAIL", "stage_qualified": False, "core_soak_qualified": False,
              "requested_seconds": args.seconds, "instance_root": str(private), "pid": args.pid,
              "pad_sha256": args.pad_sha256.lower(), "phrase_sha256": hashlib.sha256(phrase.encode()).hexdigest(),
              "driver_sha256": sha256_file(driver), "failures": [], "restore_failures": [],
              "limits": ["Parent supervisor owns service lifecycle, source/native hashes and rollback.",
                         "Two protocol peers are not real Safari sleep/Wi-Fi/hardware qualification.",
                         "Independent accepted C sample remains through phrase rests; not all-source-idle GC qualification.",
                         "Exact-zero mixed audio cannot prove individual source continuity or subjective quality.",
                         "Native musical-end marker grants a bounded5s grace; app counters are captured before terminal MIDI release.",
                         "75C ceiling,128MiB reserve and hourly memory criteria are provisional engineering gates.",
                         "All raw timing failures retained; over-budget growth alone does not abort the continuity experiment."]}
    evidence = Evidence(output)
    soak = Soak(args, private, output, pad, driver, report, evidence)
    try:
        await asyncio.wait_for(soak.session(), args.seconds+120)
    except BaseException as exc:
        report["failures"].append(f"{type(exc).__name__}: {exc}")
        if not isinstance(exc, Exception):
            raise
    finally:
        report["memory_gate"] = memory_gate(soak.system_samples, args.seconds)
        report["command_count"], report["max_ack_ms"] = soak.command_count, soak.max_ack_ms
        report["max_curve_lateness_ms"] = soak.max_curve_lateness_ms
        raw = report.get("raw_total_delta", {})
        report["raw_counter_failures"] = [f"{key} grew by {value}" for key, value in raw.items()
                                            if value and key not in PROGRESS]
        clean = not report["failures"] and not report["restore_failures"] and not report["raw_counter_failures"]
        if args.seconds == MAX_SECONDS and not report["memory_gate"]["passed"]:
            report["failures"].append("eight-hour memory gate insufficient or failed")
            clean = False
        report["core_soak_qualified"] = clean and args.seconds == MAX_SECONDS
        report["status"] = ("PASS_CORE_SOAK" if report["core_soak_qualified"] else "PASS_SHORT_RUN") if clean else "FAIL"
        report["system_sample_count"] = len(soak.system_samples)
        if soak.system_samples:
            report["min_mem_available_kib"] = min(x["mem_available_kib"] for x in soak.system_samples)
        try:
            evidence.write("session.ndjson", report)
        except Exception as exc:
            report["failures"].append(f"final evidence stream failed: {exc}")
            report["status"], report["core_soak_qualified"] = "FAIL", False
        finally:
            evidence.close()
        raw = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
        require(len(raw.encode()) <= 4*1024*1024, "final report cap exceeded")
        with exclusive_text(output / "core-soak.result.json") as handle:
            handle.write(raw)
    return 0 if report["status"].startswith("PASS_") else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("driver", "instance-root", "output-dir", "pad-sha256"):
        parser.add_argument("--"+name, required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--seconds", type=int, required=True)
    args = parser.parse_args(argv)
    async def supervised():
        loop, task = asyncio.get_running_loop(), asyncio.current_task()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            return await run(args)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
    try:
        return asyncio.run(supervised())
    except (KeyboardInterrupt, asyncio.CancelledError, RuntimeError, OSError, ValueError, TimeoutError) as exc:
        print(f"core soak refused/failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
