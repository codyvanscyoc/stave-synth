#!/usr/bin/env python3
"""Compare two explicitly named native organs offline; never start app/JACK.

Only Linux AArch64 ELF shared libraries are accepted. Native calls run in a
disposable child with a ten-second deadline. The parent prints one JSON result.
STOP checks control acceptance and finite continued processing, not internal
rotor phase, subjective sound, stage latency, or a measured mechanical stop.
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
import struct
import time


SAMPLE_RATE = 48000
BLOCK_FRAMES = 512
WORKER_TIMEOUT = 10.0
STOP_SECONDS = 8
MAX_LIBRARY_BYTES = 64 * 1024 * 1024
MAX_ZONES = 128


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def validate_libraries(baseline, candidate):
    require(platform.system() == "Linux" and platform.machine().lower() in {"aarch64", "arm64"},
            "native checker requires Linux AArch64; no library was loaded")
    paths = [Path(item).expanduser().resolve(strict=True) for item in (baseline, candidate)]
    require(paths[0] != paths[1], "baseline and candidate must be distinct libraries")
    results = []
    identities = []
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
            bytes_read = len(header)
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                bytes_read += len(chunk)
                require(bytes_read <= MAX_LIBRARY_BYTES, "library grew beyond bounded size while hashing")
                digest.update(chunk)
        after = path.stat()
        require((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                == (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns),
                "library changed during validation")
        results.append({"path": str(path), "size_bytes": stat.st_size, "sha256": digest.hexdigest()})
    require(identities[0] != identities[1], "baseline/candidate are hard links to the same library")
    return results


FloatPointer = ctypes.POINTER(ctypes.c_float)
OpenBox = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p)
CloseBox = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
Button = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p, FloatPointer)
Slider = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p, FloatPointer,
                         ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float)
Bar = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p, FloatPointer, ctypes.c_float, ctypes.c_float)
Soundfile = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
                            ctypes.POINTER(ctypes.c_void_p))
Declare = ctypes.CFUNCTYPE(None, ctypes.c_void_p, FloatPointer, ctypes.c_char_p, ctypes.c_char_p)


class UIGlue(ctypes.Structure):
    _fields_ = [("uiInterface", ctypes.c_void_p),
                ("openTabBox", OpenBox), ("openHorizontalBox", OpenBox), ("openVerticalBox", OpenBox),
                ("closeBox", CloseBox), ("addButton", Button), ("addCheckButton", Button),
                ("addVerticalSlider", Slider), ("addHorizontalSlider", Slider), ("addNumEntry", Slider),
                ("addHorizontalBargraph", Bar), ("addVerticalBargraph", Bar),
                ("addSoundfile", Soundfile), ("declare", Declare)]


class NativeOrgan:
    """Minimal Faust C API owner. No app imports, sound device, or instanceClear."""
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
        for name, (args, result) in signatures.items():
            function = getattr(self.library, name)
            function.argtypes, function.restype = args, result
        self.dsp = self.library.newStaveOrgan()
        require(self.dsp, "native organ allocation failed")
        self.zones, self.metadata, self.ui_errors = {}, {}, []
        try:
            self.library.initStaveOrgan(self.dsp, SAMPLE_RATE)

            def slider(_ui, raw_label, zone, initial, minimum, maximum, step):
                try:
                    label = raw_label.decode("utf-8")
                    require(0 < len(label) <= 80 and label not in self.zones and len(self.zones) < MAX_ZONES,
                            "duplicate, invalid, or excessive native zone metadata")
                    require(bool(zone) and all(math.isfinite(value) for value in (initial, minimum, maximum, step)),
                            "invalid native zone pointer/range")
                    require(minimum <= initial <= maximum and step >= 0, "invalid native slider bounds")
                    self.zones[label] = zone
                    self.metadata[label] = {"initial": initial, "min": minimum, "max": maximum, "step": step}
                except Exception as exc:
                    if len(self.ui_errors) < 8:
                        self.ui_errors.append(str(exc))

            open_box = OpenBox(lambda *_args: None)
            close_box = CloseBox(lambda *_args: None)
            button = Button(lambda *_args: self.ui_errors.append("unexpected button") if len(self.ui_errors) < 8 else None)
            slide = Slider(slider)
            bar = Bar(lambda *_args: None)
            soundfile = Soundfile(lambda *_args: self.ui_errors.append("unexpected soundfile") if len(self.ui_errors) < 8 else None)
            declare = Declare(lambda *_args: None)
            self.glue = UIGlue(None, open_box, open_box, open_box, close_box, button, button,
                               slide, slide, slide, bar, bar, soundfile, declare)
            self.callbacks = (open_box, close_box, button, slide, bar, soundfile, declare)
            self.library.buildUserInterfaceStaveOrgan(self.dsp, ctypes.byref(self.glue))
            require(not self.ui_errors, "; ".join(self.ui_errors))
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

    def get(self, name):
        return float(self.zones[name][0])

    def compute(self, frames):
        require(type(frames) is int and 0 < frames <= BLOCK_FRAMES, "invalid compute frame count")
        self.library.computeStaveOrgan(self.dsp, frames, self.inputs, self.outputs)
        return (ctypes.string_at(self.left, frames * 4), ctypes.string_at(self.right, frames * 4))


def configure(organ):
    # Deterministic C-major triad, no random phase or click excitation.
    for index in range(16):
        organ.set(f"freq_v{index}", (130.8127826502993, 164.81377845643496, 195.99771799087463)[index] if index < 3 else 0.0)
        organ.set(f"gate_v{index}", 0.55 if index < 3 else 0.0)
        organ.set(f"phase_v{index}", (index + 1) / 32.0)
        organ.set(f"pan_v{index}", (-0.2, 0.0, 0.2)[index] if index < 3 else 0.0)
    raw = [value / 18 for value in (8, 0, 6, 4, 0, 0, 0, 0, 0)]
    for index, value in enumerate(raw):
        organ.set(f"amp_d{index}", value + 0.008 * ((raw[index - 1] if index else 0)
                                                 + (raw[index + 1] if index < 8 else 0)))
    for name, value in {"drive": 0.05, "leslie_depth": 0.3, "leslie_target_hz": 0.8,
                        "volume": 0.5, "highcut_hz": 8000.0, "lowcut_hz": 40.0, "tone_tilt": 0.5}.items():
        organ.set(name, value)


def samples(raw, frames):
    require(len(raw) == 2 and all(len(channel) == frames * 4 for channel in raw), "native output length mismatch")
    converted = []
    for channel in raw:
        values = array("f")
        values.frombytes(channel)
        require(all(math.isfinite(value) for value in values), "native output contains NaN/Inf")
        converted.extend(values)
    return converted


def compare_metadata(baseline, candidate):
    require(set(baseline) == set(candidate), "native zone ABI names differ")
    target = "leslie_target_hz"
    require(target in candidate, "Leslie target zone absent")
    require(abs(baseline[target]["min"] - 0.1) < 1e-6, "baseline does not declare original 0.1 Hz minimum")
    require(candidate[target]["min"] == 0.0, "candidate does not declare STOP's 0 Hz minimum")
    for name in baseline:
        for field in ("initial", "min", "max", "step"):
            if name == target and field == "min":
                continue
            require(baseline[name][field] == candidate[name][field], f"unexpected metadata change {name}.{field}")


def check_pair(baseline, candidate, report):
    compare_metadata(baseline.metadata, candidate.metadata)
    report["zone_metadata"] = {"baseline": baseline.metadata, "candidate": candidate.metadata}
    configure(baseline)
    configure(candidate)
    report["parity"] = []
    for name, target in (("slow", 0.8), ("fast", 6.5)):
        baseline.set("leslie_target_hz", target)
        candidate.set("leslie_target_hz", target)
        result = {"mode": name, "frames": 0, "sample_exact": True, "max_abs_delta": 0.0, "peak": 0.0}
        report["parity"].append(result)
        while result["frames"] < SAMPLE_RATE:
            frames = min(BLOCK_FRAMES, SAMPLE_RATE - result["frames"])
            old_raw, new_raw = baseline.compute(frames), candidate.compute(frames)
            old, new = samples(old_raw, frames), samples(new_raw, frames)
            result["sample_exact"] &= old_raw == new_raw
            result["max_abs_delta"] = max(result["max_abs_delta"], max(abs(a - b) for a, b in zip(old, new)))
            result["peak"] = max(result["peak"], max(abs(value) for value in new))
            result["frames"] += frames
        require(result["sample_exact"], f"{name} native waveform parity is not sample-exact")
        require(result["peak"] > 0, f"{name} native output is entirely silent")
    retained = {name: candidate.get(name) for name in ("leslie_depth", "volume", "gate_v0", "gate_v1", "gate_v2")}
    candidate.set("leslie_target_hz", 0.0)
    require(candidate.get("leslie_target_hz") == 0.0, "native STOP target readback was not zero")
    stop = {"target_hz": 0.0, "simulated_seconds": STOP_SECONDS, "frames": 0, "peak": 0.0,
            "all_samples_finite": None, "one_second_windows": [], "retained_controls": retained}
    report["stop"] = stop
    for second in range(STOP_SECONDS):
        window = {"second": second + 1, "frames": 0, "peak": 0.0, "rms": 0.0}
        square_sum = 0.0
        while window["frames"] < SAMPLE_RATE:
            frames = min(BLOCK_FRAMES, SAMPLE_RATE - window["frames"])
            values = samples(candidate.compute(frames), frames)
            window["peak"] = max(window["peak"], max(abs(value) for value in values))
            square_sum += sum(value * value for value in values)
            window["frames"] += frames
            stop["frames"] += frames
        window["rms"] = math.sqrt(square_sum / (2 * SAMPLE_RATE))
        stop["one_second_windows"].append(window)
        stop["peak"] = max(stop["peak"], window["peak"])
        require(window["peak"] > 0, "STOP unexpectedly muted the held organ")
        require(candidate.get("leslie_target_hz") == 0.0, "STOP target changed during native processing")
        require({name: candidate.get(name) for name in retained} == retained, "STOP changed gate/depth/volume controls")
    stop["completed"] = True
    stop["all_samples_finite"] = True


def new_report():
    return {"format": 1, "status": "FAIL", "failures": [], "sample_rate": SAMPLE_RATE,
            "block_frames": BLOCK_FRAMES, "worker_timeout_seconds": WORKER_TIMEOUT,
            "limits": ["Direct native DSP test: no application, Python wrapper, MIDI device, or JACK integration.",
                       "STOP proves declared 0-Hz acceptance/readback and finite continued processing for eight simulated seconds.",
                       "No internal rotor phase/speed readback exists; no exact time-to-rest or mechanical-stop claim.",
                       "No instanceClear, gate release, or bypass is called during mode changes.",
                       "Generated compute-source comparison is separate evidence for unchanged algorithm.",
                       "This does not measure subjective sound, stage load, or controller-to-analogue latency."]}


def _worker(baseline_path, candidate_path, connection):
    report = new_report()
    organs = []
    start = time.monotonic()
    try:
        report["libraries"] = validate_libraries(baseline_path, candidate_path)
        for item in report["libraries"]:
            organs.append(NativeOrgan(item["path"]))
        check_pair(*organs, report)
        report["status"] = "PASS"
    except BaseException as exc:
        report["failures"].append(f"{type(exc).__name__}: {exc}")
    finally:
        for organ in reversed(organs):
            try:
                organ.close()
            except Exception as exc:
                report["status"] = "FAIL"
                report["failures"].append(f"native delete: {type(exc).__name__}: {exc}")
        report["elapsed_seconds"] = time.monotonic() - start
        try:
            connection.send(report)
        finally:
            connection.close()


def run(baseline, candidate):
    report = new_report()
    process = receive = send = None
    try:
        report["libraries"] = validate_libraries(baseline, candidate)
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=_worker, args=(report["libraries"][0]["path"],
                                  report["libraries"][1]["path"], send), daemon=True)
        process.start()
        send.close()
        if not receive.poll(WORKER_TIMEOUT):
            raise TimeoutError("native checker exceeded ten-second worker deadline")
        worker_report = receive.recv()
        require(isinstance(worker_report, dict) and worker_report.get("status") in {"PASS", "FAIL"},
                "native worker returned malformed evidence")
        report = worker_report
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="original AArch64 organ shared library")
    parser.add_argument("--candidate", required=True, help="new distinct AArch64 organ shared library")
    args = parser.parse_args(argv)
    report = run(args.baseline, args.candidate)
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
