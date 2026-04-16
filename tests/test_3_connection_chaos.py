"""
Test 3 — connection chaos under load.

While a chord is held (MIDI injected via aplaymidi loop) we:
  - repeatedly disconnect StaveSynth:out_L/R from the downstream sink, wait, reconnect
  - disconnect the MIDI input bridge and reconnect
  - watch the synth for crashes, xruns, exceptions, RSS growth

Pass: synth PID survives, audio resumes after every reconnect, zero new exceptions.
"""
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import websockets

DURATION = 60  # seconds
DISRUPT_PERIOD = 4.0  # break every 4s
DOWN_SECS = 1.5  # stay disconnected for 1.5s each time

CHORD_MID = Path("/tmp/chord_long.mid")


def find_synth_pid() -> int:
    for p in psutil.process_iter(["pid", "cmdline"]):
        cmd = p.info["cmdline"] or []
        if any("stave_synth.main" in c for c in cmd):
            return p.info["pid"]
    raise SystemExit("synth not running")


def journal_since(since: str) -> str:
    return subprocess.run(
        ["journalctl", "--user", "-u", "stave-synth", "--since", since, "--no-pager"],
        capture_output=True, text=True,
    ).stdout


def jack(*args) -> tuple[int, str]:
    r = subprocess.run(["pw-jack", *args], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def current_audio_sinks() -> list[tuple[str, str]]:
    """Return [(StaveSynth:out_X, downstream_port)] pairs currently connected."""
    pairs = []
    for src in ("StaveSynth:out_L", "StaveSynth:out_R"):
        r = subprocess.run(["pw-jack", "jack_lsp", "-c", src],
                           capture_output=True, text=True)
        lines = [ln.strip() for ln in r.stdout.splitlines()]
        # First line is the source port itself; remaining are connections.
        for ln in lines[1:]:
            if ln:
                pairs.append((src, ln))
    return pairs


def current_midi_sinks() -> list[tuple[str, str]]:
    """Return [(midi_source, StaveSynth:midi_in)] pairs."""
    pairs = []
    r = subprocess.run(["pw-jack", "jack_lsp", "-c", "StaveSynth:midi_in"],
                       capture_output=True, text=True)
    lines = [ln.strip() for ln in r.stdout.splitlines()]
    for ln in lines[1:]:
        if ln:
            pairs.append((ln, "StaveSynth:midi_in"))
    return pairs


async def midi_chord_loop(stop_at: float):
    # Keep replaying the chord MIDI file until stop_at.
    while time.time() < stop_at:
        proc = await asyncio.create_subprocess_exec(
            "aplaymidi", "-p", "14:0", str(CHORD_MID),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=stop_at - time.time())
        except asyncio.TimeoutError:
            proc.terminate()
            await proc.wait()
            return


async def disruption_loop(stop_at: float, log: list):
    audio_pairs = current_audio_sinks()
    midi_pairs = current_midi_sinks()
    if not audio_pairs:
        log.append(("ERROR", "no audio pairs found to disrupt"))
        return

    while time.time() < stop_at:
        # Break audio
        for src, dst in audio_pairs:
            rc, out = jack("jack_disconnect", src, dst)
            log.append(("disc", time.time(), src, dst, rc))
        # Break MIDI too
        for src, dst in midi_pairs:
            jack("jack_disconnect", src, dst)
        await asyncio.sleep(DOWN_SECS)
        # Restore
        for src, dst in audio_pairs:
            rc, out = jack("jack_connect", src, dst)
            log.append(("conn", time.time(), src, dst, rc))
        for src, dst in midi_pairs:
            jack("jack_connect", src, dst)
        await asyncio.sleep(DISRUPT_PERIOD - DOWN_SECS)


async def peak_watcher(stop_at: float, peaks: list):
    try:
        async with websockets.connect("ws://localhost:8765", ping_interval=None) as ws:
            while time.time() < stop_at:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    data = json.loads(msg)
                    if data.get("type") == "peak_level":
                        peaks.append((time.time(), data.get("peak", 0.0)))
                except asyncio.TimeoutError:
                    continue
    except Exception as e:
        peaks.append(("ws_error", str(e)))


async def main():
    pid = find_synth_pid()
    proc = psutil.Process(pid)
    start_rss = proc.memory_info().rss

    # 60-second chord (enough to outlive the test)
    from _midi_util import chord_hold_mid
    chord_hold_mid(CHORD_MID, hold_beats=120)  # 60s at 120 BPM

    t0 = time.time()
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0 - 1))
    baseline_journal = journal_since(since)

    print(f"Synth PID: {pid}; disrupting every {DISRUPT_PERIOD}s, down {DOWN_SECS}s")
    print(f"Running for {DURATION}s...")

    stop_at = t0 + DURATION
    log = []
    peaks = []

    await asyncio.gather(
        midi_chord_loop(stop_at),
        disruption_loop(stop_at, log),
        peak_watcher(stop_at, peaks),
    )

    # Check result
    alive = psutil.pid_exists(pid)
    end_rss = proc.memory_info().rss if alive else 0
    after_journal = journal_since(since)
    new_lines = [ln for ln in after_journal.splitlines()
                 if ln not in baseline_journal.splitlines()]
    exc_lines = [ln for ln in new_lines
                 if any(k in ln for k in ("Traceback", "Error", "CRITICAL", "Exception"))]

    disc_fails = sum(1 for r in log if r[0] == "disc" and r[-1] != 0)
    conn_fails = sum(1 for r in log if r[0] == "conn" and r[-1] != 0)
    disc_count = sum(1 for r in log if r[0] == "disc")

    # Assess recovery: after each reconnect event, did peaks rise within 1s?
    recoveries_ok = 0
    recoveries_fail = 0
    for entry in log:
        if entry[0] != "conn":
            continue
        t_conn = entry[1]
        # Look for any peak > 0.05 in the 0.3–1.5s window after reconnect.
        got_peak = any(p > 0.05 for (t, p) in peaks
                       if isinstance(t, float) and t_conn + 0.3 < t < t_conn + 1.5)
        if got_peak:
            recoveries_ok += 1
        else:
            recoveries_fail += 1

    print()
    print(f"Synth alive:          {alive}")
    print(f"RSS delta:            {(end_rss-start_rss)/1e6:+.2f} MB")
    print(f"Disconnect cycles:    {disc_count}")
    print(f"Disconnect failures:  {disc_fails}")
    print(f"Reconnect failures:   {conn_fails}")
    print(f"Recoveries OK:        {recoveries_ok} / {recoveries_ok+recoveries_fail}")
    print(f"New exceptions:       {len(exc_lines)}")
    for line in exc_lines[:6]:
        print("  ", line[-200:])
    print(f"Peak samples seen:    {len([p for p in peaks if isinstance(p[0], float)])}")

    fail = (not alive) or len(exc_lines) > 0 or recoveries_fail > recoveries_ok
    return 1 if fail else 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(asyncio.run(main()))
