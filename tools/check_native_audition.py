#!/usr/bin/env python3
"""Device-free audition guards using verified objects from a saved comparison.

No driver library is linked: fake JACK calls use explicit real JACK headers.
No service, Pi, network, browser, runtime state or physical devices are opened.
The complete Pi builder is benchmark_native_instrument.py --build-audition.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OBJECTS = ("sources/osc_bank.o", "sources/piano_chain.o", "buses/pad_bus.o", "buses/piano_room.o",
           "effects/ping_pong.o", "effects/reverb.o", "effects/plate.o", "effects/drone.o",
           "master/master_fx.o", "master/bus_comp.o")
SOURCES = ("stage_sources", "piano_chain", "pad_bus", "piano_room", "shared_effects", "stage_master")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--soundfont", type=Path, required=True)
    parser.add_argument("--faust-prefix", type=Path, required=True)
    parser.add_argument("--fluidsynth-prefix", type=Path, required=True)
    parser.add_argument("--jack-include", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    for path in (args.soundfont, args.reference_dir / "report.json", args.faust_prefix / "include/faust/gui/CInterface.h",
                 args.fluidsynth_prefix / "include/fluidsynth.h", args.jack_include / "jack/jack.h"):
        if not path.is_file(): parser.error(f"Explicit existing dependency required: {path}")
    out = args.output_dir.resolve()
    out.mkdir(mode=0o700, exist_ok=False)
    report = {"status": "started", "host": platform.platform(), "commands": [], "scope": "offline/fake JACK, no live qualification"}
    def run(cmd):
        result = subprocess.run([str(x) for x in cmd], text=True, capture_output=True, timeout=180)
        report["commands"].append(dict(argv=[str(x) for x in cmd], exit=result.returncode, stdout=result.stdout, stderr=result.stderr))
        if result.returncode: raise RuntimeError(result.stderr or f"Command failed: {cmd[0]}")
        return result.stdout
    try:
        old = json.loads((args.reference_dir / "report.json").read_text())
        report["reference_report_sha256"] = digest(args.reference_dir / "report.json")
        report["reference_sound_status"] = old["status"]
        cxx = shutil.which("c++")
        if not cxx: raise RuntimeError("Existing C++ compiler required")
        report["compiler"] = run([cxx, "--version"])
        if report["compiler"] != old["compiler"]: raise RuntimeError("Object reuse compiler mismatch")
        for rel in OBJECTS:
            p = args.reference_dir / rel
            if p.is_symlink() or digest(p) != old["artifact_sha256"][rel]: raise RuntimeError("Object hash mismatch: " + rel)
        report["reused_object_sha256"] = {rel: digest(args.reference_dir / rel) for rel in OBJECTS}
        # Reuse only the same generated algorithms. The C++ owner itself is
        # rebuilt from current source and retains a separate source manifest.
        rel = "faust/faust_cprelude.h"
        matches = [v for k, v in old["source_sha256"].items() if k.endswith("/" + rel)]
        if len(matches) != 1 or matches[0] != digest(ROOT / rel): raise RuntimeError("DSP prelude changed")
        for obj in OBJECTS:
            rel = str(Path(obj).with_suffix(".dsp"))
            saved = args.reference_dir / rel
            current = (ROOT / "faust" / saved.name).read_text()
            if saved.stem == "osc_bank":
                if current.splitlines().count("NVOICES = 24;") != 1: raise RuntimeError("Unexpected oscillator specialization")
                current = current.replace("NVOICES = 24;", "NVOICES = 12;", 1)
            if digest(saved) != old["artifact_sha256"][rel] or saved.read_text() != current:
                raise RuntimeError("DSP source changed: " + rel)
        paths = [*sorted((ROOT / "native_v2").rglob("*.hpp")), *sorted((ROOT / "native_v2").rglob("*.cpp")), Path(__file__)]
        hashes = lambda: {str(p.relative_to(ROOT)): digest(p) for p in paths}
        report["source_sha256"] = hashes()
        report["soundfont_sha256"] = digest(args.soundfont)
        report["jack_header_sha256"] = {p.name: digest(p) for p in (args.jack_include / "jack").glob("*.h")}
        flags = ["-std=c++17", "-O2", "-ffp-contract=off", "-Wall", "-Wextra", "-Werror", "-pthread",
                 "-fsanitize=undefined", "-fno-sanitize-recover=all", "-I", ROOT / "native_v2/include",
                 "-I", args.faust_prefix / "include", "-I", args.fluidsynth_prefix / "include", "-I", args.jack_include]
        for name in ("test_piano_chain", "test_audition_session", "test_audition_jack"):
            binary = out / name
            run([cxx, *flags, *[ROOT / f"native_v2/src/{x}.cpp" for x in SOURCES], ROOT / f"native_v2/tests/{name}.cpp",
                 *[args.reference_dir / p for p in OBJECTS], "-L", args.fluidsynth_prefix / "lib",
                 "-Wl,-rpath," + str(args.fluidsynth_prefix / "lib"), "-lfluidsynth", "-o", binary])
            print(run([binary, args.soundfont]), flush=True)
        if hashes() != report["source_sha256"]: raise RuntimeError("Source changed during checks")
        report["status"] = "passed_offline_audition_guards_not_live_qualification"
    except Exception as error:
        report.update(status="failed", error=str(error))
    report["artifact_sha256"] = {p.name: digest(p) for p in out.iterdir() if p.is_file()}
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(report["status"], report.get("error", ""), flush=True)
    return 0 if report["status"].startswith("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
