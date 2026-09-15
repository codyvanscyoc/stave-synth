#!/usr/bin/env python3
"""Offline scalar/variant StaveOrgan comparison; never imports the app or JACK.

Both libraries must be private Linux/AArch64 ELF copies.  The checker renders
the same bounded control timeline through fresh DSP instances, checks every
sample, verifies block-partition invariance, then alternates native-compute CPU
measurements.  Its JSON report is created exclusively and is never overwritten.
"""

from __future__ import annotations

import argparse
from array import array
import ctypes
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import platform
import statistics
import struct
import time


SAMPLE_RATE = 48_000
BLOCK_FRAMES = 512
SCENARIO_FRAMES = 8 * SAMPLE_RATE
WORKER_TIMEOUT_SECONDS = 45.0
MAX_LIBRARY_BYTES = 64 * 1024 * 1024
MAX_ZONES = 128
BENCHMARK_RUNS = 6  # equal scalar-first and variant-first runs
BENCHMARK_BLOCKS_PER_RUN = 32
BENCHMARK_WARMUP_BLOCKS = 8
MAX_ABS_ERROR = 2.0e-5
MAX_RELATIVE_NULL_RMS = 1.0e-5
MAX_LEVEL_DELTA_DB = 0.01
MIN_MEDIAN_SPEEDUP = 0.15
MIN_P95_SPEEDUP = 0.10
REMAINDER_PATTERN = (31, 127, 7, 347)  # sums to one production block

PRESETS = {
    "mellow": (8, 0, 6, 4, 0, 0, 0, 0, 0),
    "full": (8, 6, 8, 8, 6, 6, 4, 4, 4),
    "gospel": (8, 8, 8, 6, 4, 4, 2, 2, 2),
    "jazz": (8, 0, 8, 0, 0, 0, 0, 0, 0),
}
EVENT_FRAMES = (0, 36_000, 72_000, 108_000, 144_000, 168_000, 192_000,
                228_000, 240_000)
EVENT_NAMES = ("mellow_slow_triad", "full_16_fast", "gospel_16_fast",
               "jazz_16_fast", "leslie_stop", "release", "retrigger_extreme_a",
               "extreme_b_fast", "final_release_stop_3s_tail")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _active_library_path():
    return (Path(__file__).resolve().parent.parent / "faust" / "libstave_organ.so").resolve()


def validate_libraries(scalar, variant):
    require(platform.system() == "Linux" and platform.machine().lower() in {"aarch64", "arm64"},
            "organ variant checker requires Linux AArch64; no library was loaded")
    paths = [Path(item).expanduser().resolve(strict=True) for item in (scalar, variant)]
    require(paths[0] != paths[1], "scalar and variant paths must be distinct")
    active = _active_library_path()
    require(all(path != active for path in paths),
            "refusing checkout's active-service organ artifact; use private copied libraries")
    identities, results = [], []
    for path in paths:
        require(path.is_file(), "library must be a regular file")
        stat = path.stat()
        require(64 <= stat.st_size <= MAX_LIBRARY_BYTES, "library size outside bounded ELF range")
        identities.append((stat.st_dev, stat.st_ino))
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            header = handle.read(64)
            require(header[:7] == b"\x7fELF\x02\x01\x01"
                    and struct.unpack_from("<HH", header, 16) == (3, 183),
                    "library is not a little-endian ELF64 AArch64 shared object")
            digest.update(header)
            total = len(header)
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                total += len(chunk)
                require(total <= MAX_LIBRARY_BYTES, "library grew beyond bounded size while hashing")
                digest.update(chunk)
        after = path.stat()
        require((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                == (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns),
                "library changed during validation")
        results.append({"path": str(path), "size_bytes": stat.st_size,
                        "sha256": digest.hexdigest()})
    require(identities[0] != identities[1], "scalar/variant are hard links to one library")
    return results


FloatPointer = ctypes.POINTER(ctypes.c_float)
OpenBox = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p)
CloseBox = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
Button = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p, FloatPointer)
Slider = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p, FloatPointer,
                         ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float)
Bar = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p, FloatPointer,
                      ctypes.c_float, ctypes.c_float)
Soundfile = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
                            ctypes.POINTER(ctypes.c_void_p))
Declare = ctypes.CFUNCTYPE(None, ctypes.c_void_p, FloatPointer, ctypes.c_char_p, ctypes.c_char_p)


class UIGlue(ctypes.Structure):
    _fields_ = [("uiInterface", ctypes.c_void_p),
                ("openTabBox", OpenBox), ("openHorizontalBox", OpenBox),
                ("openVerticalBox", OpenBox), ("closeBox", CloseBox),
                ("addButton", Button), ("addCheckButton", Button),
                ("addVerticalSlider", Slider), ("addHorizontalSlider", Slider),
                ("addNumEntry", Slider), ("addHorizontalBargraph", Bar),
                ("addVerticalBargraph", Bar), ("addSoundfile", Soundfile),
                ("declare", Declare)]


def expected_zone_names():
    names = {f"{prefix}_v{index}" for prefix in ("freq", "gate", "phase", "pan")
             for index in range(16)}
    names.update(f"amp_d{index}" for index in range(9))
    names.update(("drive", "leslie_depth", "leslie_target_hz", "volume",
                  "highcut_hz", "lowcut_hz", "tone_tilt"))
    return names


class NativeOrgan:
    """Own one RTLD_LOCAL Faust C instance and fixed 512-frame buffers."""

    def __init__(self, path):
        self.dsp = None
        self.library = ctypes.CDLL(str(path), mode=os.RTLD_LOCAL | os.RTLD_NOW)
        signatures = {
            "newStaveOrgan": ([], ctypes.c_void_p),
            "deleteStaveOrgan": ([ctypes.c_void_p], None),
            "initStaveOrgan": ([ctypes.c_void_p, ctypes.c_int], None),
            "buildUserInterfaceStaveOrgan": ([ctypes.c_void_p, ctypes.POINTER(UIGlue)], None),
            "computeStaveOrgan": ([ctypes.c_void_p, ctypes.c_int,
                                   ctypes.POINTER(FloatPointer), ctypes.POINTER(FloatPointer)], None),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.library, name)
            function.argtypes, function.restype = arguments, result
        self.dsp = self.library.newStaveOrgan()
        require(self.dsp, "native organ allocation failed")
        self.zones, self.metadata, self.ui_errors = {}, {}, []
        try:
            self.library.initStaveOrgan(self.dsp, SAMPLE_RATE)

            def slider(_ui, raw_label, zone, initial, minimum, maximum, step):
                try:
                    label = raw_label.decode("utf-8")
                    require(0 < len(label) <= 80 and label not in self.zones
                            and len(self.zones) < MAX_ZONES,
                            "duplicate, invalid, or excessive native zone metadata")
                    values = (float(initial), float(minimum), float(maximum), float(step))
                    require(bool(zone) and all(math.isfinite(value) for value in values),
                            "invalid native zone pointer/range")
                    require(values[1] <= values[0] <= values[2] and values[3] >= 0,
                            "invalid native slider bounds")
                    self.zones[label] = zone
                    self.metadata[label] = dict(zip(("initial", "min", "max", "step"), values))
                except Exception as exc:
                    if len(self.ui_errors) < 8:
                        self.ui_errors.append(str(exc))

            open_box = OpenBox(lambda *_args: None)
            close_box = CloseBox(lambda *_args: None)
            button = Button(lambda *_args: self.ui_errors.append("unexpected button")
                            if len(self.ui_errors) < 8 else None)
            slide = Slider(slider)
            bar = Bar(lambda *_args: None)
            soundfile = Soundfile(lambda *_args: self.ui_errors.append("unexpected soundfile")
                                  if len(self.ui_errors) < 8 else None)
            declare = Declare(lambda *_args: None)
            self.glue = UIGlue(None, open_box, open_box, open_box, close_box, button, button,
                               slide, slide, slide, bar, bar, soundfile, declare)
            self.callbacks = (open_box, close_box, button, slide, bar, soundfile, declare)
            self.library.buildUserInterfaceStaveOrgan(self.dsp, ctypes.byref(self.glue))
            require(not self.ui_errors, "; ".join(self.ui_errors))
            require(set(self.zones) == expected_zone_names(), "native zone inventory is not StaveOrgan ABI")
            self.input = (ctypes.c_float * BLOCK_FRAMES)()
            self.left = (ctypes.c_float * BLOCK_FRAMES)()
            self.right = (ctypes.c_float * BLOCK_FRAMES)()
            self.inputs = (FloatPointer * 1)(self.input)
            self.outputs = (FloatPointer * 2)(self.left, self.right)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.dsp:
            self.library.deleteStaveOrgan(self.dsp)
            self.dsp = None

    def set(self, name, value):
        require(name in self.zones and math.isfinite(value), f"missing/invalid zone {name}")
        meta = self.metadata[name]
        require(meta["min"] <= value <= meta["max"], f"out-of-contract native value for {name}")
        self.zones[name][0] = value

    def prepare_input(self, absolute_frame, frames):
        ctypes.memset(self.input, 0, frames * ctypes.sizeof(ctypes.c_float))
        click_length = 144
        for click_frame in (0, 192_000):
            start = max(absolute_frame, click_frame)
            end = min(absolute_frame + frames, click_frame + click_length)
            for frame in range(start, end):
                offset = frame - click_frame
                self.input[frame - absolute_frame] = (0.05 * math.sin(0.37 * offset)
                                                       * math.exp(-offset / 38.0))

    def compute_native(self, frames):
        require(type(frames) is int and 0 < frames <= BLOCK_FRAMES, "invalid compute frame count")
        self.library.computeStaveOrgan(self.dsp, frames, self.inputs, self.outputs)

    def output(self, frames):
        result = []
        for buffer in (self.left, self.right):
            values = array("f")
            values.frombytes(ctypes.string_at(buffer, frames * 4))
            require(len(values) == frames and all(math.isfinite(value) for value in values),
                    "native output contains NaN/Inf or has wrong length")
            result.append(values)
        return result

    def compute(self, absolute_frame, frames):
        self.prepare_input(absolute_frame, frames)
        self.compute_native(frames)
        return self.output(frames)


def compare_metadata(scalar, variant):
    require(set(scalar) == expected_zone_names() == set(variant), "native zone ABI names differ")
    for name in sorted(scalar):
        require(scalar[name] == variant[name], f"native zone metadata differs for {name}")


def _set_preset(organ, name):
    raw = [value / 8.0 for value in PRESETS[name]]
    total = sum(raw)
    if total > 0:
        scale = 1.0 / max(total, 1.0)
        raw = [value * scale for value in raw]
    for index, value in enumerate(raw):
        adjacent = (raw[index - 1] if index else 0.0) + (raw[index + 1] if index < 8 else 0.0)
        organ.set(f"amp_d{index}", value + 0.008 * adjacent)


def _set_voices(organ, count, gate):
    notes = (36, 43, 48, 52, 55, 60, 64, 67, 71, 74, 77, 81, 84, 88, 91, 96)
    for index, note in enumerate(notes):
        organ.set(f"freq_v{index}", 440.0 * (2.0 ** ((note - 69) / 12.0)))
        organ.set(f"phase_v{index}", (index + 1) / 17.0)
        organ.set(f"pan_v{index}", -1.0 + 2.0 * index / 15.0)
        organ.set(f"gate_v{index}", gate * (0.55 + 0.45 * ((index % 5) / 4.0)) if index < count else 0.0)


def apply_event(organ, index):
    if index == 0:
        _set_preset(organ, "mellow")
        _set_voices(organ, 3, 0.72)
        values = {"drive": 0.05, "leslie_depth": 0.3, "leslie_target_hz": 0.8,
                  "volume": 0.5, "highcut_hz": 8000.0, "lowcut_hz": 40.0,
                  "tone_tilt": 0.5}
    elif index in (1, 2, 3):
        _set_preset(organ, ("full", "gospel", "jazz")[index - 1])
        _set_voices(organ, 16, 0.63)
        values = {"leslie_target_hz": 6.5}
    elif index == 4:
        values = {"leslie_target_hz": 0.0}
    elif index == 5:
        _set_voices(organ, 0, 0.0)
        values = {}
    elif index == 6:
        _set_preset(organ, "full")
        _set_voices(organ, 4, 0.91)
        values = {"drive": 1.0, "leslie_depth": 1.0, "leslie_target_hz": 0.8,
                  "volume": 1.0, "highcut_hz": 12000.0, "lowcut_hz": 500.0,
                  "tone_tilt": 1.0}
    elif index == 7:
        values = {"drive": 0.0, "leslie_depth": 0.0, "leslie_target_hz": 6.5,
                  "volume": 0.01, "highcut_hz": 200.0, "lowcut_hz": 20.0,
                  "tone_tilt": 0.0}
    else:
        _set_voices(organ, 0, 0.0)
        # Restore an audible normal tone path for the final fast-to-stop
        # release/tail interval; the preceding event already covered minima.
        values = {"leslie_target_hz": 0.0, "leslie_depth": 0.7, "volume": 0.5,
                  "highcut_hz": 8000.0, "lowcut_hz": 40.0, "tone_tilt": 0.5}
    for name, value in values.items():
        organ.set(name, value)


def render_scenario(organ, partition):
    require(partition in {"production", "remainder"}, "unknown render partition")
    left, right = array("f"), array("f")
    frame = event_index = pattern_index = 0
    next_event = EVENT_FRAMES[0]
    chunks = 0
    while frame < SCENARIO_FRAMES:
        if frame == next_event:
            apply_event(organ, event_index)
            event_index += 1
            next_event = EVENT_FRAMES[event_index] if event_index < len(EVENT_FRAMES) else SCENARIO_FRAMES
        requested = BLOCK_FRAMES if partition == "production" else REMAINDER_PATTERN[pattern_index % len(REMAINDER_PATTERN)]
        pattern_index += 1
        frames = min(requested, next_event - frame, SCENARIO_FRAMES - frame)
        require(frames > 0, "scenario scheduler made no progress")
        output = organ.compute(frame, frames)
        left.extend(output[0])
        right.extend(output[1])
        frame += frames
        chunks += 1
    require(event_index == len(EVENT_FRAMES) and len(left) == len(right) == SCENARIO_FRAMES,
            "scenario did not cover its full event/sample timeline")
    return (left, right), chunks


def parity_metrics(reference, candidate):
    require(len(reference) == len(candidate) == 2 and len(reference[0]) == len(candidate[0]),
            "parity buffers have different shapes")
    count = 2 * len(reference[0])
    square_ref = square_candidate = square_null = 0.0
    peak_ref = peak_candidate = peak_null = 0.0
    exact = True
    for ref_channel, candidate_channel in zip(reference, candidate):
        for old, new in zip(ref_channel, candidate_channel):
            require(math.isfinite(old) and math.isfinite(new), "parity input contains NaN/Inf")
            delta = float(new) - float(old)
            exact &= old == new
            peak_ref = max(peak_ref, abs(old))
            peak_candidate = max(peak_candidate, abs(new))
            peak_null = max(peak_null, abs(delta))
            square_ref += old * old
            square_candidate += new * new
            square_null += delta * delta
    rms_ref = math.sqrt(square_ref / count)
    rms_candidate = math.sqrt(square_candidate / count)
    rms_null = math.sqrt(square_null / count)
    relative_null = rms_null / max(rms_ref, 1.0e-30)
    level_delta = (20.0 * math.log10(max(rms_candidate, 1.0e-30) / max(rms_ref, 1.0e-30)))
    return {"sample_count": count, "sample_exact": exact, "reference_peak": peak_ref,
            "candidate_peak": peak_candidate, "null_peak": peak_null,
            "reference_rms": rms_ref, "candidate_rms": rms_candidate,
            "null_rms": rms_null, "relative_null_rms": relative_null,
            "rms_level_delta_db": level_delta}


def enforce_parity(metrics, label):
    require(metrics["reference_peak"] > 0 and metrics["candidate_peak"] > 0,
            f"{label} unexpectedly silent")
    require(metrics["null_peak"] <= MAX_ABS_ERROR, f"{label} absolute error exceeds gate")
    require(metrics["relative_null_rms"] <= MAX_RELATIVE_NULL_RMS,
            f"{label} relative null RMS exceeds gate")
    require(abs(metrics["rms_level_delta_db"]) <= MAX_LEVEL_DELTA_DB,
            f"{label} level delta exceeds gate")


def _nearest_rank(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def benchmark_pair(scalar, variant, clock=time.thread_time_ns, *, voices=16):
    require(type(voices) is int and 1 <= voices <= 16, "benchmark voices must be 1..16")
    for organ in (scalar, variant):
        apply_event(organ, 1)
        _set_voices(organ, voices, 0.63)  # Preserve event 1's original gate levels.
        # Correctness separately exercises click input. Keep the timed full
        # registration steady instead of replaying one onset every block.
        organ.prepare_input(BLOCK_FRAMES, BLOCK_FRAMES)
        for _ in range(BENCHMARK_WARMUP_BLOCKS):
            organ.compute_native(BLOCK_FRAMES)
    timings = {"scalar": [], "variant": []}
    run_totals = {"scalar": [], "variant": []}
    organs = {"scalar": scalar, "variant": variant}
    order_trace = []
    for run_index in range(BENCHMARK_RUNS):
        order = ("scalar", "variant") if run_index % 2 == 0 else ("variant", "scalar")
        order_trace.append(list(order))
        for name in order:
            before_run = clock()
            for _ in range(BENCHMARK_BLOCKS_PER_RUN):
                before = clock()
                organs[name].compute_native(BLOCK_FRAMES)
                after = clock()
                require(type(before) is int and type(after) is int and after >= before,
                        "invalid benchmark thread CPU clock")
                timings[name].append(after - before)
            after_run = clock()
            require(after_run >= before_run, "invalid benchmark run CPU clock")
            run_totals[name].append(after_run - before_run)
            organs[name].output(BLOCK_FRAMES)  # finite-output check outside timed region
    result = {"clock": "time.thread_time_ns", "runs": BENCHMARK_RUNS,
              "active_voices": voices, "available_voices": 16,
              "blocks_per_run": BENCHMARK_BLOCKS_PER_RUN, "warmup_blocks": BENCHMARK_WARMUP_BLOCKS,
              "alternating_order": order_trace, "quantile_method": "nearest_rank"}
    for name in ("scalar", "variant"):
        result[name] = {"samples": len(timings[name]),
                        "median_compute_cpu_ns": statistics.median(timings[name]),
                        "p95_compute_cpu_ns": _nearest_rank(timings[name], 0.95),
                        "max_compute_cpu_ns": max(timings[name]),
                        "run_cpu_ns": run_totals[name]}
    require(result["scalar"]["median_compute_cpu_ns"] > 0
            and result["scalar"]["p95_compute_cpu_ns"] > 0,
            "scalar benchmark clock did not resolve compute duration")
    result["median_speedup_fraction"] = (1.0 - result["variant"]["median_compute_cpu_ns"]
                                         / result["scalar"]["median_compute_cpu_ns"])
    result["p95_speedup_fraction"] = (1.0 - result["variant"]["p95_compute_cpu_ns"]
                                      / result["scalar"]["p95_compute_cpu_ns"])
    result["gate"] = {"minimum_median_speedup_fraction": MIN_MEDIAN_SPEEDUP,
                      "minimum_p95_speedup_fraction": MIN_P95_SPEEDUP,
                      "passed": (result["median_speedup_fraction"] >= MIN_MEDIAN_SPEEDUP
                                 and result["p95_speedup_fraction"] >= MIN_P95_SPEEDUP)}
    return result


def new_report():
    return {"format": 1, "status": "FAIL", "failures": [], "sample_rate_hz": SAMPLE_RATE,
            "production_block_frames": BLOCK_FRAMES,
            "scenario": {"frames": SCENARIO_FRAMES, "seconds": SCENARIO_FRAMES / SAMPLE_RATE,
                         "event_frames": list(EVENT_FRAMES), "event_names": list(EVENT_NAMES),
                         "remainder_pattern": list(REMAINDER_PATTERN)},
            "parity_gates": {"max_abs_error": MAX_ABS_ERROR,
                             "max_relative_null_rms": MAX_RELATIVE_NULL_RMS,
                             "max_level_delta_db": MAX_LEVEL_DELTA_DB},
            "limits": ["Offline native compute only: no app, JACK, MIDI, service, or physical output.",
                       "CPU timings include ctypes call overhead but exclude output-copy/validation.",
                       "Numeric parity and level metrics do not replace a level-matched listening review.",
                       "Passing on one Pi/compiler build does not qualify other architectures or builds."]}


def check_variants(scalar_path, variant_path, report):
    opened = []
    parity_failures = []
    try:
        for path in (scalar_path, scalar_path, variant_path, variant_path):
            opened.append(NativeOrgan(path))
        scalar_prod, scalar_rem, variant_prod, variant_rem = opened
        report["zone_metadata"] = {"scalar": scalar_prod.metadata,
                                   "variant": variant_prod.metadata}
        compare_metadata(scalar_prod.metadata, variant_prod.metadata)
        report["zone_inventory"] = {"count": len(scalar_prod.metadata),
                                    "names": sorted(scalar_prod.metadata),
                                    "metadata_equal": True}
        scalar_output, scalar_chunks = render_scenario(scalar_prod, "production")
        scalar_partition, scalar_remainder_chunks = render_scenario(scalar_rem, "remainder")
        variant_output, variant_chunks = render_scenario(variant_prod, "production")
        variant_partition, variant_remainder_chunks = render_scenario(variant_rem, "remainder")
        comparisons = {
            "scalar_vs_variant": parity_metrics(scalar_output, variant_output),
            "scalar_partition_invariance": parity_metrics(scalar_output, scalar_partition),
            "variant_partition_invariance": parity_metrics(variant_output, variant_partition),
        }
        report["comparisons"] = comparisons
        report["all_compared_samples_finite"] = True
        for label, metrics in comparisons.items():
            try:
                enforce_parity(metrics, label)
            except RuntimeError as exc:
                parity_failures.append(str(exc))
        # Whole-timeline metrics cannot conceal a local onset, mode-transition,
        # release, or tail discrepancy.  Enforce the same gates independently
        # in every interval between controls, including the final three seconds.
        report["scalar_variant_segments"] = []
        boundaries = list(EVENT_FRAMES) + [SCENARIO_FRAMES]
        for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
            segment = parity_metrics(
                (scalar_output[0][start:end], scalar_output[1][start:end]),
                (variant_output[0][start:end], variant_output[1][start:end]),
            )
            report["scalar_variant_segments"].append(
                {"name": EVENT_NAMES[index], "start_frame": start, "end_frame": end, **segment}
            )
            try:
                enforce_parity(segment, f"scalar_vs_variant segment {EVENT_NAMES[index]}")
            except RuntimeError as exc:
                parity_failures.append(str(exc))
        report["parity_failures"] = parity_failures
        report["render_chunks"] = {"scalar_production": scalar_chunks,
                                   "scalar_remainder": scalar_remainder_chunks,
                                   "variant_production": variant_chunks,
                                   "variant_remainder": variant_remainder_chunks}
    finally:
        for organ in reversed(opened):
            organ.close()
    benchmark_organs = []
    try:
        for path in (scalar_path, variant_path):
            benchmark_organs.append(NativeOrgan(path))
        report["benchmark"] = benchmark_pair(*benchmark_organs)
        require(report["benchmark"]["gate"]["passed"],
                "variant compute speedup is not material at median and p95")
        require(not parity_failures, "; ".join(parity_failures))
    finally:
        for organ in reversed(benchmark_organs):
            organ.close()


def _worker(scalar_path, variant_path, connection):
    report = new_report()
    started = time.monotonic()
    try:
        report["libraries"] = validate_libraries(scalar_path, variant_path)
        check_variants(scalar_path, variant_path, report)
        report["status"] = "PASS"
    except BaseException as exc:
        report["failures"].append(f"{type(exc).__name__}: {exc}")
    report["elapsed_seconds"] = time.monotonic() - started
    try:
        connection.send(report)
    finally:
        connection.close()


def run(scalar, variant):
    report = new_report()
    process = receive = send = None
    try:
        report["libraries"] = validate_libraries(scalar, variant)
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=_worker,
                                  args=(report["libraries"][0]["path"],
                                        report["libraries"][1]["path"], send), daemon=True)
        process.start()
        send.close()
        if not receive.poll(WORKER_TIMEOUT_SECONDS):
            raise TimeoutError("organ variant worker exceeded bounded deadline")
        report = receive.recv()
        require(isinstance(report, dict) and report.get("status") in {"PASS", "FAIL"},
                "native worker returned malformed evidence")
        process.join(0.5)
        require(process.exitcode == 0, f"native worker did not exit cleanly: {process.exitcode}")
    except BaseException as exc:
        report["status"] = "FAIL"
        report["failures"].append(f"{type(exc).__name__}: {exc}")
    finally:
        if process is not None and process.pid is not None and process.is_alive():
            process.terminate()
            process.join(0.5)
            if process.is_alive():
                process.kill()
                process.join(0.5)
        for pipe in (receive, send):
            if pipe is not None:
                pipe.close()
    return report


def write_new_report(path, report):
    target = Path(path).expanduser()
    require(target.parent.is_dir(), "report parent directory does not exist")
    payload = (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    completed = False
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            require(written > 0, "short report write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
        completed = True
    finally:
        os.close(descriptor)
        if not completed:
            try:
                target.unlink()
            except OSError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scalar", required=True, help="private scalar AArch64 organ .so")
    parser.add_argument("--variant", required=True, help="private candidate AArch64 organ .so")
    parser.add_argument("--output", required=True, help="new JSON report path (must not exist)")
    args = parser.parse_args(argv)
    report = run(args.scalar, args.variant)
    try:
        write_new_report(args.output, report)
    except BaseException as exc:
        print(json.dumps({"format": 1, "status": "FAIL",
                          "failures": [f"report output: {type(exc).__name__}: {exc}"]},
                         sort_keys=True, allow_nan=False))
        return 2
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
