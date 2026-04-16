"""
Test 2 — parameter flood.

Slam the WebSocket with fader/alt/toggle messages at max rate for DURATION
seconds. Watch:
  - synth process RSS (leak detector)
  - synth process CPU%
  - journal xrun count (audio dropouts)
  - websocket ack throughput
  - exceptions in the stave-synth journal

Pass criteria:
  - RSS growth < 10 MB over the run
  - zero fatal exceptions
  - websocket never hangs / drops
"""
import asyncio
import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path

import psutil
import websockets

DURATION = 60  # seconds
URL = "ws://localhost:8765"

FADER_IDS = [0, 1, 2, 3, 4]
TOGGLES = ["shimmer_toggle", "freeze_toggle", "drone_toggle"]


def find_synth_pid() -> int:
    for p in psutil.process_iter(["pid", "cmdline"]):
        cmd = p.info["cmdline"] or []
        if any("stave_synth.main" in c for c in cmd):
            return p.info["pid"]
    raise SystemExit("synth process not found")


def journal_xrun_count(since: str) -> int:
    out = subprocess.run(
        ["journalctl", "--user", "-u", "stave-synth", "--since", since, "--no-pager"],
        capture_output=True, text=True,
    ).stdout
    return len(re.findall(r"xrun|XRUN|underrun", out, re.IGNORECASE))


def journal_exceptions(since: str) -> list[str]:
    out = subprocess.run(
        ["journalctl", "--user", "-u", "stave-synth", "--since", since, "--no-pager"],
        capture_output=True, text=True,
    ).stdout
    hits = []
    for line in out.splitlines():
        if "Traceback" in line or "Error" in line or "CRITICAL" in line:
            hits.append(line.strip())
    return hits


async def flood(ws, stop_at: float, stats: dict, rate_per_sec: int = 800):
    # Keep alt_state so we exercise every alt mode in rotation.
    # 800 msg/s is ~4x the worst realistic UI rate (touchscreen drag at 60fps
    # × 4 concurrent faders), a meaningful stress without starving asyncio.
    alt = {i: 0 for i in FADER_IDS}
    period = 1.0 / rate_per_sec
    next_tick = time.time()
    while time.time() < stop_at:
        r = random.random()
        if r < 0.70:
            fid = random.choice(FADER_IDS)
            msg = {"type": "fader", "id": fid, "value": random.random(), "alt": alt[fid]}
        elif r < 0.85:
            fid = random.choice(FADER_IDS)
            alt[fid] = random.randint(0, 2)
            msg = {"type": "alt_toggle", "id": fid, "alt": alt[fid]}
        elif r < 0.95:
            msg = {"type": random.choice(TOGGLES), "enabled": random.choice([True, False])}
        else:
            msg = {"type": "panic"}
        await ws.send(json.dumps(msg))
        stats["sent"] += 1
        next_tick += period
        delay = next_tick - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            await asyncio.sleep(0)  # yield so drain/ping can run
            next_tick = time.time()


async def drain(ws, stop_at: float, stats: dict):
    while time.time() < stop_at:
        try:
            await asyncio.wait_for(ws.recv(), timeout=0.5)
            stats["recv"] += 1
        except asyncio.TimeoutError:
            continue
        except websockets.ConnectionClosed:
            stats["closed"] = True
            return


async def sample_rss(pid: int, stop_at: float, samples: list):
    p = psutil.Process(pid)
    p.cpu_percent(interval=None)  # prime
    while time.time() < stop_at:
        try:
            rss = p.memory_info().rss
            cpu = p.cpu_percent(interval=None)
            samples.append((time.time(), rss, cpu))
        except psutil.NoSuchProcess:
            return
        await asyncio.sleep(1.0)


async def main():
    pid = find_synth_pid()
    print(f"Synth PID: {pid}")

    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 2))
    baseline_xrun = journal_xrun_count(since)
    baseline_exc = journal_exceptions(since)
    print(f"Baseline xruns in last 2s window: {baseline_xrun}")

    p = psutil.Process(pid)
    start_rss = p.memory_info().rss

    stats = {"sent": 0, "recv": 0, "closed": False}
    samples = []
    stop_at = time.time() + DURATION

    t0 = time.time()
    since_flood = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0 - 1))

    async with websockets.connect(URL, max_size=None, ping_interval=None) as ws:
        await asyncio.gather(
            flood(ws, stop_at, stats),
            drain(ws, stop_at, stats),
            sample_rss(pid, stop_at, samples),
        )

    end_rss = p.memory_info().rss
    xruns = journal_xrun_count(since_flood)
    exceptions = journal_exceptions(since_flood)
    new_exceptions = [e for e in exceptions if e not in baseline_exc]

    duration = time.time() - t0
    print(f"Ran for {duration:.1f}s")
    print(f"Messages sent:       {stats['sent']} ({stats['sent']/duration:.0f}/s)")
    print(f"Messages received:   {stats['recv']}")
    print(f"Connection closed:   {stats['closed']}")
    print(f"RSS start:           {start_rss/1e6:.1f} MB")
    print(f"RSS end:             {end_rss/1e6:.1f} MB")
    print(f"RSS delta:           {(end_rss-start_rss)/1e6:+.2f} MB")
    if samples:
        rss_min = min(s[1] for s in samples)
        rss_max = max(s[1] for s in samples)
        cpu_max = max(s[2] for s in samples)
        cpu_avg = sum(s[2] for s in samples) / len(samples)
        print(f"RSS range:           {rss_min/1e6:.1f} – {rss_max/1e6:.1f} MB")
        print(f"CPU avg/max:         {cpu_avg:.1f}% / {cpu_max:.1f}%")
    print(f"XRUNs during flood:  {xruns}")
    print(f"New exceptions:      {len(new_exceptions)}")
    for e in new_exceptions[:8]:
        print("  ", e)

    fail = stats["closed"] or len(new_exceptions) > 0 or (end_rss - start_rss) > 10_000_000
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
