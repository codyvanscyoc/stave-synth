#!/usr/bin/env python3
"""Build the stage JACK host from source without connecting to any devices."""
import argparse
import os
from pathlib import Path
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


def build(output):
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
        os.replace(work / "audition-jack", output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    build(parser.parse_args().output.absolute())
