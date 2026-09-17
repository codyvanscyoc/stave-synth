#!/usr/bin/env python3
"""Offline raw-key split oracle. No application startup, devices or network."""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
from pathlib import Path
import platform
import random
import shutil
import tempfile
import numpy as np
import compare_native_v2 as base


def oracle():
    source=base.source_from_reference("stave_synth/synth_engine.py")
    cls=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=="SynthEngine")
    fn=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=="split_weight")
    fn.decorator_list=[]
    ns={}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),"pinned-split-weight","exec"),ns)
    return ns["split_weight"],hashlib.sha256(source.encode()).hexdigest()


def configuration(tick):
    # Four independent ranges, including partial piano velocity, silent keys,
    # split edits under sustain/sostenuto, disabled ranges and transpose edits.
    cases=((0,0,127,0,0,127,0,0,127,0,0,127,0),
           (1,36,53,8,55,76,12,0,48,6,52,127,8),
           (1,64,84,3,48,60,4,67,90,6,36,60,12),
           (1,0,0,0,127,127,0,0,0,0,127,127,0),
           (1,48,64,24,52,67,4,60,72,12,48,72,6),
           (0,127,0,24,127,0,0,127,0,24,127,0,0))
    boundaries=(0,20,36,100,124,160)
    index=max(i for i,start in enumerate(boundaries) if tick>=start)
    return cases[index]


def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--output-dir",type=Path)
    args=parser.parse_args()
    if args.output_dir: output=args.output_dir.expanduser().resolve(); output.mkdir(mode=0o700,exist_ok=False)
    else: output=Path(tempfile.mkdtemp(prefix="stave-splits-"))
    print(f"Evidence: {output}",flush=True)
    paths=[Path(__file__),Path(base.__file__),*[base.ROOT/p for p in (
        "native_v2/include/stave/stage_splits.hpp","native_v2/include/stave/stage_notes.hpp","native_v2/tests/stage_splits_probe.cpp")]]
    hashes=lambda:{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report={"status":"started","host":platform.platform(),"reference":base.REFERENCE}
    try:
        report["source_sha256"]=hashes(); weight,report["oracle_sha256"]=oracle()
        cxx=shutil.which("c++")
        if not cxx: raise RuntimeError("Existing C++ compiler required")
        probe=output/"stage_splits_probe"
        base.run([cxx,"-std=c++17","-O2","-ffp-contract=off","-Wall","-Wextra","-Werror",
                  "-fsanitize=undefined","-fno-sanitize-recover=all","-I",str(base.ROOT/"native_v2/include"),
                  str(base.ROOT/"native_v2/tests/stage_splits_probe.cpp"),"-o",str(probe)])
        rng=random.Random(base.SEED); configs=[]
        for enabled in (0,1):
            for low,high in ((0,127),(0,0),(127,127),(60,60),(48,72),(72,48),(65,60)):
                for fade in range(25): configs.append([enabled,*([low,high,fade]*4)])
            for _ in range(250):
                configs.append([enabled,*[v for _ in range(4) for v in (rng.randrange(128),rng.randrange(128),rng.randrange(25))]])
        configs += [configuration(t) for t in (0,20,36,100,124,160)]
        fixture="\n".join(" ".join(map(str,c)) for c in configs)+"\n"
        (output/"fixture.txt").write_text(fixture)
        result=base.run([str(probe)],input_text=fixture)
        actual=np.array([[float(v) for v in line.split()] for line in result.stdout.splitlines()])
        expected=np.array([[weight(note,*c[1+3*z:4+3*z]) if c[0] else 1. for z in range(4)] for c in configs for note in range(128)])
        if actual.shape!=expected.shape or not np.array_equal(actual,expected): raise RuntimeError("Split weights not exactly equal")
        np.save(output/"native.npy",actual,allow_pickle=False); np.save(output/"reference.npy",expected,allow_pickle=False)
        if hashes()!=report["source_sha256"]: raise RuntimeError("Sources changed during comparison")
        report.update(status="passed_split_component_only",configurations=len(configs),raw_key_cases=len(expected),
                      layer_weights=int(expected.size),exact=True,ubsan_guards=True,scoped_cpp_allocation_guard=True)
    except Exception as error: report.update(status="failed",error=str(error))
    report["commands"]=base.COMMANDS
    report["artifact_sha256"]={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()}
    (output/"report.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k not in ("commands","source_sha256","artifact_sha256")},indent=2))
    return 0 if report["status"].startswith("passed") else 1


if __name__=="__main__": raise SystemExit(main())
