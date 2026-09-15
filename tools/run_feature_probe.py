#!/usr/bin/env python3
"""Bounded recorder -> sampled bed probe for the private audition instance.

Run only after independently preparing an isolated audition runtime. There are
deliberately no host/port, slot-overwrite, or deletion options. Created takes
and the C pad are retained as evidence. A fresh connection checks protocol
hydration, not Safari/Wi-Fi recovery. This is not full stage qualification.
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
import struct
import sys
import time

import websockets

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.run_audition import (
    CommandSocket, DURATION_SECONDS, INSTANCE, WS_URL, _write_json_exclusive,
    case_commands, counter_growth, health_failures, render_schedule,
    restore_commands, run_driver, send_command, setting, verify_runtime,
    verify_state,
)


NOTE = 60
WORK_TIMEOUT = 90.0
CLEANUP_TIMEOUT = 25.0
CASE = "02-warm-layers"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def build_schedule():
    """A settled Cadd9 slice at 6-12 s, then continuing matching chords."""
    events = []
    for onset, release in ((2.0, 18.0), (19.0, 25.0), (26.0, 32.0),
                           (33.0, 39.0), (40.0, 46.0)):
        events.append((onset, 0xB0, 64, 127))
        for index, note in enumerate((48, 55, 60, 62, 64)):
            events.append((onset, 0x90, note, 66 if index == 0 else 72))
            events.append((release, 0x80, note, 0))
        events.append((release + 0.15, 0xB0, 64, 0))
    events.extend(((47.0, 0xB0, 66, 0), (47.0, 0xB0, 64, 0),
                   (47.0, 0xB0, 123, 0)))
    return sorted(events, key=lambda item: item[0])


def pad_slot(response, *, empty=False, active=None):
    slots = response.get("slots")
    require(isinstance(slots, list) and len(slots) == 12,
            "missing or malformed 12-slot inventory")
    require(all(isinstance(slot, dict) and type(slot.get("note")) is int
                for slot in slots), "malformed slot identity")
    require(sorted(slot["note"] for slot in slots) == list(range(60, 72)),
            "duplicate or missing pad-slot identities")
    slot = next(slot for slot in slots if slot["note"] == NOTE)
    for key in ("loaded", "active", "file_present", "preparing"):
        require(type(slot.get(key)) is bool, f"C-slot {key} evidence missing")
    require(not slot.get("error") and not slot["preparing"], "C slot has an error or pending preparation")
    if empty:
        require(not any(slot[key] for key in ("loaded", "active", "file_present")),
                "C slot is occupied; refusing to overwrite any sample")
    else:
        require(slot["loaded"] and slot["file_present"], "C slot is not actually resident and stored")
        require(type(slot.get("memory_bytes")) is int and slot["memory_bytes"] > 0,
                "C slot has no resident sample memory")
        require(type(slot.get("duration_seconds")) in (int, float)
                and math.isfinite(slot["duration_seconds"]) and slot["duration_seconds"] > 0,
                "C slot has no finite duration")
        if active is not None:
            require(slot["active"] is active, f"C-slot active state is not {active}")
    return slot


def recording_names(response):
    takes = response.get("takes")
    require(isinstance(takes, list), "recording inventory unavailable")
    require(all(isinstance(take, dict) and isinstance(take.get("filename"), str)
                for take in takes), "malformed recording inventory")
    return {take["filename"] for take in takes}


def validate_take(take, filename, *, listed=False):
    require(isinstance(take, dict) and take.get("filename") == filename,
            "completed take identity mismatch")
    require(take.get("status") == "complete" and take.get("metadata_version") == 1,
            "take is not a verified complete recording")
    for key in ("complete", "finalized", "audio_synced", "metadata_saved", "state_saved"):
        require(take.get(key) is True, f"take {key} not verified")
    for key in ("recording", "writer_pending"):
        require(take.get(key) is False, f"take {key} still active or unknown")
    for key in ("dropped_blocks", "dropped_frames", "write_errors", "stop_timeouts"):
        require(type(take.get(key)) is int and take[key] == 0, f"take {key} not zero")
    require(take.get("errors") == [] and not take.get("error"), "take reports errors")
    require(type(take.get("sample_rate")) is int and take["sample_rate"] == 48000,
            "unexpected recording sample rate")
    require(all(type(take.get(key)) is int for key in ("frames", "input_frames", "accepted_frames")),
            "take frame accounting missing")
    frames = take["frames"]
    require(frames == take["input_frames"] == take["accepted_frames"]
            and 4 * 48000 <= frames <= 9 * 48000, "take is truncated or has unexpected duration")
    if listed:
        require(type(take.get("size_bytes")) is int and take["size_bytes"] >= 44 + frames * 4,
                "listed recording WAV payload is truncated")
        require(take.get("has_state") is True, "recording state sidecar missing")


def validate_capture(report, path, event_count):
    """Validate the native driver's exact float-WAV format with bounded reads."""
    require(report.get("returncode") == 0 and not report.get("failure"), "native driver failed")
    require(report.get("stdout_truncated") is False and report.get("stderr_truncated") is False,
            "native driver diagnostics missing or truncated")
    complete = report.get("events", {}).get("capture_complete", {})
    for key in ("nonfinite", "xruns", "errors"):
        require(type(complete.get(key)) is int and complete[key] == 0, f"capture {key} not verified zero")
    frames = int(DURATION_SECONDS * 48000)
    require(complete.get("sample_rate_hz") == 48000 and complete.get("block_frames") == 512,
            "unexpected capture graph profile")
    require(complete.get("frames") == frames and complete.get("midi_events") == event_count,
            "capture duration or MIDI event count incomplete")
    require(path.stat().st_size == 58 + frames * 8, "native WAV payload length mismatch")
    peak = 0.0
    with path.open("rb") as handle:
        header = handle.read(58)
        require(header[:4] == b"RIFF" and header[8:16] == b"WAVEfmt "
                and struct.unpack_from("<IHHIIHHH", header, 16)
                == (18, 3, 2, 48000, 384000, 8, 32, 0)
                and header[38:42] == b"fact" and header[50:54] == b"data"
                and struct.unpack_from("<I", header, 54)[0] == frames * 8,
                "unexpected native float-stereo WAV header")
        while chunk := handle.read(16384 * 8):
            for left, right in struct.iter_unpack("<ff", chunk):
                require(math.isfinite(left) and math.isfinite(right), "nonfinite sample in captured WAV")
                peak = max(peak, abs(left), abs(right))
    require(peak > 0, "captured output is entirely silent")
    return {"frames": frames, "sample_rate": 48000, "channels": 2,
            "all_samples_finite": True, "peak": peak,
            "limitation": "Mixed-output validity does not isolate bed audibility or certify tone/analogue latency."}


class Probe:
    def __init__(self, report, ws):
        self.report = report
        self.ws = ws
        self.anchor = None
        self.filename = None
        self.recording_may_be_active = False

    async def command(self, label, message, reply_type=None, *, ws=None):
        socket = self.ws if ws is None else ws
        entry = {"label": label, "command": message, "sent_monotonic_ns": time.monotonic_ns()}
        self.report["timeline"].append(entry)
        try:
            if reply_type:
                reply = await socket.request(message, lambda item: item.get("type") == reply_type)
                require(not reply.get("error"), f"{label}: {reply.get('error')}")
            else:
                reply = await send_command(socket, message)
            entry["reply"] = reply
            return reply
        except BaseException as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            entry["finished_monotonic_ns"] = time.monotonic_ns()

    async def at(self, seconds, *, latest=None):
        loop = asyncio.get_running_loop()
        await asyncio.sleep(max(0.0, self.anchor + seconds - loop.time()))
        require(latest is None or loop.time() <= self.anchor + latest,
                "automation missed its musical window; refusing late gesture")

    async def state(self, label, *, faded_out=None, drone_on=None, ws=None):
        reply = await self.command(label, {"type": "get_state"}, ws=ws)
        if faded_out is not None:
            require(reply.get("drone_faded_out") is faded_out, "bed fade target was not authoritative")
        if drone_on is not None:
            synth = reply.get("state", {}).get("synth_pad", {})
            require(synth.get("drone_enabled") is drone_on, "bed enable state mismatch")
            require(synth.get("drone_key") == (NOTE if drone_on else None), "bed key state mismatch")
        return reply

    async def automate(self, ws, case, anchor):
        self.anchor = anchor
        self.report["automation_anchor_monotonic_ns"] = round(anchor * 1_000_000_000)
        self.report["automation_anchor_kind"] = "stdout_receive_loop_monotonic_not_exact_audio_frame"
        await self.at(6.0, latest=7.0)
        self.recording_may_be_active = True  # A lost acknowledgement must still trigger cleanup checks.
        start = await self.command("record_on", {"type": "record_toggle"}, "record_ack")
        require(start.get("recording") is True, "recorder did not start")
        self.filename = start.get("take", {}).get("filename")
        require(isinstance(self.filename, str) and Path(self.filename).name == self.filename
                and self.filename.endswith(".wav")
                and self.filename not in self.report["original_recording_names"], "new take identity not verified")
        self.report["created_recording"] = self.filename
        await self.at(12.0, latest=13.0)
        stop = await self.command("record_off", {"type": "record_toggle"}, "record_ack")
        require(stop.get("recording") is False, "recorder stop was not acknowledged")
        # Never toggle a second time to 'retry' stop: that could start another take.
        for attempt in range(16):
            state = await self.state(f"record_finalize_{attempt}")
            current = state.get("record_status", {})
            require(current.get("filename") == self.filename, "recorder changed ownership during probe")
            if current.get("recording") is False and current.get("writer_pending") is False:
                validate_take(current, self.filename)
                self.recording_may_be_active = False
                break
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError("recording did not finalize within bounded polling window")
        recordings = await self.command("completed_recordings", {"type": "list_recordings"}, "recordings_list")
        recording_names(recordings)
        matches = [item for item in recordings["takes"] if item["filename"] == self.filename]
        require(len(matches) == 1, "completed take missing or duplicated in recording inventory")
        validate_take(matches[0], self.filename, listed=True)
        self.report["completed_take"] = matches[0]
        await self.at(14.0, latest=20.0)
        pad_slot(await self.command("recheck_empty_slot", {"type": "list_pad_slots"}, "pad_slots"), empty=True)
        saved = await self.command("save_recording_to_C", {"type": "save_to_pad_slot",
                                   "source": self.filename, "note": NOTE}, "pad_slot_saved")
        require(saved.get("note") == NOTE and not saved.get("warnings"), "pad save identity/warnings require review")
        pad_slot(saved, active=False)
        self.report["created_pad_note"] = NOTE
        await self.at(22.0, latest=24.0)
        triggered = await self.command("sample_trigger", {"type": "drone_key", "note": NOTE}, "drone_key_ack")
        require(triggered.get("source") == "sample" and triggered.get("enabled") is True
                and triggered.get("note") == NOTE, "actual sample trigger not acknowledged")
        pad_slot(await self.command("active_slot", {"type": "list_pad_slots"}, "pad_slots"), active=True)
        await self.state("active_bed_state", faded_out=False, drone_on=True)
        await self.at(30.0, latest=31.0)
        out = await self.command("fade_out", {"type": "drone_fade", "duration_s": 2.0}, "drone_fade_ack")
        require(out.get("faded_out") is True, "fade-out target not acknowledged")
        reverse = await self.command("immediate_reverse", {"type": "drone_fade", "duration_s": 2.0}, "drone_fade_ack")
        require(reverse.get("faded_out") is False, "early fade reversal did not reverse target")
        gestures = self.report["timeline"][-2:]
        require(gestures[1]["sent_monotonic_ns"] - gestures[0]["sent_monotonic_ns"] < 500_000_000,
                "reversal arrived too late to exercise the early-ramp target bug")
        await self.state("reversed_bed_state", faded_out=False, drone_on=True)
        await self.at(33.0, latest=35.0)
        out = await self.command("absolute_fade_out", {"type": "drone_fade", "duration_s": 0.5,
                                                       "faded_out": True}, "drone_fade_ack")
        require(out.get("faded_out") is True, "absolute fade request was not honored")
        # A genuinely separate socket, never a queued/replayed one-shot command.
        await verify_runtime()
        async with websockets.connect(WS_URL, open_timeout=3, close_timeout=2,
                                      max_size=4 * 1024 * 1024, max_queue=16) as raw:
            fresh = await CommandSocket(raw).start()
            try:
                await self.state("fresh_connection_hydration", faded_out=True, drone_on=True, ws=fresh)
            finally:
                await fresh.stop()
        back = await self.command("absolute_fade_in", {"type": "drone_fade", "duration_s": 0.5,
                                                      "faded_out": False}, "drone_fade_ack")
        require(back.get("faded_out") is False, "absolute fade-in not honored")
        await self.at(46.5, latest=47.0)
        self.report["pre_panic_state"] = await self.state("pre_panic_state", faded_out=False, drone_on=True)
        self.report["pre_panic_debug"] = await self.command("pre_panic_debug", {"type": "debug"})
        await self.at(48.0, latest=49.0)
        await self.command("intentional_terminal_panic", {"type": "panic"})
        await asyncio.sleep(0.2)
        self.report["post_panic_state"] = await self.state("post_panic_state", faded_out=False, drone_on=False)
        require(self.report["post_panic_state"].get("faded_out") is False
                and self.report["post_panic_state"]["state"]["synth_pad"].get("freeze_enabled") is False,
                "panic did not clear master fade/freeze state")
        pad_slot(await self.command("post_panic_slot", {"type": "list_pad_slots"}, "pad_slots"), active=False)
        self.report["feature_sequence_complete"] = True
        return self.report["timeline"]


async def cleanup(probe, restore):
    report = probe.report
    errors = []
    report["restore_failures"] = errors  # Retain partial cleanup evidence on timeout/cancellation.
    if probe.recording_may_be_active:
        try:
            current = (await probe.state("cleanup_record_status")).get("record_status", {})
            require(type(current.get("recording")) is bool, "cleanup recorder status unavailable")
            if current["recording"]:
                require(probe.filename is not None and current.get("filename") == probe.filename,
                        "cannot safely stop unidentified recording after lost start acknowledgement")
                await probe.command("cleanup_stop_owned_take", {"type": "record_toggle"}, "record_ack")
        except Exception as exc:
            errors.append(f"recorder cleanup: {type(exc).__name__}: {exc}")
    try:
        await probe.command("cleanup_panic", {"type": "panic"})
    except Exception as exc:
        errors.append(f"panic cleanup: {type(exc).__name__}: {exc}")
    for command in restore:
        try:
            await send_command(probe.ws, command)
        except Exception as exc:
            errors.append(f"restore {command['section']}.{command['param']}: {type(exc).__name__}: {exc}")
    try:
        restored = await probe.state("restored_state", faded_out=False, drone_on=False)
        report["restored_state"] = restored
        errors.extend(verify_state(restored, restore))
        require(restored.get("record_status", {}).get("recording") is False
                and restored.get("record_status", {}).get("writer_pending") is False,
                "recorder remains active/pending after cleanup")
    except Exception as exc:
        errors.append(f"restore verification: {type(exc).__name__}: {exc}")


async def session(driver, output_dir, report):
    report["runtime"] = await verify_runtime()
    async with websockets.connect(WS_URL, open_timeout=5, close_timeout=3,
                                  max_size=4 * 1024 * 1024, max_queue=16) as raw:
        ws = await CommandSocket(raw).start()
        probe = Probe(report, ws)
        mutated = False
        restore = []
        try:
            original_response = await probe.state("original_state")
            report["original_state"] = copy.deepcopy(original_response)
            _write_json_exclusive(output_dir / "00-original-state.json", original_response)
            require(not health_failures(original_response), "audition preflight health failed: " + "; ".join(health_failures(original_response)))
            original = copy.deepcopy(original_response["state"])
            require(original_response.get("faded_out") is False
                    and original_response.get("drone_faded_out") is False, "baseline has an active/unknown fade")
            require(original["piano"].get("soundfont") == "Fluid"
                    and original["synth_pad"].get("reverb_type") == "wash", "unexpected baseline piano/reverb")
            require(original["synth_pad"].get("freeze_enabled") is False
                    and original["synth_pad"].get("drone_enabled") is False
                    and original["synth_pad"].get("drone_key") is None, "baseline freeze/bed is not idle")
            rec = original_response.get("record_status", {})
            require(rec.get("recording") is False and rec.get("writer_pending") is False, "recorder is not idle")
            audio = original_response["health"]["audio"]
            require(audio.get("sample_rate") == 48000 and audio.get("block_frames") == 512,
                    "probe requires the existing 48 kHz / 512-frame profile")
            slots = await probe.command("original_slots", {"type": "list_pad_slots"}, "pad_slots")
            pad_slot(slots, empty=True)
            require(all(slot.get("active") is False and slot.get("preparing") is False for slot in slots["slots"]),
                    "another pad is active/preparing")
            report["original_slots"] = slots
            recordings = await probe.command("original_recordings", {"type": "list_recordings"}, "recordings_list")
            report["original_recordings"] = recordings
            report["original_recording_names"] = sorted(recording_names(recordings))
            restore = restore_commands(original) + [setting("synth_pad", "drone_level", original["synth_pad"]["drone_level"])]
            setup = case_commands(CASE, original) + [setting("synth_pad", "drone_level", 0.4)]
            report["setup_commands"] = setup
            report["restore_commands"] = restore
            schedule = render_schedule(build_schedule())
            schedule_path = output_dir / "midi_schedule.txt"
            with schedule_path.open("x", encoding="ascii") as handle:
                handle.write(schedule)
            report["schedule_sha256"] = hashlib.sha256(schedule.encode("ascii")).hexdigest()
            report["schedule_events"] = len(build_schedule())
            await verify_runtime()  # Recheck identity immediately before the first mutation.
            mutated = True
            await probe.command("setup_panic", {"type": "panic"})
            for command in setup:
                await send_command(ws, command)
            await asyncio.sleep(1.0)
            before = await probe.state("verified_setup")
            report["verified_state"] = before
            require(not verify_state(before, setup), "setup did not match acknowledged settings")
            require(not health_failures(before), "setup health failed: " + "; ".join(health_failures(before)))
            before_debug = await probe.command("before_debug", {"type": "debug"})
            report["before_debug"] = before_debug
            report["driver"] = await run_driver(driver, schedule_path, output_dir / "feature-probe.wav",
                                                ws, CASE, automation=probe.automate)
            after = await probe.state("capture_after")
            after_debug = await probe.command("after_debug", {"type": "debug"})
            report["after_state"] = after
            report["after_debug"] = after_debug
            report["failures"].extend(health_failures(after))
            report["failures"].extend(counter_growth(before, after, before_debug, after_debug))
            if "pre_panic_state" in report:
                report["counter_growth_before_intentional_panic"] = counter_growth(
                    before, report["pre_panic_state"], before_debug, report["pre_panic_debug"])
            require(report.get("feature_sequence_complete") is True, "feature sequence did not complete")
            report["capture_validation"] = validate_capture(report["driver"], output_dir / "feature-probe.wav", report["schedule_events"])
        finally:
            try:
                if mutated:
                    try:
                        await asyncio.wait_for(cleanup(probe, restore), CLEANUP_TIMEOUT)
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
    report = {"format": 1, "instance": INSTANCE, "websocket_url": WS_URL,
              "status": "FAIL", "failures": [], "timeline": [],
              "duration_seconds": DURATION_SECONDS, "piano_telemetry_required": True,
              "limits": ["No hardware/analogue latency or subjective quality qualification.",
                         "No browser sleep/Wi-Fi fault test; only fresh WebSocket hydration.",
                         "Fade acknowledgements/state verify the target, not sample-by-sample ramp gain.",
                         "Counters include setup-adjacent capture and deliberate terminal panic; none are suppressed.",
                         "Private take and pad retained; no deletion and no existing-slot overwrite.",
                         "No cross-client atomic no-overwrite API; isolated exclusive operator required."]}
    try:
        await asyncio.wait_for(session(driver, output_dir, report), WORK_TIMEOUT)
    except BaseException as exc:
        report["failures"].append(f"{type(exc).__name__}: {exc}")
        if not isinstance(exc, Exception):
            raise
    finally:
        if not report["failures"] and not report.get("restore_failures") and report.get("feature_sequence_complete"):
            report["status"] = "PASS"
        _write_json_exclusive(output_dir / "feature-probe.result.json", report)
    return 0 if report["status"] == "PASS" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", required=True, help="compiled exact-instance native_audition driver")
    parser.add_argument("--output-dir", required=True, help="new non-existing evidence directory")
    try:
        return asyncio.run(run(parser.parse_args(argv)))
    except (KeyboardInterrupt, RuntimeError, OSError, TimeoutError) as exc:
        print(f"feature probe refused/failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
