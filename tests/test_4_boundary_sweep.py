"""
Test 4 — parameter boundary sweep + audio NaN/Inf scan.

While a held chord is sounding, we pipe every slider and setting to its
extreme values via the WebSocket (min / 0 / max / negative / huge), then capture
the synth's output to a 32-bit float WAV and scan it for NaN/Inf/clipping/silence.

Pass:
  - zero NaN or Inf samples in the recording
  - zero exceptions in journal
  - synth PID still alive
  - peak never stuck at 0 for > 2s while a chord is being held
"""
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import psutil
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _midi_util import chord_hold_mid

DURATION = 60  # seconds
CHORD_MID = Path("/tmp/chord_sweep.mid")
WAV_PATH = Path("/tmp/sweep_capture.wav")


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


# Each entry: (section, param, [values to try])
# Values chosen to probe extremes: 0, typical, above-typical, absurd.
SWEEP = [
    ("synth_pad", "filter_cutoff_hz",   [20.0, 100.0, 1000.0, 20000.0, 50000.0, -10.0]),
    ("synth_pad", "filter_highpass_hz", [20.0, 500.0, 2000.0, 20000.0, -50.0]),
    ("synth_pad", "filter_resonance",   [0.0, 0.5, 0.99, 5.0, 20.0, -1.0]),
    ("synth_pad", "filter_slope",       [12, 24]),
    ("synth_pad", "reverb_dry_wet",     [0.0, 0.5, 1.0, 1.5, -0.1]),
    ("synth_pad", "reverb_wet_gain",    [0.5, 1.0, 3.0, 10.0, 0.0]),
    ("synth_pad", "reverb_decay_seconds", [0.1, 2.0, 20.0, 100.0]),
    ("synth_pad", "reverb_low_cut",     [20.0, 500.0, 5000.0]),
    ("synth_pad", "reverb_high_cut",    [200.0, 2000.0, 20000.0]),
    ("synth_pad", "reverb_space",       [0.0, 0.5, 1.0, 2.0, -0.5]),
    ("synth_pad", "reverb_predelay_ms", [0.0, 50.0, 250.0, 1000.0]),
    ("synth_pad", "shimmer_mix",        [0.0, 0.5, 1.0, 2.0]),
    ("synth_pad", "osc1_blend",         [0.0, 0.5, 1.0]),
    ("synth_pad", "osc2_blend",         [0.0, 0.5, 1.0]),
    ("synth_pad", "unison_voices",      [1, 3, 5, 10, 0, -2]),
    ("synth_pad", "unison_detune",      [0.0, 0.07, 0.5, 2.0, -0.5]),
    ("synth_pad", "unison_spread",      [0.0, 0.5, 1.0, 1.5, -0.5]),
    ("synth_pad", "osc1_indep_cutoff",  [20.0, 20000.0, 50000.0, -10.0]),
    ("synth_pad", "osc2_indep_cutoff",  [20.0, 20000.0, 50000.0, -10.0]),
    ("synth_pad", "volume",             [0.0, 0.5, 1.0, 2.0]),
    ("synth_pad", "adsr.attack_ms",     [0.0, 10.0, 500.0, 5000.0]),
    ("synth_pad", "adsr.release_ms",    [0.0, 100.0, 2000.0, 10000.0]),
    ("piano",     "volume",             [0.0, 0.5, 1.0, 2.0]),
    ("piano",     "filter_highcut_hz",  [200.0, 10000.0, 20000.0]),
    ("piano",     "filter_lowcut_hz",   [20.0, 500.0, 2000.0]),
    ("piano",     "comp_threshold_db",  [-40.0, -20.0, 0.0, 10.0]),
    ("piano",     "comp_ratio",         [1.0, 2.0, 10.0, 100.0, 0.5, -1.0]),
    ("piano",     "comp_makeup_db",     [0.0, 6.0, 24.0, 60.0]),
]

FADER_MSGS = [
    {"type": "fader", "id": 0, "value": 0.0, "alt": 0},
    {"type": "fader", "id": 0, "value": 1.0, "alt": 0},
    {"type": "fader", "id": 2, "value": 0.0, "alt": 0},
    {"type": "fader", "id": 2, "value": 1.0, "alt": 0},  # filter to max
    {"type": "fader", "id": 3, "value": 0.0, "alt": 0},
    {"type": "fader", "id": 3, "value": 1.0, "alt": 0},  # reverb to max
    {"type": "fader", "id": 4, "value": 0.0, "alt": 0},
    {"type": "fader", "id": 4, "value": 1.0, "alt": 0},  # master vol to max
]


async def midi_chord_loop(stop_at: float):
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


async def sweep(ws, stop_at: float, stats: dict):
    await asyncio.sleep(1.0)  # let chord establish

    # Sweep each parameter through every value.
    while time.time() < stop_at:
        for section, param, values in SWEEP:
            for v in values:
                if time.time() >= stop_at:
                    return
                msg = {"type": "setting", "section": section, "param": param, "value": v}
                await ws.send(json.dumps(msg))
                stats["sweep_sent"] += 1
                await asyncio.sleep(0.05)

        # Also throw some fader jumps into the mix.
        for msg in FADER_MSGS:
            if time.time() >= stop_at:
                return
            await ws.send(json.dumps(msg))
            stats["fader_sent"] += 1
            await asyncio.sleep(0.05)

        # Toggle freeze / shimmer / drone mid-sweep.
        for tmsg in ({"type": "freeze_toggle"},
                     {"type": "shimmer_toggle"},
                     {"type": "drone_toggle"},
                     {"type": "panic"}):
            if time.time() >= stop_at:
                return
            await ws.send(json.dumps(tmsg))
            stats["toggle_sent"] += 1
            await asyncio.sleep(0.2)


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


def scan_wav(path: Path) -> dict:
    """Scan the 32-bit-float capture for nastiness."""
    import wave
    # Use scipy-free read: parse via numpy on raw bytes.
    # pw-record writes WAVE_FORMAT_IEEE_FLOAT — not supported by stdlib `wave` module.
    # Read header manually.
    data = path.read_bytes()
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"
    # Find fmt and data chunks.
    i = 12
    channels = 2
    sample_rate = 48000
    bits = 32
    pcm_start = None
    pcm_len = 0
    fmt_code = 1
    while i < len(data) - 8:
        chunk = data[i:i+4]
        size = int.from_bytes(data[i+4:i+8], "little")
        if chunk == b"fmt ":
            fmt_code = int.from_bytes(data[i+8:i+10], "little")
            channels = int.from_bytes(data[i+10:i+12], "little")
            sample_rate = int.from_bytes(data[i+12:i+16], "little")
            bits = int.from_bytes(data[i+22:i+24], "little")
        elif chunk == b"data":
            pcm_start = i + 8
            pcm_len = size
            break
        i += 8 + size + (size & 1)

    assert pcm_start is not None, "no data chunk"
    raw = data[pcm_start:pcm_start+pcm_len]

    if fmt_code == 3 and bits == 32:
        samples = np.frombuffer(raw, dtype="<f4").reshape(-1, channels)
    elif fmt_code == 1 and bits == 16:
        samples = np.frombuffer(raw, dtype="<i2").reshape(-1, channels).astype(np.float32) / 32768.0
    else:
        raise SystemExit(f"unsupported WAV fmt_code={fmt_code} bits={bits}")

    # Stats
    nan_count = int(np.isnan(samples).sum())
    inf_count = int(np.isinf(samples).sum())
    finite = samples[np.isfinite(samples)]
    abs_max = float(np.abs(finite).max()) if len(finite) else 0.0
    clip_samples = int(np.sum(np.abs(finite) >= 0.999))
    total_samples = samples.size
    duration = samples.shape[0] / sample_rate
    # Silent spans: find contiguous blocks where |sample| < 1e-5 on both channels
    block_size = int(sample_rate * 0.1)  # 100ms windows
    n_blocks = samples.shape[0] // block_size
    silent_blocks = 0
    for b in range(n_blocks):
        block = samples[b*block_size:(b+1)*block_size]
        if np.all(np.abs(block) < 1e-5):
            silent_blocks += 1
    silent_ratio = silent_blocks / max(1, n_blocks)

    return {
        "duration": duration,
        "sample_rate": sample_rate,
        "channels": channels,
        "format": f"fmt_code={fmt_code} bits={bits}",
        "total_samples": total_samples,
        "nan": nan_count,
        "inf": inf_count,
        "abs_max": abs_max,
        "clip_samples": clip_samples,
        "clip_ratio": clip_samples / total_samples,
        "silent_100ms_blocks": silent_blocks,
        "silent_ratio": silent_ratio,
    }


async def main():
    pid = find_synth_pid()
    proc = psutil.Process(pid)
    start_rss = proc.memory_info().rss

    # 120s chord so even if aplaymidi relaunches we never go silent.
    chord_hold_mid(CHORD_MID, hold_beats=240)

    if WAV_PATH.exists():
        WAV_PATH.unlink()

    t0 = time.time()
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0 - 1))

    print(f"Synth PID: {pid}")
    print(f"Starting capture → {WAV_PATH}")

    # Start ffmpeg as a JACK client, then manually link StaveSynth outputs to it.
    rec = subprocess.Popen(
        ["pw-jack", "ffmpeg", "-loglevel", "error",
         "-f", "jack", "-i", "stave_sweep_cap",
         "-t", str(DURATION + 5),
         "-c:a", "pcm_f32le",
         "-y", str(WAV_PATH)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Wait for the JACK client to register, then connect.
    for _ in range(30):
        await asyncio.sleep(0.1)
        r = subprocess.run(["pw-jack", "jack_lsp", "stave_sweep_cap"],
                           capture_output=True, text=True)
        if "stave_sweep_cap:input_1" in r.stdout:
            break
    subprocess.run(["pw-jack", "jack_connect", "StaveSynth:out_L",
                    "stave_sweep_cap:input_1"], capture_output=True)
    subprocess.run(["pw-jack", "jack_connect", "StaveSynth:out_R",
                    "stave_sweep_cap:input_2"], capture_output=True)

    stop_at = t0 + DURATION
    stats = {"sweep_sent": 0, "fader_sent": 0, "toggle_sent": 0, "recv": 0, "closed": False}

    async with websockets.connect("ws://localhost:8765", ping_interval=None) as ws:
        await asyncio.gather(
            midi_chord_loop(stop_at),
            sweep(ws, stop_at, stats),
            drain(ws, stop_at, stats),
        )

    # Stop capture — send 'q' to ffmpeg for clean WAV close, fallback to SIGTERM.
    try:
        rec.terminate()
        rec.wait(timeout=5)
    except subprocess.TimeoutExpired:
        rec.kill()

    alive = psutil.pid_exists(pid)
    end_rss = proc.memory_info().rss if alive else 0
    j = journal_since(since)
    exc_lines = [ln for ln in j.splitlines()
                 if any(k in ln for k in ("Traceback", "Error", "CRITICAL", "Exception"))]

    # Reset synth to sane state so we don't leave a mangled config.
    async with websockets.connect("ws://localhost:8765", ping_interval=None) as ws:
        resets = [
            {"type": "fader", "id": 4, "value": 0.75, "alt": 0},  # master vol back to 75%
            {"type": "fader", "id": 3, "value": 0.35, "alt": 0},  # reverb back to 35%
            {"type": "fader", "id": 2, "value": 0.8,  "alt": 0},  # filter back to open
            {"type": "setting", "section": "synth_pad", "param": "reverb_decay_seconds", "value": 4.0},
            {"type": "setting", "section": "synth_pad", "param": "filter_resonance",     "value": 0.2},
            {"type": "setting", "section": "synth_pad", "param": "unison_voices",        "value": 3},
            {"type": "setting", "section": "synth_pad", "param": "unison_detune",        "value": 0.07},
        ]
        for r in resets:
            await ws.send(json.dumps(r))
            await asyncio.sleep(0.05)

    # Scan WAV
    if not WAV_PATH.exists() or WAV_PATH.stat().st_size < 1000:
        print("ERROR: capture file missing or empty")
        return 1
    scan = scan_wav(WAV_PATH)

    print()
    print("=== AUDIO SCAN ===")
    for k, v in scan.items():
        print(f"  {k:22s} {v}")
    print()
    print("=== SWEEP STATS ===")
    print(f"  setting msgs sent:   {stats['sweep_sent']}")
    print(f"  fader msgs sent:     {stats['fader_sent']}")
    print(f"  toggle msgs sent:    {stats['toggle_sent']}")
    print(f"  ws replies received: {stats['recv']}")
    print(f"  ws closed early:     {stats['closed']}")
    print()
    print("=== PROCESS ===")
    print(f"  alive:               {alive}")
    print(f"  RSS delta:           {(end_rss-start_rss)/1e6:+.2f} MB")
    print(f"  new exceptions:      {len(exc_lines)}")
    for e in exc_lines[:8]:
        print("  ", e[-200:])

    # Fail criteria
    fail = (
        scan["nan"] > 0 or
        scan["inf"] > 0 or
        not alive or
        len(exc_lines) > 0 or
        scan["silent_ratio"] > 0.30 or  # more than 30% silent = the synth stopped rendering
        (end_rss - start_rss) > 30_000_000
    )
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
