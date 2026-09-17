#!/usr/bin/env python3
"""Offline pad/filter/delay/reverb mix vs pinned routing and actual Faust.

Explicit exclusions: global modulation, sympathetic/sample beds, wet-output
filter, piano/organ dry mix and master processing. No devices or app startup.
"""
from __future__ import annotations
import argparse
import ast
import ctypes as ct
import hashlib
import json
import logging
from pathlib import Path
import platform
import shutil
import tempfile
from types import SimpleNamespace
import numpy as np
import compare_native_v2 as base
import compare_native_buses as buses
import compare_shared_effects as effects

PD=effects.PD

def mix_oracle(with_motion=False):
    source=base.source_from_reference("stave_synth/synth_engine.py")
    cls=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=="SynthEngine")
    body=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=="_render_locked").body
    def find(test):
        hits=[i for i,n in enumerate(body) if isinstance(n,ast.If) and ast.unparse(n.test)==test]
        if len(hits)!=1: raise RuntimeError("Pinned routing boundary changed: "+test)
        return hits[0]
    start=find("external_reverb_send is not None and external_reverb_send.shape[1] >= n_samples")
    end=find("bypass_l is not None")
    split=[n for n in body if isinstance(n,ast.If) and ast.unparse(n.test)=="separate_fx" and
           any(isinstance(x,ast.Assign) and any(ast.unparse(t)=="eff_dry_gain" for t in x.targets) for x in n.body)]
    if len(split)!=1: raise RuntimeError("Pinned split boundary changed")
    fn=ast.parse("def render(self,pad,state,external_reverb_send):\n    pass\n").body[0]
    intro=ast.parse("""
n_samples=pad.shape[1]
output_l=pad[0].copy()
output_r=pad[1].copy()
reverb_in_l=pad[2].copy()
reverb_in_r=pad[3].copy()
bypass_ratio=state[7]
bypass_l=pad[4] if bypass_ratio>1e-6 else None
bypass_r=pad[5] if bypass_ratio>1e-6 else None
alpha_s=1.0-np.exp(-n_samples/(0.08*self.sample_rate))
bus_amul_l=bus_amul_r=None
separate_fx=True
_pre_fx_dry_l=self._pre_fx_snapshot[0]
_pre_fx_dry_r=self._pre_fx_snapshot[1]
pad_l=pad_r=np.zeros(n_samples)
pad_vol=0.0
""").body
    if with_motion: intro+=ast.parse("bus_amul_l=self._bus_amul_l\nbus_amul_r=self._bus_amul_r").body
    fn.body=intro+body[start:end+1]+split
    env={"np":np,"logger":logging.getLogger("offline-mix")}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),"pinned-ambience-mix","exec"),env)
    return env["render"]

def api(path):
    lib=ct.CDLL(str(path))
    signatures={"ambience_create":([ct.c_uint],ct.c_void_p),"ambience_delete":([ct.c_void_p],None),
        "ambience_configure":([ct.c_void_p,PD,PD,ct.c_double,ct.c_double],ct.c_int),
        "ambience_command":([ct.c_void_p,ct.c_uint,ct.c_double],ct.c_int),
        "ambience_process":([ct.c_void_p,PD,ct.c_uint,ct.c_uint,PD,PD],ct.c_int),
        "ambience_channel":([ct.c_void_p,ct.c_uint],PD),"ambience_wet":([ct.c_void_p],ct.c_double),
        "ambience_stop":([ct.c_void_p],None)}
    for name,(args,res) in signatures.items(): getattr(lib,name).argtypes=args; getattr(lib,name).restype=res
    return lib

def compare(output,lib,refs,classes,filters,size,signals=None):
    pcreate,prender=buses.pad_oracle(refs[0],fallback_types=filters,with_delay=True)[:2]
    pad=pcreate(); pad._synth_diagnostics=None; pad._dry_wet_cur=.75; pad.reverb_filter_enabled=False
    pad.reverb=classes["FaustReverb"]()
    pad._stereo_out=np.zeros((2,size)); pad._dry_bus_scratch=np.zeros((2,size)); pad._fx_bus_scratch=np.zeros((2,size))
    delay=classes["DelayOwner"](); delay.sample_rate=48000; delay._faust_ping_pong=classes["FaustPingPong"]()
    pad._process_ping_pong=delay._process_ping_pong
    render_mix=mix_oracle(); handle=lib.ambience_create(size)
    if not handle: raise RuntimeError("Native ambience construction failed")
    total=4096*512 if signals is None else signals.shape[1]
    bus=buses.DEFAULT.copy(); d=effects.DEFAULT.copy(); changes,flag_changes,_=buses.fixture()
    dchanges,events=effects.fixtures(); flags=3; wet=.75; gain=1
    actual=np.empty((6,total)); expected=np.empty_like(actual); state_delta=0; muted=0
    ptr=lambda x:x.ctypes.data_as(PD)
    try:
        for frame in range(0,total,size):
            tick=frame//512
            if frame%512==0:
                for i,v in changes.get(tick,{}).items(): bus[i]=v
                if tick in flag_changes: flags=flag_changes[tick]
                for i,v in dchanges.get(tick,{}).items(): d[i]=v
                for action,v in events.get(tick,[]):
                    if action==9: continue # Terminal STOP is separate; no new soft panic here.
                    if not lib.ambience_command(handle,action,v): raise RuntimeError("Ambience command rejected")
                    if action<7: getattr(pad.reverb,effects.SETTERS[action])(v)
                    elif action==7: pad.reverb.set_type(effects.TYPES[v])
                    elif action==8: pad.reverb.set_freeze(bool(v))
                if tick in (75,175,275,375,500,700,950): wet=(tick%3)/2; gain=1+(tick%4)/2
            paused=270<=tick<280 or 1500<=tick<1530
            current_bus=bus.copy(); current_flags=flags
            if paused: current_bus[15]=0; current_flags=10; muted+=1
            for name,v in zip(buses.CONFIG_NAMES,current_bus): setattr(pad,name,float(v))
            pad.filter_slope=24 if current_bus[4] else 12
            for i,name in enumerate(effects.DELAY_NAMES): setattr(delay,name,effects.DIVISIONS[int(d[i])] if i in (3,4) else d[i])
            pad.reverb.dry_wet=wet; pad.reverb.wet_gain=gain
            if not lib.ambience_configure(handle,ptr(current_bus),ptr(d),wet,gain): raise RuntimeError("Ambience configuration rejected")
            t=(frame+np.arange(size))/48000
            if signals is None:
                source=np.array([.2*np.sin(2*np.pi*hz*t) for hz in (220,330,261.63,392,440)])
                if tick%300>100: source.fill(0)
                ext=np.array([.07*np.sin(2*np.pi*hz*t) for hz in (164.81,196)])
            else:
                source=np.ascontiguousarray(signals[:5,frame:frame+size])
                ext=np.ascontiguousarray(signals[5:7,frame:frame+size])*.3
            if paused: source=np.zeros_like(source)
            extd=ext if tick%300<180 else None; extr=ext*.4 if tick%250<200 else None
            delay._external_delay_send=extd
            reference_pad,state=prender(pad,source,current_flags,not paused)
            dry,fx=render_mix(pad,reference_pad,state,extr)
            expected[:,frame:frame+size]=np.vstack((pad._stereo_out*.85,dry,fx))
            if not lib.ambience_process(handle,ptr(source),size,current_flags,ptr(extd) if extd is not None else None,
                                        ptr(extr) if extr is not None else None): raise RuntimeError("Ambience render failed")
            for c in range(6): actual[c,frame:frame+size]=np.ctypeslib.as_array(lib.ambience_channel(handle,c),shape=(size,))
            state_delta=max(state_delta,abs(lib.ambience_wet(handle)-pad._dry_wet_cur))
        if not np.isfinite(actual).all() or not np.isfinite(expected).all(): raise RuntimeError("Nonfinite ambience output")
        peak_delta=np.max(np.abs(actual-expected),axis=1)
        label="synthetic" if signals is None else "sources"
        np.save(output/f"ambience-{label}-native-{size}.npy",actual,allow_pickle=False)
        np.save(output/f"ambience-{label}-reference-{size}.npy",expected,allow_pickle=False)
        if np.max(peak_delta)>effects.TOLERANCE or state_delta>1e-12: raise RuntimeError(f"Ambience mismatch: {peak_delta}; wet {state_delta}")
        lib.ambience_stop(handle)
        if lib.ambience_process(handle,ptr(source),size,flags,None,None): raise RuntimeError("Ambience resumed after STOP")
        if any(np.any(np.ctypeslib.as_array(lib.ambience_channel(handle,c),shape=(size,))) for c in range(6)): raise RuntimeError("Ambience STOP not silent")
        return {"fixture":label,"frames":total,"block_frames":size,"peak_difference":peak_delta.tolist(),"wet_state_difference":state_delta,
                "peak":np.max(np.abs(actual),axis=1).tolist(),"terminal_stop":True,"muted_source_blocks":muted}
    finally: lib.ambience_delete(handle)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir",type=Path); parser.add_argument("--source-dir",type=Path)
    args=parser.parse_args()
    if args.output_dir: output=args.output_dir.expanduser().resolve(); output.mkdir(mode=0o700,exist_ok=False)
    else: output=Path(tempfile.mkdtemp(prefix="stave-pad-ambience-"))
    print(f"Evidence: {output}",flush=True)
    paths=[*sorted((base.ROOT/"native_v2").rglob("*.hpp")),*sorted((base.ROOT/"native_v2").rglob("*.cpp")),
           Path(__file__),Path(effects.__file__),Path(buses.__file__),Path(buses.room.__file__),Path(base.__file__),base.ROOT/"faust/faust_cprelude.h"]
    if args.source_dir: paths.extend(args.source_dir/f"sources-native-{n}.npy" for n in (512,256))
    hashes=lambda:{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report={"status":"started","host":platform.platform(),"reference":base.REFERENCE,"runs":[],"tolerance":effects.TOLERANCE}
    try:
        report["source_sha256"]=hashes()
        edir=output/"effects"; edir.mkdir(); libraries,_,guard,compiler,faust=effects.build(edir)
        report.update(compiler=compiler,faust=faust); report["effect_guards"]=base.run([str(guard)],timeout=60).stdout
        classes,report["oracle_sha256"]=effects.oracles(libraries)
        bdir=output/"buses"; bdir.mkdir(); refs,_,bguard,_,_=buses.build(bdir)
        report["bus_guards"]=base.run([str(bguard)],timeout=60).stdout
        cxx=shutil.which("c++"); include=Path(shutil.which("faust")).resolve().parent.parent/"include"
        candidate,aguard=output/"pad_ambience.so",output/"test_pad_ambience"
        for dest,test,extra in ((candidate,"pad_ambience_probe.cpp",["-shared"]),
                               (aguard,"test_pad_ambience.cpp",["-fsanitize=undefined","-fno-sanitize-recover=all"])):
            base.run([cxx,"-std=c++17","-O2","-ffp-contract=off","-fPIC",*extra,"-Wall","-Wextra","-Werror",
                  "-I",str(include),"-I",str(base.ROOT/"native_v2/include"),
                  str(base.ROOT/"native_v2/src/shared_effects.cpp"),str(base.ROOT/"native_v2/src/pad_bus.cpp"),
                  str(base.ROOT/"native_v2/src/piano_room.cpp"),
                  str(base.ROOT/"native_v2/tests"/test),
                  *[str(edir/f"{name}.o") for name,_,_ in effects.MODULES],str(bdir/"pad_bus.o"),str(bdir/"piano_room.o"),"-o",str(dest)])
        report["ambience_guards"]=base.run([str(aguard)],timeout=60).stdout
        print(report["ambience_guards"],flush=True)
        lib=api(candidate)
        create_piano,report["filter_oracle_sha256"]=base.piano_oracle(output,shutil.which("cc"))
        filters=create_piano()._faust_chain
        filter_types=(type(filters.lp[0][0]),type(filters.hp[0][0]))
        for size in (512,256):
            inputs=[None]
            if args.source_dir:
                data=np.load(args.source_dir/f"sources-native-{size}.npy",allow_pickle=False)
                if data.ndim!=2 or data.shape[0]!=7 or data.shape[1]%512 or not np.isfinite(data).all(): raise RuntimeError("Expected finite seven-channel source evidence")
                inputs.append(data)
            for signal in inputs:
                result=compare(output,lib,refs,classes,filter_types,size,signal); report["runs"].append(result); print(f"PASS: {result}",flush=True)
        (output/"fixture.json").write_text(json.dumps({"bus":buses.fixture()[:2],"effects":effects.fixtures(),
            "muted_intervals":[[270,280],[1500,1530]],"wet_change_ticks":[75,175,275,375,500,700,950]},indent=2)+"\n")
        if hashes()!=report["source_sha256"]: raise RuntimeError("Source changed during comparison")
        report["status"]="passed_pad_ambience_only"
    except Exception as error: report.update(status="failed",error=str(error))
    report["commands"]=base.COMMANDS
    report["artifact_sha256"]={str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.rglob("*")) if p.is_file()}
    (output/"report.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k in ("status","error","runs")},indent=2))
    return 0 if report["status"].startswith("passed") else 1

if __name__=="__main__": raise SystemExit(main())
