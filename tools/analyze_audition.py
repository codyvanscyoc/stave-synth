#!/usr/bin/env python3
"""Read-only numerical screening of ONE explicitly supplied, finalized WAV.

Usage: python3 tools/analyze_audition.py /explicit/capture.wav

No app imports, devices, capture, resampling, normalization, or file writes.
Streams <=512 MiB RIFF/WAVE mono/stereo PCM16/24 or IEEE float32, including
their WAVE_FORMAT_EXTENSIBLE equivalents with full-width valid samples.
RF64/RIFX/compressed WAV and padded-bit PCM are deliberately unsupported.

Capture owned JACK out_L/out_R for final master/fade/BTL/underrun behavior;
the app recorder taps BEFORE those operations. Digital capture does not
qualify the DAC/analogue path. Statistics cannot establish subjective quality,
upstream clipping, true-peak headroom, or MIDI-to-output latency. Threshold
times are file-relative signal activity, not note identification.

Extensible format reference:
https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ksmedia/ns-ksmedia-waveformatextensible
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import struct

import numpy as np


MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_CHUNKS = 4096
READ_FRAMES = 65536
MAX_RUNS_PER_KIND = 64
GUID_SUFFIX = bytes.fromhex("00001000800000aa00389b71")


def _header(stream, size):
    raw = stream.read(12)
    if len(raw) != 12 or raw[:4] != b"RIFF" or raw[8:] != b"WAVE":
        raise ValueError("Expected little-endian RIFF/WAVE (not RF64/RIFX)")
    end = 8 + struct.unpack_from("<I", raw, 4)[0]
    if not 12 <= end <= size:
        raise ValueError("Truncated or invalid RIFF size; finalize capture first")
    fmt = data = None
    chunks = 0
    while stream.tell() < end:
        chunks += 1
        if chunks > MAX_CHUNKS or end - stream.tell() < 8:
            raise ValueError("Invalid or excessive RIFF chunk table")
        chunk_id, count = struct.unpack("<4sI", stream.read(8))
        offset = stream.tell()
        padded_end = offset + count + (count & 1)
        if padded_end > end:
            raise ValueError("Truncated WAV chunk")
        if chunk_id == b"fmt ":
            if fmt is not None or not 16 <= count <= 4096:
                raise ValueError("Duplicate or unsupported format chunk")
            fmt = stream.read(count)
        elif chunk_id == b"data":
            if data is not None:
                raise ValueError("Multiple data chunks are unsupported")
            data = (offset, count)
        stream.seek(padded_end)
    if fmt is None or data is None:
        raise ValueError("WAV needs format and data chunks")
    tag, channels, rate, byte_rate, align, bits = struct.unpack_from("<HHIIHH", fmt)
    extensible = tag == 0xFFFE
    mask = None
    if extensible:
        if len(fmt) < 40:
            raise ValueError("Truncated extensible format")
        extra, valid, mask = struct.unpack_from("<HHI", fmt, 16)
        if extra < 22 or 18 + extra > len(fmt) or valid != bits:
            raise ValueError("Unsupported extensible sample/container width")
        if fmt[28:40] != GUID_SUFFIX:
            raise ValueError("Unsupported extensible subformat GUID")
        tag = struct.unpack_from("<I", fmt, 24)[0]
        if mask not in ({0, 4} if channels == 1 else {0, 3}):
            raise ValueError("Expected mono or front-left/front-right channel layout")
    if (tag, bits) not in {(1, 16), (1, 24), (3, 32)}:
        raise ValueError("Supported samples: PCM16, PCM24, IEEE float32")
    if channels not in (1, 2) or not 8000 <= rate <= 384000:
        raise ValueError("Expected mono/stereo at 8–384 kHz")
    if align != channels * (bits // 8) or byte_rate != rate * align:
        raise ValueError("Inconsistent WAV frame size or byte rate")
    offset, count = data
    if not count or count % align:
        raise ValueError("Empty or partial-frame WAV data")
    return dict(encoding="float32" if tag == 3 else f"pcm{bits}",
                channels=channels, sample_rate=rate, bits=bits,
                block_align=align, frames=count // align, data_offset=offset,
                data_bytes=count, extensible=extensible, channel_mask=mask,
                trailing_bytes=size - end)


def _decode(raw, info):
    if info["encoding"] == "float32":
        values = np.frombuffer(raw, dtype="<f4").astype(np.float64)
    elif info["bits"] == 16:
        values = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    else:
        octets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        packed = octets[:, 0] | (octets[:, 1] << 8) | (octets[:, 2] << 16)
        signed = (packed ^ 0x800000) - 0x800000
        values = signed.astype(np.float64) / 8388608.0
    return values.reshape(-1, info["channels"])


def _db(amplitude):
    return 20.0 * math.log10(amplitude) if amplitude > 0 else None


class _Runs:
    """Keep exact counts/durations but only the first 64 runs of each kind."""
    def __init__(self, rate):
        self.rate = rate
        self.current = None
        self.runs = {kind: [] for kind in ("active", "silence", "invalid")}
        self.counts = dict.fromkeys(self.runs, 0)
        self.frames = dict.fromkeys(self.runs, 0)
        self.longest = dict.fromkeys(self.runs, 0)

    def add(self, kind, start, count):
        self.frames[kind] += count
        if self.current is not None and self.current[0] == kind:
            self.current[2] += count
            return
        self.finish()
        self.current = [kind, start, count]

    def finish(self):
        if self.current is None:
            return
        kind, start, count = self.current
        self.counts[kind] += 1
        self.longest[kind] = max(self.longest[kind], count)
        if len(self.runs[kind]) < MAX_RUNS_PER_KIND:
            self.runs[kind].append({"start_s": start / self.rate,
                                    "end_s": (start + count) / self.rate})
        self.current = None

    def report(self):
        self.finish()
        return {kind: {"runs": self.runs[kind], "run_count": self.counts[kind],
                       "runs_truncated": self.counts[kind] > len(self.runs[kind]),
                       "total_s": self.frames[kind] / self.rate,
                       "longest_s": self.longest[kind] / self.rate}
                for kind in self.runs}


def analyze(path, *, window_ms=20.0, silence_dbfs=-80.0):
    """Return JSON-safe evidence, never an audio-quality pass/fail verdict."""
    if not math.isfinite(window_ms) or not 1 <= window_ms <= 1000:
        raise ValueError("window_ms must be finite and between 1 and 1000")
    if not math.isfinite(silence_dbfs) or not -160 <= silence_dbfs <= 0:
        raise ValueError("silence_dbfs must be finite and between -160 and 0")
    path = Path(path)
    # Nonblocking open permits rejecting FIFOs/devices before any read.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise ValueError("Expected regular WAV file no larger than 512 MiB")
        info = _header(stream, before.st_size)
        channels, rate = info["channels"], info["sample_rate"]
        window = max(1, round(rate * window_ms / 1000.0))
        block = max(1, READ_FRAMES // window) * window
        threshold = 10.0 ** (silence_dbfs / 20.0)
        peak = np.zeros(channels)
        sums, squares = np.zeros(channels), np.zeros(channels)
        valid_counts = np.zeros(channels, dtype=np.int64)
        full_scale = np.zeros(channels, dtype=np.int64)
        above_one = np.zeros(channels, dtype=np.int64)
        zeros = np.zeros(channels, dtype=np.int64)
        max_step = np.zeros(channels)
        previous = None
        first = last = None
        pair_sum, pair_squares = np.zeros(2), np.zeros(2)
        cross = mono_squares = side_squares = 0.0
        pairs = 0
        runs = _Runs(rate)
        endpoint = (1.0 if info["encoding"] == "float32"
                    else 1.0 - 2.0 ** (1 - info["bits"]))
        stream.seek(info["data_offset"])
        position = 0
        while position < info["frames"]:
            count = min(block, info["frames"] - position)
            raw = stream.read(count * info["block_align"])
            if len(raw) != count * info["block_align"]:
                raise ValueError("Capture changed or became truncated during analysis")
            values = _decode(raw, info)
            valid = np.isfinite(values)
            # Invalid samples are counted/reported, excluded from finite-only
            # statistics, and invalidate their activity window (not silence).
            finite = np.where(valid, values, 0.0)
            valid_counts += valid.sum(axis=0)
            sums += finite.sum(axis=0)
            squares += np.square(finite).sum(axis=0)
            peak = np.maximum(peak, np.abs(finite).max(axis=0))
            full_scale += (((values >= endpoint) | (values <= -1.0)) & valid).sum(axis=0)
            above_one += ((np.abs(values) > 1.0) & valid).sum(axis=0)
            zeros += (values == 0.0).sum(axis=0)
            activity = np.flatnonzero(np.any((np.abs(values) >= threshold) & valid, axis=1))
            if len(activity):
                if first is None:
                    first = position + int(activity[0])
                last = position + int(activity[-1])
            joined = values if previous is None else np.vstack((previous, values))
            adjacent = np.isfinite(joined[:-1]) & np.isfinite(joined[1:])
            difference = np.abs(np.where(adjacent, joined[1:], 0.0)
                                - np.where(adjacent, joined[:-1], 0.0))
            if len(difference):
                max_step = np.maximum(max_step, difference.max(axis=0))
            previous = values[-1:].copy()
            if channels == 2:
                paired = values[np.all(valid, axis=1)]
                pairs += len(paired)
                pair_sum += paired.sum(axis=0)
                pair_squares += np.square(paired).sum(axis=0)
                cross += float(np.sum(paired[:, 0] * paired[:, 1]))
                mono_squares += float(np.square(paired.mean(axis=1)).sum())
                side_squares += float(np.square((paired[:, 0] - paired[:, 1]) * 0.5).sum())
            for start in range(0, count, window):
                section = values[start:start + window]
                if not np.isfinite(section).all():
                    kind = "invalid"
                else:
                    kind = "active" if float(np.sqrt(np.mean(section * section))) >= threshold else "silence"
                runs.add(kind, position + start, len(section))
            position += count
        digest = hashlib.sha256()
        stream.seek(0)
        remaining = before.st_size
        while remaining:
            raw = stream.read(min(remaining, 256 * 1024))
            if not raw:
                raise ValueError("Capture shrank during analysis")
            digest.update(raw)
            remaining -= len(raw)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("Capture changed during analysis; analyze a finalized file")
    channel_stats = []
    for channel in range(channels):
        n = int(valid_counts[channel])
        rms = math.sqrt(float(squares[channel]) / n) if n else None
        channel_stats.append({
            "channel": channel + 1, "finite_samples": n,
            "nonfinite_samples": info["frames"] - n,
            "sample_peak": float(peak[channel]) if n else None,
            "sample_peak_dbfs": _db(float(peak[channel])) if n else None,
            "rms": rms, "rms_dbfs": _db(rms) if rms is not None else None,
            "dc_mean": float(sums[channel]) / n if n else None,
            "full_scale_or_over_samples": int(full_scale[channel]),
            "above_unity_samples": int(above_one[channel]),
            "exact_zero_samples": int(zeros[channel]),
            "max_adjacent_finite_step": float(max_step[channel]),
        })
    stereo = None
    if channels == 2 and pairs:
        mono_rms = math.sqrt(mono_squares / pairs)
        ref_rms = math.sqrt(float(pair_squares.sum()) / (2 * pairs))
        variance = np.maximum(0.0, pair_squares - pair_sum * pair_sum / pairs)
        denominator = math.sqrt(float(variance[0] * variance[1]))
        correlation = ((cross - float(pair_sum[0] * pair_sum[1]) / pairs) / denominator
                       if denominator else None)
        stereo = {"finite_pairs": pairs, "mono_rms": mono_rms,
                  "stereo_reference_rms": ref_rms,
                  "side_rms": math.sqrt(side_squares / pairs),
                  "mono_relative_db": _db(mono_rms / ref_rms) if ref_rms else None,
                  "mono_zero_with_nonzero_stereo": mono_rms == 0 and ref_rms > 0,
                  "correlation": max(-1.0, min(1.0, correlation)) if correlation is not None else None}
    nonfinite = sum(item["nonfinite_samples"] for item in channel_stats)
    return {
        "path": str(path.absolute()), "sha256": digest.hexdigest(),
        "file_bytes": before.st_size, "format": info,
        "duration_s": info["frames"] / rate, "channels": channel_stats,
        "stereo": stereo, "nonfinite_samples": nonfinite,
        "status": "invalid_samples" if nonfinite else "analyzed",
        "stage_qualified": False, "audio_modified": False,
        "positive_full_scale_threshold": endpoint,
        "activity": {"threshold_dbfs": silence_dbfs, "window_frames": window,
                     "window_ms": window * 1000.0 / rate,
                     "first_sample_at_threshold": first, "last_sample_at_threshold": last,
                     "first_time_s": first / rate if first is not None else None,
                     "last_time_s": last / rate if last is not None else None,
                     **runs.report()},
        "limitations": [
            "Finite-only sample statistics; not true-peak or upstream clipping detection.",
            "Full-scale samples and large adjacent steps are observations, not proof of audible clipping/clicks.",
            "Mono cancellation can be intentional BTL output; interpret with capture routing and BTL state.",
            "Silence/activity depends on threshold and musical intent; times are not MIDI latency or note identification.",
            "No subjective sound, analogue path, recorder drop integrity, or stage qualification verdict.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", help="Explicit finalized WAV; no default recording directory")
    parser.add_argument("--window-ms", type=float, default=20.0)
    parser.add_argument("--silence-dbfs", type=float, default=-80.0)
    args = parser.parse_args(argv)
    try:
        report = analyze(args.wav, window_ms=args.window_ms, silence_dbfs=args.silence_dbfs)
    except (OSError, ValueError, struct.error) as exc:
        print(json.dumps({"status": "error", "error": str(exc), "stage_qualified": False}))
        return 2
    print(json.dumps(report, indent=2, allow_nan=False))
    return 1 if report["nonfinite_samples"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
