#!/usr/bin/env python3
"""Explicit bounded muted live-callback probe of ONLY native-v2 audition.

No WAV recording, production runtime, service changes or device fallback.
Requires an already-running isolated owner and an explicit compiled MIDI peer.
"""
import argparse
import ipaddress
import json
import math
from pathlib import Path
import subprocess
import time
import urllib.request
import urllib.parse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--midi-probe", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-muted-live-test", action="store_true")
    args = parser.parse_args()
    url = urllib.parse.urlsplit(args.url)
    try:
        address = ipaddress.ip_address(url.hostname or "")
    except ValueError:
        parser.error("Use an explicit private IP address")
    if (not args.allow_muted_live_test or url.scheme != "http" or not url.hostname or
            not address.is_private or address.is_unspecified or address.is_multicast or
            url.port not in range(8082, 8091) or url.path or url.query or url.fragment or url.username or not args.midi_probe.is_file()):
        parser.error("Explicit isolated URL8082..8090, live opt-in and existing MIDI peer required")
    out = args.output_dir.resolve(); out.mkdir(mode=0o700, exist_ok=False)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    report = {"status": "started", "scope": "muted physical-driver callback test; not player or analog-latency acceptance", "samples": [], "commands": []}
    def get():
        with opener.open(args.url + "/status", timeout=2) as response:
            data = json.load(response)
        if (data.get("instance") != "native-v2-audition" or data.get("stale") or data.get("exited") is not None or
                data["status"].get("fault") != 0 or data["status"].get("frames") != 512 or not data["status"].get("routed")):
            raise RuntimeError("Isolated512 owner not healthy/routed/advancing")
        if data["values"].get("master") != 0:
            raise RuntimeError("Muted-only probe refused: master is not zero")
        return data
    def set_control(key, value):
        if key == "master": raise RuntimeError("Probe may never open master startup gate")
        request = urllib.request.Request(args.url + "/control", data=json.dumps({"key": key, "value": value}).encode(),
                                         headers={"Content-Type": "application/json", "Origin": args.url}, method="POST")
        with opener.open(request, timeout=2) as response:
            data = json.load(response)
        if not data.get("queued"): raise RuntimeError("Control not queued")
        report["commands"].append({"key": key, "value": value, "queued": data["queued"]})
    child = None
    try:
        report["before"] = get()
        child = subprocess.Popen(["/usr/bin/pw-jack", str(args.midi_probe.resolve()), "--allow-isolated-live-midi"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        start = time.monotonic(); phase = 0
        while time.monotonic() - start < 100:
            elapsed = time.monotonic() - start
            sample = get(); sample["elapsed"] = elapsed
            thermal = Path("/sys/class/thermal/thermal_zone0/temp")
            sample["temperature"] = float(thermal.read_text()) / 1000 if thermal.exists() else None
            if sample["temperature"] is not None and sample["temperature"] >= 80:
                raise RuntimeError("Thermal guard80C")
            report["samples"].append(sample)
            if elapsed >= 20 and phase == 0:
                set_control("osc1", .6); set_control("osc2", .4); phase = 1
            if elapsed >= 40 and phase == 1:
                for key, value in (("reverb", 1), ("shimmer", 1), ("shimmer_mix", .5), ("delay_wet", .3), ("piano_reverb", .2)):
                    set_control(key, value)
                phase = 2
            if elapsed >= 42:
                set_control("cutoff", 4500 + 3800 * math.sin(elapsed * .6))
            if child.poll() is not None:
                break
            time.sleep(.3)
        if child.poll() is None: raise RuntimeError("MIDI peer timed out")
        stdout, stderr = child.communicate(timeout=2)
        report["peer"] = dict(exit=child.returncode, stdout=stdout, stderr=stderr)
        peer = json.loads(stdout)
        if child.returncode or not peer.get("complete") or peer.get("chords") != 45 or peer.get("midi_events") != 857:
            raise RuntimeError("Incomplete MIDI stimulus")
        # Allow the last telemetry snapshot to include peer's terminal release.
        time.sleep(1.2)
        report["after"] = get()
        before, after = report["before"]["status"], report["after"]["status"]
        report["delta"] = {key: after[key] - before[key] for key in ("blocks", "notes", "xruns", "over_budget", "piano_full_scale", "unsupported_midi")}
        report["duration_seconds"] = time.monotonic() - start
        d = report["delta"]
        if d["notes"] != 360 or d["blocks"] < 8000 or d["unsupported_midi"]:
            raise RuntimeError("MIDI/block accounting mismatch; no player input expected")
        if d["xruns"] or d["over_budget"]:
            raise RuntimeError("Strict live timing gate failed")
        report["status"] = "passed_muted_callback_probe_not_player_qualification"
    except Exception as error:
        report.update(status="failed", error=str(error))
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try: child.wait(timeout=3)
            except subprocess.TimeoutExpired: child.kill(); child.wait()
        if child is not None:
            child.stdout.close(); child.stderr.close()
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("status", "error", "delta", "duration_seconds") if k in report}), flush=True)
    return 0 if report["status"].startswith("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
