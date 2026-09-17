#!/usr/bin/env python3
"""Offline piano-room port vs pinned v1.2 Faust wrapper AND actual mix code.

No app import, drivers, network, production paths or package installation.
Creates only a new evidence directory. Existing source-graph .npy stems may
be supplied explicitly for an additional composed piano-chain/room comparison.
"""
from __future__ import annotations
import argparse
import ast
from contextlib import nullcontext
import ctypes as ct
import hashlib
import json
from pathlib import Path
import platform
import shutil
import tempfile
from types import SimpleNamespace
import numpy as np
from cffi import FFI
import compare_native_v2 as base

ROOT = base.ROOT
TOLERANCE = 1e-6
PD = ct.POINTER(ct.c_double)


def build(output):
    cc, cxx, faust = (shutil.which(n) for n in ("cc", "c++", "faust"))
    if not all((cc, cxx, faust)): raise RuntimeError("Installed C/C++ and Faust required")
    include = Path(faust).resolve().parent.parent / "include"
    dsp, generated, obj = (output / ("piano_room." + ext) for ext in ("dsp", "c", "o"))
    dsp.write_text(base.source_from_reference("faust/piano_room.dsp"))
    base.run([faust, "-lang", "c", "-cn", "StavePianoRoom", "-o", str(generated), str(dsp)])
    common = ["-O2", "-ffp-contract=off", "-fPIC", "-I", str(include)]
    base.run([cc, "-std=c11", *common, "-include", str(ROOT / "faust/faust_cprelude.h"),
              "-c", str(generated), "-o", str(obj)])
    reference = output / "reference_room.so"
    base.run([cc, "-shared", str(obj), "-o", str(reference)])
    candidate, guard = output / "native_room.so", output / "test_piano_room"
    for dest, test, extra in ((candidate, "piano_room_probe.cpp", ["-shared"]),
                              (guard, "test_piano_room.cpp", ["-fsanitize=undefined", "-fno-sanitize-recover=all"])):
        base.run([cxx, "-std=c++17", *common, "-Wall", "-Wextra", "-Werror", *extra,
                  "-I", str(ROOT / "native_v2/include"), str(ROOT / "native_v2/src/piano_room.cpp"),
                  str(ROOT / "native_v2/tests" / test), str(obj), "-o", str(dest)])
    return reference, candidate, guard, base.run([cxx, "--version"]).stdout, base.run([faust, "--version"]).stdout


def oracle(library):
    wrapper = base.source_from_reference("stave_synth/faust_piano_room.py")
    definitions = [n.value.args[0].value for n in ast.parse(wrapper).body if isinstance(n, ast.Expr)
                   and isinstance(n.value, ast.Call) and ast.unparse(n.value.func) == "_ffi.cdef"]
    if len(definitions) != 1: raise RuntimeError("Pinned room ABI inventory mismatch")
    ffi = FFI(); ffi.cdef(definitions[0])
    namespace = base.declarations(wrapper, ["FaustPianoRoom", "_install_ui_callbacks"],
                                  {"np": np, "_ffi": ffi, "_lib": ffi.dlopen(str(library))})
    player = base.source_from_reference("stave_synth/fluidsynth_player.py")
    cls = next(n for n in ast.parse(player).body if isinstance(n, ast.ClassDef) and n.name == "FluidSynthPlayer")
    # Match the sole render-level room branch, not other clear/settings methods.
    candidates = [(m, n) for m in cls.body if isinstance(m, ast.FunctionDef) for n in m.body
                  if isinstance(n, ast.If) and ast.unparse(n.test) == "self._piano_room is not None"
                  and any(isinstance(x, ast.Name) and x.id == "n_samples" for x in ast.walk(n))]
    if len(candidates) != 1: raise RuntimeError("Pinned room mix boundary mismatch")
    _, branch = candidates[0]
    fn = ast.parse("def render(self, left, right, n_samples):\n    pass\n").body[0]
    fn.body = [branch, ast.parse("return np.array([left, right])").body[0]]
    ns = {"np": np}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), "pinned-room-mix", "exec"), ns)
    def create():
        return SimpleNamespace(_piano_room=namespace["FaustPianoRoom"](48000),
                               _piano_room_lock=nullcontext(), piano_room_enabled=True,
                               _piano_room_was_enabled=True, reverb_dry_wet=.4,
                               _piano_room_wet_cur=.4, sample_rate=48000)
    return create, ns["render"], {p: hashlib.sha256(s.encode()).hexdigest() for p, s in
                               (("wrapper", wrapper), ("player", player))}


def native_api(path):
    lib = ct.CDLL(str(path))
    for name, result, args in (("create", ct.c_void_p, [ct.c_uint]), ("delete", None, [ct.c_void_p]),
        ("configure", ct.c_int, [ct.c_void_p, ct.c_int, ct.c_double, ct.c_double, ct.c_double]),
        ("process", ct.c_int, [ct.c_void_p, PD, PD, PD, PD, ct.c_uint]),
        ("clear", None, [ct.c_void_p]), ("wet", ct.c_double, [ct.c_void_p])):
        fn = getattr(lib, "room_" + name); fn.restype = result; fn.argtypes = args
    return lib


def compare(output, lib, create, render, frames, signal, label):
    reference = create(); native = lib.room_create(frames)
    if not native: raise RuntimeError("Native room construction failed")
    actual = np.zeros_like(signal); expected = np.zeros_like(signal)
    # Canonical512-frame timestamps; same gestures at both tested cadences.
    controls = {0:(1,.4,.5,.6), 24:(1,.85,1,.1), 64:(1,0,1,.1),
                80:(1,.001,0,.99), 96:(1,.00101,0,.99), 112:(1,1,1,0),
                144:(0,1,1,0), 176:(1,.65,.3,.8), 208:(1,0,.3,.8),
                224:(1,.9,.9,.2), 288:(0,.9,.9,.2), 289:(1,.9,.9,.2),
                320:(1,.4,.5,.6)}
    clears = {256, 512}
    fixture = {"frames": frames, "controls_canonical_512": controls, "clear_canonical_512": sorted(clears)}
    (output / f"{label}-{frames}-fixture.json").write_text(json.dumps(fixture, indent=2) + "\n")
    wet_delta = 0.
    try:
        for start in range(0, signal.shape[1], frames):
            n = min(frames, signal.shape[1] - start)
            canonical = start // 512
            if start % 512 == 0:
                if canonical in controls:
                    enabled, wet, size, damp = controls[canonical]
                    if not lib.room_configure(native, enabled, wet, size, damp): raise RuntimeError("Config rejected")
                    reference.piano_room_enabled = bool(enabled); reference.reverb_dry_wet = wet
                    reference._piano_room.set_zone("size", size); reference._piano_room.set_zone("damp", damp)
                if canonical in clears:
                    lib.room_clear(native); reference._piano_room.clear()
            chunk = np.ascontiguousarray(signal[:, start:start+n])
            dest = np.empty((2, n))
            args = [c.ctypes.data_as(PD) for c in (*chunk, *dest)]
            if not lib.room_process(native, *args, n): raise RuntimeError("Native processing failed")
            actual[:, start:start+n] = dest
            expected[:, start:start+n] = render(reference, *chunk, n)
            wet_delta = max(wet_delta, abs(lib.room_wet(native) - reference._piano_room_wet_cur))
    finally:
        lib.room_delete(native)
    np.save(output / f"{label}-{frames}-native.npy", actual, allow_pickle=False)
    np.save(output / f"{label}-{frames}-reference.npy", expected, allow_pickle=False)
    delta = float(np.max(np.abs(actual - expected)))
    if not np.isfinite(actual).all() or not np.isfinite(expected).all() or delta > TOLERANCE or wet_delta > 2e-12:
        raise RuntimeError(f"Room comparison failed: audio={delta}, wet={wet_delta}")
    # The synthetic fixture supplies silence after canonical block384: require
    # nonzero release tail before the explicit block512 clear, exact zero after.
    tail_peak = float(np.max(np.abs(actual[:, 384*512:512*512])))
    if label == "synthetic" and tail_peak <= 1e-8: raise RuntimeError("Fixture failed to excite release tail")
    if np.any(signal[:, 512*512:] != 0) or np.any(actual[:, 512*512:] != 0):
        raise RuntimeError("Hard-clear silence verification failed")
    return {"fixture": label, "frames": frames, "samples_per_channel": signal.shape[1], "max_absolute_delta": delta,
            "tolerance": TOLERANCE, "wet_smoother_delta": wet_delta, "release_tail_peak": tail_peak,
            "post_clear_exact_silence": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--source-stems", type=Path, help="Existing source graph 7-channel .npy; no pickle")
    args = parser.parse_args()
    if args.output_dir:
        output = args.output_dir.expanduser().resolve(); output.mkdir(mode=0o700, exist_ok=False)
    else: output = Path(tempfile.mkdtemp(prefix="stave-native-room-"))
    print(f"Evidence: {output}", flush=True)
    paths = [ROOT / p for p in ("native_v2/include/stave/piano_room.hpp", "native_v2/src/piano_room.cpp",
             "native_v2/tests/piano_room_probe.cpp", "native_v2/tests/test_piano_room.cpp",
             "tools/compare_native_room.py", "tools/compare_native_v2.py", "faust/faust_cprelude.h")]
    if args.source_stems: paths.append(args.source_stems.expanduser().resolve())
    def hashes(): return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report = {"status":"started", "reference":base.REFERENCE, "host":platform.platform(), "checks":[]}
    try:
        report["source_sha256"] = hashes()
        ref, native, guard, compiler, faust = build(output)
        report.update(compiler=compiler, faust=faust)
        report["guard_stdout"] = base.run([str(guard)], timeout=60).stdout
        create, render, oracle_hashes = oracle(ref); report["oracle_sha256"] = oracle_hashes
        lib = native_api(native)
        t = np.arange(640 * 512) / 48000
        signal = np.stack([.12 * np.sin(2*np.pi*261.6256*t) + .08*np.sin(2*np.pi*329.6276*t),
                           .11 * np.sin(2*np.pi*391.9954*t) + .05*np.sin(2*np.pi*523.2511*t)])
        signal *= np.exp(-4 * (t % .7)); signal[:, :512] = 0; signal[0, 0] = .5; signal[1, 37] = -.3
        signal[:, 384*512:] = 0
        np.save(output / "synthetic-input.npy", signal, allow_pickle=False)
        for frames in (512, 256): report["checks"].append(compare(output, lib, create, render, frames, signal, "synthetic"))
        if args.source_stems:
            stems = np.load(paths[-1], allow_pickle=False)
            if stems.ndim != 2 or stems.shape[0] != 7 or not np.isfinite(stems).all():
                raise RuntimeError("Expected finite seven-channel source graph stems")
            # Use up to512canonical blocks of real piano, then explicit silence
            # for hard-clear assertions. Cadence-specific source identity remains
            # the source runner's responsibility, not this downstream test.
            signal = np.zeros((2, 640*512)); n = min(stems.shape[1], 512*512)
            signal[:, :n] = stems[5:7, :n]
            for frames in (512, 256): report["checks"].append(compare(output, lib, create, render, frames, signal, "piano-stems"))
        if hashes() != report["source_sha256"]: raise RuntimeError("Sources changed during comparison")
        report["status"] = "passed_room_component_only"
    except Exception as error:
        report["status"] = "failed"; report["error"] = str(error)
    report["commands"] = base.COMMANDS
    report["artifact_sha256"] = {p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted(output.iterdir()) if p.is_file()}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k:v for k,v in report.items() if k in ("status", "error", "checks", "guard_stdout")}, indent=2))
    return 0 if report["status"] == "passed_room_component_only" else 1


if __name__ == "__main__": raise SystemExit(main())
