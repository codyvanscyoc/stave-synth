#!/usr/bin/env python3
"""Pinned, device-free v1.2/native compatibility tests; no app imports.

Only specifically named AST declarations from the preserved Git object execute.
No constructor/server/device/runtime startup is imported. Reports live in a NEW
directory. This establishes component compatibility, not whole-instrument parity.
"""
from __future__ import annotations

import argparse
import ast
import copy
import ctypes
import dataclasses
from collections import deque
from contextlib import nullcontext
import hashlib
import json
import math
import logging
from pathlib import Path
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = "57bb94cdbd3c9349fc2a47833d659451ebb8738b"
ENVELOPE_TOLERANCE = 2e-12
SEED = 9162026
PIANO_TOLERANCE = 1e-6
COMMANDS: list[dict] = []


def source_from_reference(path: str) -> str:
    result = subprocess.run(["git", "show", f"{REFERENCE}:{path}"], cwd=ROOT,
                            check=True, capture_output=True, text=True, timeout=10)
    return result.stdout


def declarations(source: str, names: list[str], namespace: dict) -> dict:
    tree = ast.parse(source)
    nodes = [node for node in tree.body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    if sorted(node.name for node in nodes) != sorted(names):
        raise RuntimeError("Pinned oracle declaration inventory mismatch")
    module = ast.Module(body=nodes, type_ignores=[])
    namespace = {"__name__": __name__, **namespace}
    exec(compile(module, "pinned-v1.2-component-oracle", "exec"), namespace)
    return namespace


def run(command: list[str], *, input_text: str | None = None, timeout: int = 60):
    result = subprocess.run(command, cwd=ROOT, input=input_text, capture_output=True,
                            text=True, timeout=timeout)
    COMMANDS.append({"command": command, "returncode": result.returncode, "stderr": result.stderr,
                     "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest()})
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed ({result.returncode}): {result.stderr[-4000:]}")
    return result


def envelope_comparison(output: Path, cxx: str) -> dict:
    source = source_from_reference("stave_synth/synth_engine.py")
    oracle = declarations(source, ["ADSRConfig", "_exp_factors", "ADSREnvelope"],
                          {"dataclass": dataclasses.dataclass, "np": np,
                           "SAMPLE_RATE": 48000, "_EXP_FACTOR_CACHE": {}, "_EXP_FACTOR_CACHE_MAX": 64})
    config_type, envelope_type = oracle["ADSRConfig"], oracle["ADSREnvelope"]
    commands: list[str] = []
    expected: list[tuple[float, float, int, int]] = []
    labels: list[str] = []
    rng = random.Random(SEED)
    cases = 0

    def case(config, rate, lengths, name):
        nonlocal cases
        cases += 1
        envelope = envelope_type(config_type(*config), rate)
        commands.append("reset " + " ".join(map(str, [rate, *config])))
        commands.append("guards")
        for index, (operation, value) in enumerate(lengths):
            if operation == "on":
                envelope.trigger(); commands.append("on")
            elif operation == "off":
                envelope.release(); commands.append("off")
            elif operation == "config":
                envelope.config = config_type(*value)
                commands.append("config " + " ".join(map(str, value)))
            elif operation == "process":
                samples = envelope.process(value)
                expected.append((float(samples[-1]), float(envelope.level),
                                 envelope.stage, int(envelope.is_active())))
                labels.append(f"{name}:{index}:frames={value}")
                commands.append(f"process {value}")
            else:
                raise AssertionError(operation)

    # The reference itself is block-cadence dependent. Compare each cadence to
    # its own reference, NOT a misleading claim that ADSR256 equals ADSR512.
    for rate in (44100, 48000, 96000):
        for size in (1, 37, 256, 512):
            for config in ((200, 1500, 80, 500), (0, 0, 0, 0),
                           (.02, .02, 100, .02), (10, 10, 50, 10),
                           (10000, 20000, 1, 30000)):
                sequence = [("process", size), ("off", None), ("on", None)]
                sequence += [("process", size)] * 24
                sequence += [("on", None), ("process", size), ("off", None)]
                sequence += [("process", size)] * 60
                sequence += [("on", None), ("process", size), ("config", (0, 0, 12, 0)),
                             ("process", size), ("off", None), ("process", size)]
                case(config, rate, sequence, f"grid-{rate}-{size}-{config}")
    for index in range(40):
        config = (rng.uniform(0, 10000), rng.uniform(0, 20000),
                  rng.uniform(0, 100), rng.uniform(0, 30000))
        sequence = [("on", None)]
        for step in range(150):
            if step % 19 == 0: sequence.append(("off", None))
            if step % 31 == 0: sequence.append(("on", None))
            sequence.append(("process", rng.choice((1, 2, 37, 127, 256, 511, 512, 4096))))
        case(config, 48000, sequence, f"seeded-{index}")

    fixture = "\n".join(commands) + "\n"
    (output / "envelope-commands.txt").write_text(fixture)
    np.save(output / "envelope-reference.npy", np.asarray(expected), allow_pickle=False)
    binary = output / "envelope_probe"
    compile_result = run([cxx, "-std=c++17", "-O2", "-ffp-contract=off", "-Wall", "-Wextra", "-Werror",
                          "-I", str(ROOT / "native_v2/include"),
                          str(ROOT / "native_v2/tests/envelope_probe.cpp"), "-o", str(binary)])
    result = run([str(binary)], input_text=fixture, timeout=30)
    (output / "envelope-native.txt").write_text(result.stdout)
    actual = np.asarray([[float(item) for item in row.split()] for row in result.stdout.splitlines()])
    wanted = np.asarray(expected)
    if actual.shape != wanted.shape or not np.isfinite(actual).all() or not np.isfinite(wanted).all():
        raise RuntimeError("Invalid envelope result shape or nonfinite sample")
    delta = np.abs(actual[:, :2] - wanted[:, :2])
    bad = np.flatnonzero((delta > ENVELOPE_TOLERANCE).any(axis=1) |
                         (actual[:, 2:] != wanted[:, 2:]).any(axis=1))
    if len(bad):
        index = int(bad[0])
        raise RuntimeError(f"Envelope mismatch {labels[index]}: native={actual[index]}, reference={wanted[index]}")
    return {"cases": cases, "compared_blocks": len(expected), "max_absolute_delta": float(delta.max()),
            "absolute_tolerance": ENVELOPE_TOLERANCE, "stage_active_match": True,
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "fixture_sha256": hashlib.sha256(fixture.encode()).hexdigest(),
            "compile_stderr": compile_result.stderr,
            "scope": "ADSR final sample/state only; block-cadence dependent; not complete voice/audio parity"}


def piano_oracle(output: Path, cc: str):
    synth_source = source_from_reference("stave_synth/synth_engine.py")
    player_source = source_from_reference("stave_synth/fluidsynth_player.py")
    # The Pi's actual Biquad* fast path uses this original C kernel. Compile
    # ONLY the pinned standalone function, not JACK callbacks/startup code.
    bridge_source = source_from_reference("stave_synth/jack_bridge.c")
    marker = "void bridge_biquad("
    if bridge_source.count(marker) != 1: raise RuntimeError("Pinned biquad inventory mismatch")
    begin = bridge_source.index(marker)
    finish = bridge_source.index("\n}\n", begin) + 3
    kernel = bridge_source[begin:finish]
    (output / "reference_biquad.c").write_text(kernel)
    run([cc, "-std=c11", "-O2", "-ffp-contract=off", "-shared", "-fPIC",
         str(output / "reference_biquad.c"), "-o", str(output / "reference_biquad.so")])
    bridge = ctypes.CDLL(str(output / "reference_biquad.so"))
    pd = ctypes.POINTER(ctypes.c_double)
    bridge.bridge_biquad.argtypes = [pd, pd, ctypes.c_int, pd, pd, pd]
    bridge.bridge_biquad.restype = None
    namespace = declarations(synth_source, ["_biquad_run", "BiquadLowpass", "BiquadHighpass", "BiquadPeakingEQ"],
                             {"np": np, "SAMPLE_RATE": 48000, "TWO_PI": 2 * np.pi,
                              "_BIQUAD_C": bridge.bridge_biquad, "_PD": pd, "ctypes": ctypes,
                              "_reference_library": bridge})
    lowpass, highpass, eq = (namespace[name] for name in ("BiquadLowpass", "BiquadHighpass", "BiquadPeakingEQ"))
    tree = ast.parse(player_source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FluidSynthPlayer")
    methods = {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}
    render = copy.deepcopy(methods["render_block"])
    begin = [index for index, node in enumerate(render.body)
             if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute)
             and node.target.attr == "_render_count"]
    if len(begin) != 1: raise RuntimeError("Pinned piano post-processing boundary missing")
    render.body = render.body[begin[0]:]
    render.name = "process_raw"
    render.args.args.insert(1, ast.arg(arg="raw"))
    safe_class = ast.ClassDef(name="PianoOracle", bases=[], keywords=[], decorator_list=[],
                             body=[render, *[copy.deepcopy(methods[name]) for name in
                                            ("_render_chain_faust", "_comp_gain_ramp", "_compress")]])
    module = ast.fix_missing_locations(ast.Module(body=[safe_class], type_ignores=[]))
    target = {"np": np, "math": math}
    exec(compile(module, "pinned-v1.2-piano-post-oracle", "exec"), target)

    class ReferenceFilters:
        """Original Python biquads stand in for Faust's pre-compressor region.

        Original _render_chain_faust and post-processing methods still execute.
        This avoids sharing the candidate native filters with the oracle.
        """
        def __init__(self):
            self.hp = [[highpass(20, .707, 48000) for _ in range(2)] for _ in range(2)]
            self.lp = [[lowpass(20000, .707, 48000) for _ in range(2)] for _ in range(2)]
            self.eq = [[eq(sample_rate=48000) for _ in range(4)] for _ in range(2)]
        def in_buffer(self, n):
            self.input = np.empty((2, n), dtype=np.float64)
            return self.input
        def set_block_params(self, *, gain, lowcut_hz, highcut_hz, eq_bands):
            self.gain, self.bands = gain, eq_bands
            for channel in range(2):
                for flt in self.hp[channel]: flt.set_params(lowcut_hz, .707)
                for flt in self.lp[channel]: flt.set_params(highcut_hz, .707)
                for flt, band in zip(self.eq[channel], eq_bands):
                    flt.set_params(band["freq_hz"], band["gain_db"], band["q"])
        def process_in_place(self, n):
            out = self.input * self.gain
            for channel in range(2):
                for flt in self.hp[channel] + self.lp[channel]: out[channel] = flt.process(out[channel])
                for flt, band in zip(self.eq[channel], self.bands):
                    if band["enabled"]: out[channel] = flt.process(out[channel])
            return np.vstack((out, (out[0] + out[1]) * .5))

    def create():
        player = target["PianoOracle"]()
        player.sample_rate = 48000
        player._volume_cur = .5
        player._render_count = 0
        player._comp_envelope = 0
        player._vel_tracker_cur = .7
        player._vel_bright_last_cutoff = 18000
        player._vel_bright_filter_l = lowpass(18000, .707, 48000)
        player._vel_bright_filter_r = lowpass(18000, .707, 48000)
        player._tremolo_phase = 0
        player._faust_chain = ReferenceFilters()
        player._piano_room = None
        return player
    return create, {"synth": hashlib.sha256(synth_source.encode()).hexdigest(),
                    "player": hashlib.sha256(player_source.encode()).hexdigest(),
                    "biquad_kernel": hashlib.sha256(kernel.encode()).hexdigest()}


def piano_comparison(output: Path, cxx: str, font: Path, fluid_prefix: Path) -> dict:
    faust, cc = shutil.which("faust"), shutil.which("cc")
    if not faust or not cc: raise RuntimeError("Piano comparison requires installed Faust/C compiler")
    faust_prefix = Path(faust).resolve().parent.parent
    if not (fluid_prefix / "include/fluidsynth.h").is_file(): raise RuntimeError("Installed FluidSynth headers missing")
    flags = ["-O2", "-ffp-contract=off", "-I", str(ROOT / "native_v2/include")]
    cppflags = ["-std=c++17", "-Wall", "-Wextra", "-Werror", *flags]
    # Compile the pinned DSP unchanged, double precision just like v1.2.
    dsp = source_from_reference("faust/piano_chain.dsp")
    (output / "piano_chain.dsp").write_text(dsp)
    run([faust, "-double", "-lang", "c", "-cn", "StavePianoChain", "-o", str(output / "piano_chain.c"),
         str(output / "piano_chain.dsp")])
    run([cc, "-std=c11", *flags, "-fPIC", "-DFAUSTFLOAT=double", "-I", str(faust_prefix / "include"),
         "-include", str(ROOT / "faust/faust_cprelude.h"), "-c", str(output / "piano_chain.c"),
         "-o", str(output / "piano_chain.o")])
    library = output / "piano_probe.so"
    run([cxx, *cppflags, "-shared", "-fPIC", "-I", str(faust_prefix / "include"),
         str(ROOT / "native_v2/src/piano_chain.cpp"), str(ROOT / "native_v2/tests/piano_chain_probe.cpp"),
         str(output / "piano_chain.o"), "-o", str(library)])
    capture = output / "capture_piano"
    run([cxx, *cppflags, "-I", str(fluid_prefix / "include"), str(ROOT / "native_v2/tests/capture_piano.cpp"),
         "-L", str(fluid_prefix / "lib"), "-Wl,-rpath," + str(fluid_prefix / "lib"), "-lfluidsynth", "-o", str(capture)])
    captured = subprocess.run([str(capture), str(font)], capture_output=True, timeout=60)
    if captured.returncode: raise RuntimeError("Piano source capture failed: " + captured.stderr.decode(errors="replace"))
    raw = np.frombuffer(captured.stdout, dtype="<i2").copy()
    if raw.size != 512 * 512 * 2 or not np.any(raw): raise RuntimeError("Invalid/silent piano capture")
    (output / "piano-source-s16le.raw").write_bytes(captured.stdout)
    dll = ctypes.CDLL(str(library))
    pointer = ctypes.POINTER(ctypes.c_double)
    dll.piano_chain_create.argtypes = [ctypes.c_uint32, ctypes.c_uint32]; dll.piano_chain_create.restype = ctypes.c_void_p
    dll.piano_chain_delete.argtypes = [ctypes.c_void_p]; dll.piano_chain_delete.restype = None
    dll.piano_chain_configure.argtypes = [ctypes.c_void_p, pointer, ctypes.c_uint32]; dll.piano_chain_configure.restype = ctypes.c_int
    dll.piano_chain_process.argtypes = [ctypes.c_void_p, pointer, pointer, pointer, pointer, ctypes.c_uint32]
    dll.piano_chain_process.restype = ctypes.c_int
    dll.piano_chain_state.argtypes = [ctypes.c_void_p, pointer]; dll.piano_chain_state.restype = None
    dll.piano_chain_clear.argtypes = [ctypes.c_void_p]; dll.piano_chain_clear.restype = None
    dll.piano_chain_healthy.argtypes = [ctypes.c_void_p]; dll.piano_chain_healthy.restype = ctypes.c_int
    create_oracle, reference_hashes = piano_oracle(output, cc)
    ptr = lambda array: array.ctypes.data_as(pointer)
    results = []
    for size in (512, 256):
        owner = dll.piano_chain_create(48000, size)
        if not owner: raise RuntimeError("Native piano-chain construction failed")
        oracle = create_oracle()
        # Same physical transition times at either cadence, but separate oracle
        # comparisons: the original compressor intentionally depends on cadence.
        values = np.array([.5, 20, 20000, 0, -20, 3, 10, 80, 0, 18, 0, 1, 0, .5, .7, 0, 0,
                           150, 2, .8, 1, 300, -2.5, 1, 1, 2800, -3, 1.5, 1, 10000, -1.5, .7, 1], dtype=np.float64)
        transitions = {12: {0: .8}, 36: {3: 1, 4: -24, 8: 2, 10: 6, 11: .7},
                       60: {1: 40, 2: 14000, 18: -2}, 80: {24: 0}, 96: {21: 600, 22: 4},
                       112: {24: 1}, 136: {11: .25}, 152: {3: 0}, 168: {3: 1},
                       192: {12: 1, 13: .8, 14: .15}, 224: {14: .95},
                       248: {15: 5.5, 16: .5}, 280: {0: 0}, 312: {0: .85},
                       344: {12: 0, 16: 0}, 368: {12: 1, 16: .25},
                       400: {0: .5, 3: 0, 12: 0, 16: 0}, 440: {0: 0}}
        expected = np.empty((2, raw.size // 2)); actual = np.empty_like(expected)
        state_delta = np.zeros(6)
        try:
            for frame in range(0, raw.size // 2, size):
                if frame % 512 == 0:
                    for index, value in transitions.get(frame // 512, {}).items(): values[index] = value
                if not dll.piano_chain_configure(owner, ptr(values), len(values)): raise RuntimeError("Valid piano config rejected")
                oracle.volume, oracle.lowcut_hz, oracle.highcut_hz = values[:3]
                fields = ["comp_enabled", "comp_threshold_db", "comp_ratio", "comp_attack_ms", "comp_release_ms",
                          "comp_makeup_db", "comp_knee_db", "comp_drive_db", "comp_wet", "vel_bright_enabled",
                          "vel_bright_amount", "_vel_tracker", "tremolo_hz", "tremolo_depth"]
                for name, value in zip(fields, values[3:17]): setattr(oracle, name, value)
                oracle.eq_bands = [dict(zip(("freq_hz", "gain_db", "q", "enabled"), values[17+4*i:21+4*i])) for i in range(4)]
                segment = raw[frame*2:(frame+size)*2]
                expected[:, frame:frame+size] = oracle.process_raw(segment, size)
                left = np.ascontiguousarray(segment[::2], dtype=np.float64) / 32768
                right = np.ascontiguousarray(segment[1::2], dtype=np.float64) / 32768
                out_l, out_r = np.empty(size), np.empty(size)
                if not dll.piano_chain_process(owner, ptr(left), ptr(right), ptr(out_l), ptr(out_r), size):
                    raise RuntimeError("Native piano render failed")
                actual[:, frame:frame+size] = out_l, out_r
                state = np.empty(6); dll.piano_chain_state(owner, ptr(state))
                reference_state = [oracle._volume_cur, oracle._comp_envelope, getattr(oracle, "_prev_comp_gain", 0),
                                   oracle._vel_tracker_cur, oracle._vel_bright_last_cutoff, oracle._tremolo_phase]
                if not np.isfinite(state).all() or not np.isfinite(reference_state).all():
                    raise RuntimeError("Nonfinite candidate/reference piano state")
                state_delta = np.maximum(state_delta, np.abs(state - reference_state))
            if not np.isfinite(actual).all() or not np.isfinite(expected).all():
                raise RuntimeError("Nonfinite candidate/reference piano-chain output")
            delta = np.abs(actual - expected)
            np.save(output / f"piano-reference-{size}.npy", expected, allow_pickle=False)
            np.save(output / f"piano-native-{size}.npy", actual, allow_pickle=False)
            if float(delta.max()) > PIANO_TOLERANCE or float(state_delta.max()) > PIANO_TOLERANCE:
                raise RuntimeError(f"Piano mismatch at{size}: sample={delta.max()}, states={state_delta.tolist()}")
            # Failed configuration must be transactional. Numeric render faults
            # are terminal and safely silent; clear must not hide the fault.
            state = np.empty(6); dll.piano_chain_state(owner, ptr(state))
            invalid = values.copy(); invalid[0] = float("nan")
            if dll.piano_chain_configure(owner, ptr(invalid), 33): raise RuntimeError("NaN config accepted")
            after = np.empty(6); dll.piano_chain_state(owner, ptr(after))
            if not np.array_equal(state, after): raise RuntimeError("Rejected config changed state")
            left[0] = float("nan")
            if dll.piano_chain_process(owner, ptr(left), ptr(right), ptr(out_l), ptr(out_r), size):
                raise RuntimeError("NaN input accepted")
            if np.any(out_l) or np.any(out_r) or dll.piano_chain_healthy(owner): raise RuntimeError("Numeric fault not safely latched")
            dll.piano_chain_clear(owner)
            if dll.piano_chain_healthy(owner): raise RuntimeError("Clear hid numeric fault")
            results.append({"block_frames": size, "compared_samples": int(actual.size),
                            "max_absolute_delta": float(delta.max()), "rms_delta": float(np.sqrt(np.mean(delta**2))),
                            "state_max_delta": state_delta.tolist(), "peak": float(np.max(np.abs(actual)))})
        finally:
            dll.piano_chain_delete(owner)
    return {"source_capture": captured.stderr.decode().strip(), "raw_sha256": hashlib.sha256(captured.stdout).hexdigest(),
            "raw_full_scale_samples": int(np.count_nonzero((raw == -32768) | (raw == 32767))),
            "soundfont_sha256": hashlib.sha256(font.read_bytes()).hexdigest(), "reference_sources": reference_hashes,
            "dsp_sha256": hashlib.sha256(dsp.encode()).hexdigest(), "absolute_tolerance": PIANO_TOLERANCE,
            "runs": results, "scope": "Identical int16 input; native dry piano post-processing only; no pedal/Fluid-float/room/shared-FX parity"}


def note_ownership_comparison(output: Path, cxx: str) -> dict:
    jack_source = source_from_reference("stave_synth/jack_engine.py")
    fixture_source = source_from_reference("tests/test_midi_notes.py")
    tree = ast.parse(jack_source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "JackEngine")
    methods = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in ("_midi_loop_body", "panic")]
    if len(methods) != 2: raise RuntimeError("Pinned MIDI method inventory mismatch")
    namespace = {"ctypes": ctypes, "time": SimpleNamespace(sleep=lambda _: None),
                 "_MIDI_POLL_S": .002, "SAMPLE_RATE": 48000,
                 "SynthEngine": SimpleNamespace(split_weight=lambda *args: 1.0),
                 "logger": logging.getLogger("native-v2-midi-oracle")}
    exec(compile(ast.Module(body=methods, type_ignores=[]), "pinned-midi-owner", "exec"), namespace)
    helpers = declarations(fixture_source, ["_Bridge", "_Synth", "_Engine"],
                           {"_METHODS": namespace, "deque": deque, "SimpleNamespace": SimpleNamespace})
    owner_type = helpers["_Engine"]
    commands = []
    expected = []
    owner = None

    def apply(command):
        nonlocal owner
        commands.append(command)
        parts = command.split()
        operation = parts[0]
        if operation == "reset":
            owner = owner_type(); owner.min_velocity = 10
        owner.synth.events.clear(); owner.piano_events.clear()
        if operation == "config": owner.transpose, owner.piano_octave, owner.min_velocity = map(int, parts[1:])
        elif operation == "on": owner.feed((0x90, int(parts[1]), int(parts[2])))
        elif operation == "off": owner.feed((0x80, int(parts[1]), 0))
        elif operation in ("sustain", "sostenuto"):
            owner.feed((0xB0, 64 if operation == "sustain" else 66, 127 if int(parts[1]) else 0))
        events = []
        for layer, source in enumerate((owner.synth.events, owner.piano_events)):
            for kind, note, velocity in source: events.append((layer, int(kind == "note_on"), note, velocity))
        keys = []
        for raw in sorted(set(owner._note_map) | owner._physically_held | owner._sustained_notes | owner._sostenuto_held):
            keys.append((raw, int(raw in owner._note_map), int(raw in owner._physically_held),
                         int(raw in owner._sustained_notes), int(raw in owner._sostenuto_held),
                         *owner._note_map.get(raw, (-1, -1))))
        # Releases within one pedal command have no intervening render. Compare
        # their multiset; Python set iteration order isn't the musical contract.
        expected.append((sorted(events), keys, (int(owner._sustain_on), int(owner._sostenuto_on))))

    scenarios = [
        ["on 60 100", "on 60 80", "off 60", "off 60"],
        ["sustain 1", "on 60 100", "off 60", "config 0 1 10", "on 60 70", "off 60", "sustain 0"],
        ["on 60 100", "sostenuto 1", "sustain 1", "off 60", "sustain 0", "sostenuto 0"],
        ["on 60 100", "sostenuto 1", "off 60", "sustain 1", "sostenuto 0", "sustain 0"],
        ["on 60 100", "sostenuto 1", "off 60", "on 62 90", "sostenuto 1", "off 62", "sostenuto 0"],
        ["on 60 100", "sustain 1", "sostenuto 1", "sostenuto 0", "sustain 0", "off 60"],
        ["on 60 100", "config 2 -1 10", "on 60 100", "on 60 0"],
        ["on 0 100", "on 127 100", "config -24 -3 10", "on 0 100", "off 0", "off 127"],
        ["on 60 9", "off 60", "on 60 10", "on 60 1", "off 60"],
    ]
    for scenario in scenarios:
        apply("reset")
        for command in scenario: apply(command)
    rng = random.Random(SEED)
    for _ in range(32):
        apply("reset")
        for step in range(200):
            choice = rng.randrange(10)
            note = rng.choice((0, 36, 48, 60, 62, 64, 67, 72, 127))
            if choice < 4: apply(f"on {note} {rng.randrange(128)}")
            elif choice < 7: apply(f"off {note}")
            elif choice == 7: apply(f"sustain {rng.randrange(2)}")
            elif choice == 8: apply(f"sostenuto {rng.randrange(2)}")
            else: apply(f"config {rng.randrange(-24, 25)} {rng.randrange(-3, 4)} {rng.randrange(1, 25)}")
        for note in range(128): apply(f"off {note}")
        apply("sostenuto 0"); apply("sustain 0")
        if expected[-1][1]: raise RuntimeError("Reference cleanup left held notes")
    fixture = "\n".join(commands) + "\n"
    (output / "stage-note-commands.txt").write_text(fixture)
    binary = output / "stage_notes_probe"
    run([cxx, "-std=c++17", "-O2", "-ffp-contract=off", "-Wall", "-Wextra", "-Werror",
         "-I", str(ROOT / "native_v2/include"), str(ROOT / "native_v2/tests/stage_notes_probe.cpp"), "-o", str(binary)])
    result = run([str(binary)], input_text=fixture, timeout=30)
    rows = result.stdout.splitlines()
    if len(rows) != len(expected): raise RuntimeError("MIDI oracle result count mismatch")
    for index, (row, wanted) in enumerate(zip(rows, expected)):
        events_text, rest = row.removeprefix("E").split(" K", 1)
        keys_text, pedals_text = rest.split(" P ", 1)
        events = sorted(tuple(map(float, value.split(","))) for value in events_text.split())
        keys = [tuple(map(int, value.split(","))) for value in keys_text.split()]
        pedals = tuple(map(int, pedals_text.split(",")))
        if events != wanted[0] or keys != wanted[1] or pedals != wanted[2]:
            raise RuntimeError(f"MIDI mismatch at command{index} {commands[index]}: native={(events, keys, pedals)}, reference={wanted}")
    (output / "stage-note-native.txt").write_text(result.stdout)
    return {"compared_commands": len(commands), "seeded_sequences": 32, "focused_sequences": len(scenarios),
            "exact_note_destinations_velocities_and_ownership": True,
            "reference_sha256": hashlib.sha256(jack_source.encode()).hexdigest(),
            "fixture_helpers_sha256": hashlib.sha256(fixture_source.encode()).hexdigest(),
            "fixture_sha256": hashlib.sha256(fixture.encode()).hexdigest(),
            "scope": "v1.2 collapsed-channel key/transpose/pedal ownership; no split-weight/audio/UI/clock parity"}


def voice_oracle_components() -> tuple[str, dict]:
    source = source_from_reference("stave_synth/synth_engine.py")
    namespace = declarations(source, ["ADSRConfig", "_exp_factors", "ADSREnvelope", "Voice"],
                             {"dataclass": dataclasses.dataclass, "field": dataclasses.field, "np": np,
                              "SAMPLE_RATE": 48000, "TWO_PI": 2 * np.pi,
                              "_EXP_FACTOR_CACHE": {}, "_EXP_FACTOR_CACHE_MAX": 64})
    cls = next(node for node in ast.parse(source).body
               if isinstance(node, ast.ClassDef) and node.name == "SynthEngine")
    methods = {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}
    namespace["logger"] = logging.getLogger("native-v2-voice-oracle")
    exec(compile(ast.Module(body=[methods[name] for name in
                                ("_note_on_locked", "note_off", "all_notes_off")], type_ignores=[]),
                 "pinned-v1.2-voice-events", "exec"), namespace)
    render = methods["_render_locked"]
    loops = [node for node in ast.walk(render) if isinstance(node, ast.For)
             and isinstance(node.target, ast.Name) and node.target.id == "voice"
             and ast.unparse(node.iter) == "self.voices"]
    if len(loops) != 1: raise RuntimeError("Pinned voice render-loop inventory mismatch")
    loop = copy.deepcopy(loops[0])
    # Execute the actual inactive detection, two envelopes, skip test and Faust
    # gate expressions. Exclude waveform/LFO/drift/effects processing explicitly.
    if len(loop.body) < 4 or ast.unparse(loop.body[3].test) != "skip_voices":
        raise RuntimeError("Pinned voice envelope boundary changed")
    faust_branches = [node for node in loop.body if isinstance(node, ast.If)
                      and ast.unparse(node.test) == "use_faust"]
    if len(faust_branches) != 1: raise RuntimeError("Pinned voice gate branch changed")
    gate_body = faust_branches[0].body
    if (len(gate_body) < 6 or "set_voice" not in ast.unparse(gate_body[4]) or
            "set_shimmer_weight" not in ast.unparse(gate_body[5])):
        raise RuntimeError("Pinned voice gate expressions changed")
    loop.body = loop.body[:4] + ast.parse("base_freq = voice.note").body + gate_body[:6]
    begin_ast = ast.parse("def begin(self, n_samples, skip_voices):\n    dead_voices = []\n    voice_idx = 0\n")
    begin_ast.body[0].body += [loop, *ast.parse("self.dead_voices = dead_voices").body]
    cleanup = [node for node in render.body if isinstance(node, ast.For)
               and ast.unparse(node.iter) == "dead_voices"]
    if len(cleanup) != 1: raise RuntimeError("Pinned voice cleanup boundary changed")
    end_ast = ast.parse("def end(self):\n    dead_voices = self.dead_voices\n")
    end_ast.body[0].body.append(copy.deepcopy(cleanup[0]))
    faust_render = [node for node in render.body if isinstance(node, ast.If)
                    and ast.unparse(node.test) == "use_faust"
                    and any(isinstance(x, ast.Assign) and any(ast.unparse(t) == "active_slots" for t in x.targets)
                            for x in node.body)]
    if len(faust_render) != 1: raise RuntimeError("Pinned active-slot cleanup boundary changed")
    prepare_ast = ast.parse("def prepare(self):\n    pass\n")
    prepare_ast.body[0].body = copy.deepcopy(faust_render[0].body[:2])
    exec(compile(ast.fix_missing_locations(begin_ast), "pinned-v1.2-envelope-render", "exec"), namespace)
    exec(compile(ast.fix_missing_locations(end_ast), "pinned-v1.2-voice-cleanup", "exec"), namespace)
    exec(compile(ast.fix_missing_locations(prepare_ast), "pinned-v1.2-active-slot-cleanup", "exec"), namespace)
    return source, namespace


def voice_comparison(output: Path, cxx: str) -> dict:
    source, namespace = voice_oracle_components()
    config_type, envelope_type, voice_type = (namespace[name] for name in ("ADSRConfig", "ADSREnvelope", "Voice"))
    expected, commands, events = [], [], []
    owner = None

    class Bank:
        def clear_voice(self, slot): events.append(("clear", slot))
        def randomize_phase(self, slot): events.append(("new", slot))
        def randomize_lfo_phase(self, slot): pass
        def set_voice(self, slot, note, first, second): events.append(("gate", slot, note, first, second))
        def set_shimmer_weight(self, slot, weight):
            if events[-1][0:2] != ("gate", slot): raise RuntimeError("Gate/weight sequence changed")
            events[-1] += (weight,)

    def apply(command):
        nonlocal owner
        commands.append(command); events.clear()
        parts = command.split(); op = parts[0]
        if op == "reset":
            rate, limit = map(int, parts[1:]); first, second = config_type(), config_type()
            owner = SimpleNamespace(voices=[], max_voices=limit, _age_counter=0, lfo_key_sync=False,
                                    lfo2_key_sync=False, _faust_osc_bank=Bank(), _faust_slot_free=list(range(limit)),
                                    unison_voices=3, _render_lock=nullcontext())
            owner._voice_pool = [voice_type(adsr_osc1=envelope_type(first, rate),
                                          adsr_osc2=envelope_type(second, rate)) for _ in range(limit)]
        elif op == "config":
            values = list(map(float, parts[1:])); first, second = config_type(*values[:4]), config_type(*values[4:])
            for voice in owner.voices + owner._voice_pool:
                voice.adsr_osc1.config, voice.adsr_osc2.config = first, second
        elif op == "on":
            events.append(("key",))
            namespace["_note_on_locked"](owner, int(parts[1]), *map(float, parts[2:]))
        elif op == "off": namespace["note_off"](owner, int(parts[1]))
        elif op == "all": namespace["all_notes_off"](owner)
        elif op == "begin": namespace["begin"](owner, int(parts[1]), bool(int(parts[2])))
        elif op == "end": namespace["end"](owner)
        else: raise AssertionError(op)
        states = [(v.faust_slot, v.note, v.velocity, v.age, v.adsr_osc1.stage, float(v.adsr_osc1.level),
                   v.adsr_osc2.stage, float(v.adsr_osc2.level), v.osc1_weight, v.osc2_weight, v.shimmer_weight)
                  for v in owner.voices]
        expected.append((list(events), states))

    rng = random.Random(SEED)
    np.random.seed(SEED)  # Only old fallback phase initialization consumes this.
    def block(size=512, skip=0): apply(f"begin {size} {skip}"); apply("end")
    for rate in (44100, 48000, 96000):
        for limit in (1, 2, 3, 12):
            apply(f"reset {rate} {limit}")
            # Sustain live edit must retain OLD envelope.level for later release.
            apply("config 0 0 40 50 0 0 80 100")
            apply("on 60 1 1 0.5 0.25"); block(); block()
            apply("config 0 0 10 50 0 0 90 100"); block()
            apply("off 60"); block()
            # Released same-pitch note becomes a distinct voice; held repeat
            # retains its original weights, phase and envelope level.
            apply("on 60 0.5 0 1 0.7"); block()
            apply("on 60 0.7 1 0 1"); block(256)
            for index in range(limit + 2): apply(f"on {36 + index} 0.8 1 1 1"); block(37)
            for index in range(limit + 2): apply(f"off {36 + index}")
            apply("on 72 0.9 1 0.3 0.2"); block()
            apply("all")
            for _ in range(70): block(4096)
            if owner.voices: raise RuntimeError("Reference voice cleanup failed")
            # All-off envelopes stay in the active list until NEXT begin/end.
            apply("config 0 0 0 0 0 0 0 0")
            apply("on 60 1 1 1 1"); apply("off 60"); block()
            apply("on 60 0.5 0.1 0.2 0.3"); block(1); block(512, 1); block()
            for step in range(350):
                choice = rng.randrange(10)
                if choice < 5:
                    apply(f"on {rng.randrange(48, 72)} {rng.random()} {rng.random()} {rng.random()} {rng.random()}")
                elif choice < 7: apply(f"off {rng.randrange(48, 72)}")
                elif choice == 7: apply("all")
                elif choice == 8:
                    apply("config " + " ".join(map(str, [rng.choice((0, .02, 50, 200)), rng.choice((0, 50, 1500)),
                          rng.uniform(0, 100), rng.choice((0, 100, 500)), rng.choice((0, 10, 300)),
                          rng.choice((0, 50, 2000)), rng.uniform(0, 100), rng.choice((0, 200, 700))])))
                else: block(rng.choice((1, 37, 256, 512, 4096)), rng.randrange(2))
            apply("config 0 0 0 0 0 0 0 0"); apply("all"); block(); block()
            if owner.voices: raise RuntimeError("Reference randomized cleanup failed")
    fixture = "\n".join(commands) + "\n"
    (output / "voice-commands.txt").write_text(fixture)
    binary = output / "stage_voices_probe"
    run([cxx, "-std=c++17", "-O2", "-ffp-contract=off", "-Wall", "-Wextra", "-Werror",
         "-I", str(ROOT / "native_v2/include"), str(ROOT / "native_v2/tests/stage_voices_probe.cpp"), "-o", str(binary)])
    result = run([str(binary)], input_text=fixture, timeout=30)
    (output / "voice-native.txt").write_text(result.stdout)
    for invalid in ("reset\n", "reset 48000 13\n", "reset 48000 12\non 60\n",
                    "reset 48000 12\nbegin 512 2\n", "reset 48000 12 extra\n"):
        refused = subprocess.run([str(binary)], input=invalid, text=True, capture_output=True, timeout=5)
        if refused.returncode <= 0: raise RuntimeError("Voice probe failed malformed-input refusal")
    rows = result.stdout.splitlines()
    if len(rows) != len(expected): raise RuntimeError("Voice oracle result count mismatch")
    maximum = 0.0
    for index, (row, wanted) in enumerate(zip(rows, expected)):
        ev, states = row.removeprefix("E").split(" V", 1)
        actual_events = [(fields[0], *map(float, fields[1:])) for item in ev.split() if (fields := item.split(","))]
        actual_states = [tuple(map(float, item.split(","))) for item in states.split()]
        for actual, target in ((actual_events, wanted[0]), (actual_states, wanted[1])):
            if len(actual) != len(target): raise RuntimeError(f"Voice count mismatch at {index}: {commands[index]}")
            for a, b in zip(actual, target):
                if len(a) != len(b): raise RuntimeError(f"Voice field count mismatch at {index}")
                for av, bv in zip(a, b):
                    delta = abs(av - bv) if not isinstance(av, str) else (0 if av == bv else math.inf)
                    maximum = max(maximum, delta)
                    if len(a) != len(b) or not math.isfinite(delta) or delta > ENVELOPE_TOLERANCE:
                        raise RuntimeError(f"Voice mismatch at {index} {commands[index]}: native={a}, reference={b}")
    return {"compared_commands": len(commands), "rates": [44100, 48000, 96000], "voice_limits": [1, 2, 3, 12],
            "maximum_state_gate_delta": maximum, "absolute_tolerance": ENVELOPE_TOLERANCE,
            "malformed_probe_refusals": 5,
            "reference_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "fixture_sha256": hashlib.sha256(fixture.encode()).hexdigest(),
            "scope": "Voice/retrigger/steal/release, two envelope gates and post-render slot retirement; no phase/LFO/drift/audio parity"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="New directory only; parent must exist")
    parser.add_argument("--soundfont", type=Path, help="Explicit existing SF2 also exercises real piano processing")
    parser.add_argument("--fluidsynth-prefix", type=Path, default=Path("/opt/homebrew/opt/fluid-synth"))
    args = parser.parse_args()
    if args.soundfont and not args.soundfont.is_file(): parser.error("SoundFont must already exist")
    if args.output_dir:
        output = args.output_dir.absolute()
        output.mkdir(mode=0o700, exist_ok=False)
    else:
        output = Path(tempfile.mkdtemp(prefix="stave-native-compat-"))
    paths = [Path(__file__), *sorted((ROOT / "native_v2").rglob("*.hpp")),
             *sorted((ROOT / "native_v2").rglob("*.cpp")), ROOT / "faust/faust_cprelude.h"]
    def hashes():
        return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    report = {"status": "running", "reference_commit": REFERENCE, "seed": SEED,
              "host": platform.platform(), "native_source_sha256": hashes(),
              "commands": COMMANDS,
              "whole_instrument_parity": False, "pi4_qualified": False, "checks": {}}
    print(f"Offline comparison artifacts: {output}", flush=True)
    start = time.monotonic()
    try:
        cxx = shutil.which("c++")
        if not cxx: raise RuntimeError("C++ compiler unavailable; nothing installed")
        report["compiler"] = run([cxx, "--version"]).stdout
        report["checks"]["envelope"] = envelope_comparison(output, cxx)
        print("PASS: pinned envelope comparison", flush=True)
        report["checks"]["stage_notes"] = note_ownership_comparison(output, cxx)
        print("PASS: pinned stage-note/pedal comparison", flush=True)
        report["checks"]["stage_voices"] = voice_comparison(output, cxx)
        print("PASS: pinned voice ownership/gate comparison", flush=True)
        if args.soundfont:
            font = args.soundfont.resolve()
            before_font = hashlib.sha256(font.read_bytes()).hexdigest()
            report["checks"]["piano"] = piano_comparison(output, cxx, font, args.fluidsynth_prefix)
            if hashlib.sha256(font.read_bytes()).hexdigest() != before_font:
                raise RuntimeError("SoundFont changed during comparison")
            print("PASS: pinned dry-piano comparison", flush=True)
        if hashes() != report["native_source_sha256"]: raise RuntimeError("Source changed during comparison")
        report["status"] = "passed_component_comparison_only"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = str(error)
        print(str(error), file=sys.stderr)
    finally:
        report["elapsed_seconds"] = time.monotonic() - start
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report["checks"], indent=2))
        print(f"Report: {output / 'report.json'}")
    return 0 if report["status"] == "passed_component_comparison_only" else 1


if __name__ == "__main__":
    raise SystemExit(main())
