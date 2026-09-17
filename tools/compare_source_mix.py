#!/usr/bin/env python3
"""Pinned source-fader/pan/gating scalar comparison; no app/runtime imports."""
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
from types import SimpleNamespace
import numpy as np
import compare_native_v2 as base


def oracle():
    source=base.source_from_reference("stave_synth/synth_engine.py")
    ns=base.declarations(source,["blend_to_amplitude"],{"BLEND_DB_RANGE":24})
    cls=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=="SynthEngine")
    body=next(n.body for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=="_render_locked")
    def assignment(name):
        matches=[i for i,n in enumerate(body) if isinstance(n,ast.Assign) and any(ast.unparse(t)==name for t in n.targets)]
        if len(matches)!=1: raise RuntimeError(f"Pinned mix boundary mismatch: {name}")
        return matches[0]
    smooth=body[assignment("smooth"):assignment("skip_voices")+1]
    pan=[n for n in body if isinstance(n,ast.If) and ast.unparse(n.test)=="self.osc_hard_pan"]
    if len(pan)!=1: raise RuntimeError("Pinned hard-pan boundary mismatch")
    tail=ast.parse("return [self._osc1_blend_cur,self._osc2_blend_cur,osc1_b,osc2_b,o1_pan,o2_pan,render_osc1,render_osc2,render_shimmer,skip_voices,haas_active,self.shimmer_high]").body
    fn=ast.parse("def advance(self,n_samples):\n    pass\n").body[0]
    fn.body=smooth+pan+[body[assignment("haas_active")]]+tail
    ns["np"]=np
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),"pinned-source-mix","exec"),ns)
    return ns["advance"],hashlib.sha256(source.encode()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--output-dir",type=Path)
    args=parser.parse_args()
    if args.output_dir:
        output=args.output_dir.expanduser().resolve(); output.mkdir(mode=0o700,exist_ok=False)
    else: output=Path(tempfile.mkdtemp(prefix="stave-source-mix-"))
    print(f"Evidence: {output}",flush=True)
    report={"status":"started","host":platform.platform(),"reference":base.REFERENCE,"tolerance":2e-12}
    paths=[base.ROOT/p for p in ("native_v2/include/stave/source_mix.hpp","native_v2/tests/source_mix_probe.cpp",
                                "tools/compare_source_mix.py","tools/compare_native_v2.py")]
    def hashes(): return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    try:
        report["source_sha256"]=hashes(); advance,report["oracle_sha256"]=oracle()
        cxx=shutil.which("c++")
        if not cxx: raise RuntimeError("Installed C++ compiler required")
        probe=output/"source_mix_probe"
        base.run([cxx,"-std=c++17","-O2","-ffp-contract=off","-Wall","-Wextra","-Werror",
                  "-fsanitize=undefined","-fno-sanitize-recover=all","-I",str(base.ROOT/"native_v2/include"),
                  str(base.ROOT/"native_v2/tests/source_mix_probe.cpp"),"-o",str(probe)])
        report["compiler"]=base.run([cxx,"--version"]).stdout
        commands=[]; expected=[]; mute_blocks=[]
        rng=random.Random(base.SEED)
        for frames in (512,256):
            obj=SimpleNamespace(sample_rate=48000,_osc1_blend_cur=.6,_osc2_blend_cur=.4)
            commands += [f"reset {frames}","guards"]
            # Sustained step transitions, mute-to-exact-zero, threshold crossings,
            # hard-pan overriding user pans and seeded changing controller values.
            cases=[(.6,.4,0,0,0,0,0,.5),(0,0,0,0,0,0,0,.5),(0,0,.25,-.25,0,1,0,.001),
                   (0,0,.25,-.25001,0,1,1,.00101),(1,1,.9,.9,1,1,0,1),
                   (.001,.005,-1,1,0,0,0,0)]
            cases += [(rng.random(),rng.random(),rng.uniform(-1,1),rng.uniform(-1,1),
                       rng.randrange(2),rng.randrange(2),rng.randrange(2),rng.random()) for _ in range(80)]
            for case in cases:
                commands.append("config "+" ".join(map(str,case)))
                for name,value in zip(("osc1_blend","osc2_blend","osc1_pan","osc2_pan","osc_hard_pan",
                                       "shimmer_enabled","shimmer_high","shimmer_mix"),case): setattr(obj,name,value)
                for _ in range(32):
                    commands.append("advance"); result=advance(obj,frames); expected.append(result)
                    if result[9]: mute_blocks.append(len(expected)-1)
                commands.append("guards")
        text="\n".join(commands)+"\n"; (output/"fixture.txt").write_text(text)
        result=base.run([str(probe)],input_text=text)
        actual=np.array([[float(x) for x in line.split()] for line in result.stdout.splitlines()])
        expected=np.asarray(expected,dtype=np.float64)
        np.save(output/"native.npy",actual,allow_pickle=False); np.save(output/"reference.npy",expected,allow_pickle=False)
        if actual.shape!=expected.shape or not np.isfinite(actual).all(): raise RuntimeError("Invalid result")
        delta=np.abs(actual-expected)
        if np.max(delta[:,:6])>2e-12 or np.any(actual[:,6:]!=expected[:,6:]): raise RuntimeError("Source mix mismatch")
        if not mute_blocks or np.any(actual[mute_blocks,2:4]): raise RuntimeError("Muted sources not exactly zero")
        if hashes()!=report["source_sha256"]: raise RuntimeError("Sources changed during comparison")
        report.update(status="passed_scalar_component_only",compared_blocks=len(expected),max_delta=float(np.max(delta)),
                      exact_boolean_decisions=True,exact_mute_blocks=len(mute_blocks),ubsan_passed=True)
    except Exception as error: report.update(status="failed",error=str(error))
    report["commands"]=base.COMMANDS
    report["artifact_sha256"]={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir()) if p.is_file()}
    (output/"report.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k in ("status","error","compared_blocks","max_delta","exact_mute_blocks")},indent=2))
    return 0 if report["status"]=="passed_scalar_component_only" else 1


if __name__=="__main__": raise SystemExit(main())
