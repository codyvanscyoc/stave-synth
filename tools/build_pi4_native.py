#!/usr/bin/env python3
"""Build the stage JACK host from source without connecting to any devices."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    ("osc_bank", "StaveOscBank", False),
    ("piano_chain", "StavePianoChain", True),
    ("pad_bus", "StavePadBus", True),
    ("piano_room", "StavePianoRoom", False),
    ("ping_pong", "StavePingPong", False),
    ("reverb", "StaveReverb", False),
    ("plate", "StavePlate", False),
    ("drone", "StaveDrone", False),
    ("master_fx", "StaveMasterFX", False),
    ("bus_comp", "StaveBusComp", False),
)
SOURCES = ("stage_sources", "piano_chain", "pad_bus", "piano_room", "shared_effects", "stage_master")


def run(*command):
    subprocess.run(command, check=True)


def source_hashes():
    inputs = [Path(__file__).resolve(), ROOT / "faust/faust_cprelude.h"]
    inputs.extend(ROOT / "faust" / (name + ".dsp") for name, _, _ in MODULES)
    for directory in ("native_v2/include", "native_v2/src"):
        inputs.extend(path for path in (ROOT / directory).rglob("*") if path.suffix in {".hpp", ".cpp"})
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(inputs)}


def build(output):
    sources = source_hashes()
    faust = shutil.which("faust")
    if not faust:
        raise SystemExit("Install Faust to build the native engine.")
    includes = Path(faust).resolve().parents[1] / "include"
    flags = ["-O2", "-ffp-contract=off", "-I", str(includes)]
    deps = shlex.split(subprocess.check_output(
        ["pkg-config", "--cflags", "--libs", "fluidsynth", "jack"], text=True))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        work = Path(tmp)
        objects = []
        for name, classname, double in MODULES:
            print("Building", name, flush=True)
            source = ROOT / "faust" / (name + ".dsp")
            if name == "osc_bank":
                text = source.read_text()
                if text.splitlines().count("NVOICES = 24;") != 1:
                    raise ValueError("Unexpected oscillator voice declaration")
                source = work / "osc_bank.dsp"
                source.write_text(text.replace("NVOICES = 24;", "NVOICES = 12;", 1))
            generated = work / (name + ".c")
            obj = work / (name + ".o")
            run(faust, *(["-double"] if double else []), "-lang", "c", "-cn", classname,
                "-o", generated, source)
            run("cc", "-std=c11", *flags, *(["-DFAUSTFLOAT=double"] if double else []),
                "-include", ROOT / "faust/faust_cprelude.h", "-c", generated, "-o", obj)
            objects.append(obj)
        run("c++", "-std=c++17", *flags, "-pthread", "-Wall", "-Wextra", "-Werror",
            "-I", ROOT / "native_v2/include",
            *(ROOT / "native_v2/src" / (name + ".cpp") for name in SOURCES),
            ROOT / "native_v2/src/audition_jack.cpp", *objects, *deps, "-o", work / "audition-jack")
        if sources != source_hashes():
            raise RuntimeError("Native build inputs changed during compilation")
        engine = work / "audition-jack"
        versions = {name: subprocess.check_output(cmd, text=True).strip().splitlines()[0]
                    for name, cmd in (("cc", ["cc", "--version"]), ("c++", ["c++", "--version"]),
                                      ("faust", [faust, "--version"]),
                                      ("fluidsynth", ["pkg-config", "--modversion", "fluidsynth"]),
                                      ("jack", ["pkg-config", "--modversion", "jack"]))}
        receipt = {"format": 1, "machine": platform.machine(), "toolchain": versions,
                   "binary_sha256": hashlib.sha256(engine.read_bytes()).hexdigest(),
                   "source_sha256": sources}
        (work / "build.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        os.replace(engine, output)
        os.replace(work / "build.json", output.with_name(output.name + ".build.json"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    build(parser.parse_args().output.absolute())
