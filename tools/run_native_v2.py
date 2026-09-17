#!/usr/bin/env python3
"""Build/test the opt-in native prototype OFFLINE, never the installed synth.

No package installation, audio/MIDI driver, network call or production state.
Generated sources, binaries, WAVs and report live in a newly created directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
COMMANDS: list[dict] = []


def run(command: list[str], *, timeout: int = 180) -> str:
    print("+ " + shlex.join(map(str, command)), flush=True)
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True,
                            timeout=timeout)
    COMMANDS.append({"command": list(map(str, command)), "returncode": result.returncode,
                     "stdout": result.stdout, "stderr": result.stderr})
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {command[0]}")
    return result.stdout


def source_hashes() -> dict[str, str]:
    paths = sorted((ROOT / "native_v2").rglob("*.hpp")) + sorted((ROOT / "native_v2").rglob("*.cpp"))
    paths += [ROOT / "faust/osc_bank.dsp", ROOT / "faust/piano_chain.dsp", ROOT / "faust/faust_cprelude.h", Path(__file__)]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def executable(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"Missing {name}; no dependency will be installed automatically")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-sound", action="store_true",
                        help="Build existing Faust DSP and run offline oscillator tests/WAV")
    parser.add_argument("--soundfont", type=Path,
                        help="Explicit local SF2 enables the real native piano; never downloaded")
    parser.add_argument("--fluidsynth-prefix", type=Path,
                        help="Installed prefix with include/fluidsynth.h and lib/")
    parser.add_argument("--faust-prefix", type=Path,
                        help="Installed prefix containing include/faust/gui/CInterface.h")
    parser.add_argument("--output-dir", type=Path,
                        help="New directory only; existing paths are refused")
    sanitizers = parser.add_mutually_exclusive_group()
    sanitizers.add_argument("--sanitize", action="store_true",
                        help="Address/undefined sanitizers for core tests (not timing evidence)")
    sanitizers.add_argument("--ubsan", action="store_true",
                        help="Undefined-behavior sanitizer for core/component tests only")
    args = parser.parse_args()
    if args.soundfont and not args.with_sound:
        parser.error("--soundfont requires --with-sound")
    font = args.soundfont.resolve() if args.soundfont else None
    if font and (not font.is_file() or font.suffix.lower() != ".sf2"):
        parser.error("--soundfont must name an existing .sf2 file")
    if args.output_dir:
        output = args.output_dir.absolute()
        output.mkdir(parents=False, exist_ok=False)
    else:
        output = Path(tempfile.mkdtemp(prefix="stave-native-v2-"))
    print(f"Offline artifacts: {output}", flush=True)
    report: dict = {"status": "running", "prototype": True,
                    "live_driver": False, "pi4_qualified": False,
                    "v1_sound_parity": False, "piano_enabled": bool(font),
                    "sanitized_core": args.sanitize, "ubsan_core": args.ubsan, "checks": {},
                    "platform": platform.platform(), "commands": COMMANDS,
                    "source_sha256": source_hashes()}
    start = time.monotonic()
    try:
        cxx = executable("c++")
        report["compiler"] = run([cxx, "--version"])
        flags = ["-std=c++17", "-O2", "-ffp-contract=off", "-Wall", "-Wextra", "-Werror", "-pthread",
                 "-I", str(ROOT / "native_v2/include")]
        core = output / "test_engine"
        sanitizer = (["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
                     if args.sanitize else ["-fsanitize=undefined", "-fno-sanitize-recover=all"] if args.ubsan else [])
        report["checks"]["core_compile"] = run([
            cxx, *flags, *sanitizer, str(ROOT / "native_v2/tests/test_engine.cpp"),
            "-o", str(core)])
        report["checks"]["core"] = run([str(core)], timeout=30)
        components = output / "test_components"
        run([cxx, *flags, *sanitizer, str(ROOT / "native_v2/tests/test_components.cpp"), "-o", str(components)])
        report["checks"]["components"] = run([str(components)], timeout=30)
        if args.with_sound:
            faust = executable("faust")
            report["faust"] = run([faust, "--version"])
            cc = executable("cc")
            prefix = (args.faust_prefix or Path(faust).resolve().parent.parent)
            if not (prefix / "include/faust/gui/CInterface.h").is_file():
                raise RuntimeError("Faust C headers missing; supply --faust-prefix")
            source = ROOT / "faust/osc_bank.dsp"
            dsp = source.read_text()
            original = "NVOICES = 24;"
            if dsp.splitlines().count(original) != 1:
                raise RuntimeError("Refusing unverified oscillator slot transformation")
            lite = output / "osc_bank_lite.dsp"
            # Same mechanical12-slot transformation as the preserved Pi4 build.
            lite.write_text(dsp.replace(original, "NVOICES = 12;", 1))
            generated = output / "osc_bank.c"
            obj = output / "osc_bank.o"
            run([faust, "-lang", "c", "-cn", "StaveOscBank", "-o", str(generated), str(lite)])
            # Deliberately no -ffast-math: do not defeat finite-value guards.
            run([cc, "-std=c11", "-O2", "-I", str(prefix / "include"),
                 "-include", str(ROOT / "faust/faust_cprelude.h"),
                 "-c", str(generated), "-o", str(obj)])
            includes = ["-I", str(prefix / "include")]
            link: list[str] = []
            if font:
                includes += ["-DSTAVE_V2_WITH_FLUIDSYNTH=1"]
                fluid_prefix = args.fluidsynth_prefix
                if fluid_prefix is None and Path("/opt/homebrew/opt/fluid-synth/include/fluidsynth.h").is_file():
                    fluid_prefix = Path("/opt/homebrew/opt/fluid-synth")
                if fluid_prefix:
                    if not (fluid_prefix / "include/fluidsynth.h").is_file():
                        raise RuntimeError("FluidSynth headers missing at supplied prefix")
                    includes += ["-I", str(fluid_prefix / "include")]
                    link = ["-L", str(fluid_prefix / "lib"),
                            "-Wl,-rpath," + str(fluid_prefix / "lib"), "-lfluidsynth"]
                else:
                    pkg = executable("pkg-config")
                    includes += shlex.split(run([pkg, "--cflags", "fluidsynth"]))
                    link = shlex.split(run([pkg, "--libs", "fluidsynth"]))
            backend = str(ROOT / "native_v2/src/sound_backend.cpp")
            sound_test = output / "test_sound_backend"
            run([cxx, *flags, *includes, backend,
                 str(ROOT / "native_v2/tests/test_sound_backend.cpp"), str(obj),
                 *link, "-o", str(sound_test)])
            report["checks"]["sound"] = run([str(sound_test), *([str(font)] if font else [])], timeout=60)
            piano_c, piano_obj = output / "piano_chain.c", output / "piano_chain.o"
            run([faust, "-double", "-lang", "c", "-cn", "StavePianoChain", "-o", str(piano_c),
                 str(ROOT / "faust/piano_chain.dsp")])
            run([cc, "-std=c11", "-O2", "-ffp-contract=off", "-DFAUSTFLOAT=double", "-I", str(prefix / "include"),
                 "-include", str(ROOT / "faust/faust_cprelude.h"), "-c", str(piano_c), "-o", str(piano_obj)])
            piano_test = output / "test_piano_chain"
            run([cxx, *flags, "-I", str(prefix / "include"), str(ROOT / "native_v2/src/piano_chain.cpp"),
                 str(ROOT / "native_v2/tests/test_piano_chain.cpp"), str(piano_obj), "-o", str(piano_test)])
            report["checks"]["piano_chain_guards"] = run([str(piano_test)], timeout=30)
            demo = output / "render_demo"
            run([cxx, *flags, *includes, backend,
                 str(ROOT / "native_v2/src/render_demo.cpp"), str(obj),
                 *link, "-o", str(demo)])
            for block in (512, 256):
                wav = output / f"prototype-{block}.wav"
                report["checks"][f"render_{block}"] = run([
                    str(demo), str(wav), str(block), *([str(font)] if font else [])], timeout=60)
            # Exercise the demo's direct-invocation no-overwrite contract too.
            existing = output / "prototype-512.wav"
            before = hashlib.sha256(existing.read_bytes()).hexdigest()
            refused = subprocess.run([str(demo), str(existing), "512"], cwd=ROOT,
                                     capture_output=True, text=True, timeout=60)
            if refused.returncode != 1 or hashlib.sha256(existing.read_bytes()).hexdigest() != before:
                raise RuntimeError("Demo failed to preserve existing WAV")
            link_path, target = output / "dangling.wav", output / "must-not-create.wav"
            link_path.symlink_to(target)
            refused_link = subprocess.run([str(demo), str(link_path), "512"], cwd=ROOT,
                                          capture_output=True, text=True, timeout=60)
            if refused_link.returncode != 1 or target.exists() or not link_path.is_symlink():
                raise RuntimeError("Demo failed to refuse dangling symlink")
            report["checks"]["exclusive_wav"] = "PASS: existing file unchanged; dangling symlink refused"
        if source_hashes() != report["source_sha256"]:
            raise RuntimeError("Source changed during build; rerun stable inputs before using this evidence")
        if font:
            report["soundfont_sha256"] = hashlib.sha256(font.read_bytes()).hexdigest()
        report["status"] = "passed_offline_only"
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        report["status"] = "failed"
        report["error"] = str(error)
        print(str(error), file=sys.stderr)
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - start, 3)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"Report: {output / 'report.json'}", flush=True)
    return 0 if report["status"] == "passed_offline_only" else 1


if __name__ == "__main__":
    raise SystemExit(main())
