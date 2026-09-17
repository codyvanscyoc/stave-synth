#!/usr/bin/env python3
"""Offline master path and bounded lookahead limiter vs pinned v1.2.

No application imports, device access, network or production state. Reference
mode preserves v1; safe mode has an explicit independent limiter-oracle change
to cap release at target and final roundoff at the declared ceiling.
"""
from __future__ import annotations
import argparse
import ast
import copy
import ctypes as ct
import hashlib
import json
from pathlib import Path
import platform
import importlib.metadata
import shutil
import tempfile
from types import SimpleNamespace
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy.signal import lfilter
from cffi import FFI
import compare_native_v2 as base

PD=ct.POINTER(ct.c_double)
TOLERANCE=1e-6
DEFAULT=np.array([200,0,1.5,1000,0,1.5,5000,0,1.5,0,80,12,0,1.5,0,0,0,1,1,0,-10,4,3,300,2,0,1,100],dtype=np.float64)
SOURCES=("self","piano","lfo","bpm")

def build(output):
    cc,cxx,faust=(shutil.which(n) for n in ("cc","c++","faust"))
    if not all((cc,cxx,faust)): raise RuntimeError("Existing C/C++/Faust required")
    include=Path(faust).resolve().parent.parent/"include"; libraries={}; objects=[]
    for name,cls in (("master_fx","StaveMasterFX"),("bus_comp","StaveBusComp")):
        dsp,c,obj=(output/f"{name}.{s}" for s in ("dsp","c","o"))
        dsp.write_text(base.source_from_reference(f"faust/{name}.dsp"))
        base.run([faust,"-lang","c","-cn",cls,"-o",str(c),str(dsp)])
        base.run([cc,"-std=c11","-O2","-ffp-contract=off","-fPIC","-I",str(include),"-include",str(base.ROOT/"faust/faust_cprelude.h"),"-c",str(c),"-o",str(obj)])
        so=output/f"reference_{name}.so"; base.run([cc,"-shared",str(obj),"-o",str(so)])
        libraries[name]=so; objects.append(str(obj))
    candidate,guard=output/"stage_master.so",output/"test_stage_master"
    for dest,test,extra in ((candidate,"master_probe.cpp",["-shared"]),(guard,"test_stage_master.cpp",["-fsanitize=undefined","-fno-sanitize-recover=all"])):
        base.run([cxx,"-std=c++17","-O2","-ffp-contract=off","-fPIC","-Wall","-Wextra","-Werror",*extra,
                  "-I",str(include),"-I",str(base.ROOT/"native_v2/include"),str(base.ROOT/"native_v2/src/stage_master.cpp"),
                  str(base.ROOT/"native_v2/tests"/test),*objects,"-o",str(dest)])
    return libraries,candidate,guard,base.run([cxx,"--version"]).stdout,base.run([faust,"--version"]).stdout

def oracles(output,libraries):
    hashes={}; classes={}
    for name,cls in (("master_fx","FaustMasterFX"),("bus_comp","FaustBusComp")):
        path=f"stave_synth/faust_{name}.py"; source=base.source_from_reference(path); hashes[path]=hashlib.sha256(source.encode()).hexdigest()
        defs=[n.value.args[0].value for n in ast.parse(source).body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call) and ast.unparse(n.value.func)=="_ffi.cdef"]
        if len(defs)!=1: raise RuntimeError("Pinned master ABI changed")
        ffi=FFI(); ffi.cdef(defs[0])
        env=base.declarations(source,[cls,"_install_ui_callbacks"],{"np":np,"_ffi":ffi,"_lib":ffi.dlopen(str(libraries[name]))})
        classes[cls]=env[cls]
    bridge=base.source_from_reference("stave_synth/jack_bridge.c"); kernels=[]
    for marker in ("void bridge_biquad(","void bridge_onepole(","double bridge_limiter_env("):
        if bridge.count(marker)!=1: raise RuntimeError("Pinned kernel boundary changed")
        begin=bridge.index(marker); kernels.append(bridge[begin:bridge.index("\n}\n",begin)+3])
    c=output/"reference_kernels.c"; c.write_text("#include <math.h>\n"+"\n".join(kernels))
    so=output/"reference_kernels.so"; base.run([shutil.which("cc"),"-std=c11","-O2","-ffp-contract=off","-shared","-fPIC",str(c),"-o",str(so)])
    kernel=ct.CDLL(str(so)); kernel.bridge_biquad.argtypes=[PD,PD,ct.c_int,PD,PD,PD]; kernel.bridge_biquad.restype=None
    kernel.bridge_onepole.argtypes=[PD,PD,ct.c_int,ct.c_double,ct.c_double,ct.c_double,PD]; kernel.bridge_onepole.restype=None
    synth=base.source_from_reference("stave_synth/synth_engine.py"); jack=base.source_from_reference("stave_synth/jack_engine.py")
    for path,src in (("stave_synth/jack_bridge.c",bridge),("stave_synth/synth_engine.py",synth),("stave_synth/jack_engine.py",jack)):
        hashes[path]=hashlib.sha256(src.encode()).hexdigest()
    names=["_biquad_run","_onepole_run","BiquadHighpass","OnePole6dBHighpass","BiquadPeakingEQ","BiquadLowShelf","BusCompressor"]
    env=base.declarations(synth,names,{"np":np,"SAMPLE_RATE":48000,"TWO_PI":2*np.pi,"lfilter":lfilter,
        "_BIQUAD_C":kernel.bridge_biquad,"_ONEPOLE_C":kernel.bridge_onepole,"_PD":PD,"ctypes":ct})
    classes.update({n:env[n] for n in names})
    tree=ast.parse(jack); limiter=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=="LookaheadLimiter")
    ns={"np":np,"sliding_window_view":sliding_window_view,"ctypes":ct}
    exec(compile(ast.Module(body=[limiter],type_ignores=[]),"pinned-limiter","exec"),ns)
    safe=copy.deepcopy(limiter); safe.name="SafeLimiter"
    process=next(n for n in safe.body if isinstance(n,ast.FunctionDef) and n.name=="process_inplace")
    attack=[n for n in ast.walk(process) if isinstance(n,ast.If) and ast.unparse(n.test)=="t < gain"]
    if len(attack)!=1: raise RuntimeError("Pinned limiter release branch changed")
    attack[0].orelse+=ast.parse("gain = min(t, gain)").body
    process.body+=ast.parse("np.clip(stereo, -self.ceiling, self.ceiling, out=stereo)").body
    exec(compile(ast.fix_missing_locations(ast.Module(body=[safe],type_ignores=[])),"explicit-ceiling-correction-oracle","exec"),ns)
    classes.update(LookaheadLimiter=ns["LookaheadLimiter"],SafeLimiter=ns["SafeLimiter"])
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=="JackEngine")
    loop=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=="_render_loop")
    def assign(node,name): return isinstance(node,ast.Assign) and any(ast.unparse(t)==name for t in node.targets)
    lists=[value for node in ast.walk(loop) for _,value in ast.iter_fields(node) if isinstance(value,list) and any(assign(x,"use_faust_master") for x in value)]
    if len(lists)!=1: raise RuntimeError("Pinned master render parent changed")
    body=lists[0]; idx=next(i for i,n in enumerate(body) if assign(n,"use_faust_master"))
    starts=[i for i,n in enumerate(body[:idx]) if assign(n,"piano_for_sc")]
    peak=next(i for i,n in enumerate(body) if assign(n,"pre_peak"))
    sat=[n for n in body[peak:] if isinstance(n,ast.If) and ast.unparse(n.test)=="not use_faust_master"]
    limit=[n for n in body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call) and ast.unparse(n.value.func)=="self._limiter.process_inplace"]
    if len(starts)!=1 or len(sat)!=1 or len(limit)!=1: raise RuntimeError("Pinned master routing boundary changed")
    fn=ast.parse("def process(self,pad,piano_pre):\n    pass\n").body[0]
    fn.body=ast.parse("""
bs=pad.shape[1]
split=self.bus_comp.enabled and self.bus_comp_fx_bypass
stereo=pad[2:4].copy() if split else pad[:2].copy()
fx_bus=pad[4:6].copy() if split else None
""").body+body[starts[0]:peak]+sat+limit+ast.parse("return stereo").body
    methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in
             ("_generate_bpm_sidechain","_get_sidechain","set_master_hp","set_master_eq")]
    if len(methods)!=4: raise RuntimeError("Pinned master method inventory changed")
    cls.name="MasterOracle"; cls.body=methods+[fn]
    env={"np":np,"SAMPLE_RATE":48000}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),"pinned-master-routing","exec"),env)
    def create(frames,reference):
        obj=env["MasterOracle"](); obj.synth=SimpleNamespace(reverb=SimpleNamespace(space=0),bpm=120,lfo_depth=0,_lfo_mod_a_last=0)
        obj._faust_master_fx=classes["FaustMasterFX"](); obj._native_comp=classes["FaustBusComp"](); obj._faust_bus_comp=obj._native_comp
        obj.bus_comp=classes["BusCompressor"](); obj.bus_comp_fx_bypass=False; obj.bus_comp_source="self"
        obj._limiter=classes["LookaheadLimiter" if reference else "SafeLimiter"](48000)
        if reference: obj._limiter.attach_bridge(kernel)
        obj._master_eq_l=[classes["BiquadPeakingEQ"](f,0,1.5,48000) for f in (200,1000,5000)]
        obj._master_eq_r=[classes["BiquadPeakingEQ"](f,0,1.5,48000) for f in (200,1000,5000)]
        for ch in ("l","r"):
            setattr(obj,"_master_hp6_"+ch,classes["OnePole6dBHighpass"](80,48000))
            setattr(obj,"_master_hp12_"+ch,classes["BiquadHighpass"](80,.707,48000))
            setattr(obj,"_master_hp24_"+ch,[classes["BiquadHighpass"](80,.707,48000) for _ in range(2)])
            setattr(obj,"_sat_dc_"+ch,classes["OnePole6dBHighpass"](15,48000))
        obj._shuffler_shelf=classes["BiquadLowShelf"](250,0,.707,48000); obj._shuffler_space_last=-1
        obj._bpm_beat_phase=0.; obj._bpm_pulse_remaining=0
        obj._sc_scratch=np.empty(frames); obj._sat_scratch=np.empty((2,frames))
        return obj
    return classes,create,hashes

def api(path):
    lib=ct.CDLL(str(path))
    sig={"master_create":([ct.c_uint,ct.c_int],ct.c_void_p),"master_delete":([ct.c_void_p],None),
        "master_configure":([ct.c_void_p,PD,ct.c_uint],ct.c_int),"master_process":([ct.c_void_p,PD,PD,ct.c_uint,ct.c_double,ct.c_double,ct.c_double],ct.c_int),
        "master_channel":([ct.c_void_p,ct.c_uint],PD),"master_state":([ct.c_void_p,PD],None),"master_command":([ct.c_void_p,ct.c_uint],ct.c_int),
        "limiter_create":([ct.c_uint,ct.c_int],ct.c_void_p),"limiter_delete":([ct.c_void_p],None),"limiter_process":([ct.c_void_p,PD,PD],ct.c_int),
        "limiter_channel":([ct.c_void_p,ct.c_uint],PD),"limiter_reset":([ct.c_void_p],None)}
    for name,(args,result) in sig.items(): getattr(lib,name).argtypes=args; getattr(lib,name).restype=result
    return lib

def limiter_compare(lib,classes,size):
    ptr=lambda x:x.ctypes.data_as(PD)
    result={"block_frames":size,"blocks":768,"peak_difference":[],"peak_output":[]}
    for reference in (1,0):
        rng=np.random.RandomState(base.SEED)
        ref=classes["LookaheadLimiter" if reference else "SafeLimiter"](48000)
        handle=lib.limiter_create(size,reference); delta=0; peak=0
        try:
            for block in range(768):
                signal=rng.uniform(-8,8,(2,size)) if block%5 else np.full((2,size),2.)
                if block%7==0: signal*=.01
                if block%11==0: signal.fill(0); signal[0,-1]=6
                if block%103==0: ref.reset(); lib.limiter_reset(handle)
                expected=signal.copy(); ref.process_inplace(expected)
                if not lib.limiter_process(handle,ptr(signal[0]),ptr(signal[1])): raise RuntimeError("Limiter render rejected")
                native=np.array([np.ctypeslib.as_array(lib.limiter_channel(handle,c),shape=(size,)) for c in (0,1)])
                delta=max(delta,float(np.max(np.abs(native-expected)))); peak=max(peak,float(np.max(np.abs(native))))
                if not np.isfinite(native).all() or (not reference and np.max(np.abs(native))>.98): raise RuntimeError("Limiter finite/ceiling failure")
            if delta>1e-12: raise RuntimeError(f"Limiter comparison failed: {delta}")
            result["peak_difference"].append(delta); result["peak_output"].append(peak)
        finally: lib.limiter_delete(handle)
    original=classes["LookaheadLimiter"](48000); x=np.full((2,size),2.); original.process_inplace(x)
    result["original_constant2_peak"]=float(np.max(np.abs(x)))
    if result["original_constant2_peak"]<=.98: raise RuntimeError("Original release overshoot not reproduced")
    return result

def fixture():
    return {0:{},16:{12:.5,1:3,4:-2,7:1},32:{9:1,10:200,11:6},48:{11:24,14:1},64:{15:1},
        80:{16:1},96:{19:1,21:8,26:.5},112:{19:2},128:{19:3,18:0,23:50},160:{11:12,24:0},
        192:{19:0,17:0,21:1000},224:{17:1},256:{15:0,14:0},272:{12:0,9:0,1:0,4:0,7:0},
        304:{15:1,19:1,26:0},336:{26:.0009},352:{26:.001},368:{26:.9989},384:{26:.999},
        400:{19:2,18:1,27:500,22:.1,20:-40},432:{19:3},480:{15:0,12:.001},
        496:{12:.006},512:{12:.0109},528:{12:.0111},544:{9:1,10:2000,11:12},576:{14:1,13:3},
        608:{15:1,19:0,17:1,16:0},640:{16:1,25:6},704:{17:0,19:3},768:{15:0,12:0},
        800:{15:1,19:1,21:4,24:2},864:{19:2},928:{19:0,17:0},992:{15:0}}

def configure(ref,v):
    ref.set_master_eq([dict(freq_hz=v[i],gain_db=v[i+1],q=v[i+2]) for i in (0,3,6)])
    ref.set_master_hp(v[10],int(v[11]),bool(v[9])); ref.synth.reverb.space=v[12]
    ref.pre_gain=v[13]; ref.saturation_enabled=bool(v[14]); ref.bus_comp.enabled=bool(v[15]); ref.bus_comp_fx_bypass=bool(v[16])
    ref._faust_bus_comp=ref._native_comp if v[17] else None; ref.bus_comp.release_auto=bool(v[18]); ref.bus_comp_source=SOURCES[int(v[19])]
    for name,value in zip(("threshold_db","ratio","attack_ms","release_ms","knee_db","makeup_db","mix","sidechain_hpf_hz"),v[20:28]): setattr(ref.bus_comp,name,value)
    ref.bus_comp._hpf_l.set_params(v[27],.707); ref.bus_comp._hpf_r.set_params(v[27],.707)

def compare(output,lib,create,size,reference,signals=None):
    ref=create(size,reference); handle=lib.master_create(size,reference)
    if not handle: raise RuntimeError("Native master construction failed")
    total=1024*512 if signals is None else signals.shape[1]
    config=DEFAULT.copy(); changes=fixture(); state_delta=np.zeros(5)
    actual=np.empty((2,total)); expected=np.empty_like(actual); ptr=lambda x:x.ctypes.data_as(PD)
    try:
        for frame in range(0,total,size):
            tick=frame//512
            if frame%512==0:
                for i,v in changes.get(tick,{}).items(): config[i]=v
                if tick in (129,196,441,732): ref._bpm_beat_phase=0; ref._bpm_pulse_remaining=2400; lib.master_command(handle,0)
                if tick in (450,810): ref._limiter.reset(); lib.master_command(handle,1)
            configure(ref,config)
            if not lib.master_configure(handle,ptr(config),len(config)): raise RuntimeError("Valid master config rejected")
            t=(frame+np.arange(size))/48000
            if signals is None:
                dry=np.array([.3*np.sin(2*np.pi*f*t) for f in (220,330)])
                wet=np.array([.15*np.cos(2*np.pi*f*t) for f in (164.81,196)])
                pad=np.vstack((dry+wet,dry,wet))
                if tick%180<4: pad*=8
            else: pad=np.ascontiguousarray(signals[:,frame:frame+size])
            piano=np.array([.12*np.cos(2*np.pi*f*t) for f in (261.63,392)]) if tick%150<110 else None
            bpm=97 if tick<400 else 143; depth=0 if tick%64<16 else .7; value=float(np.sin(t[0]*3))
            ref.synth.bpm=bpm; ref.synth.lfo_depth=depth; ref.synth._lfo_mod_a_last=value
            expected[:,frame:frame+size]=ref.process(pad,piano)
            if not lib.master_process(handle,ptr(pad),ptr(piano) if piano is not None else None,size,bpm,depth,value): raise RuntimeError(f"Master render failed at {frame}")
            for c in (0,1): actual[c,frame:frame+size]=np.ctypeslib.as_array(lib.master_channel(handle,c),shape=(size,))
            state=np.empty(5); lib.master_state(handle,ptr(state))
            wanted=np.array([ref._limiter._gain,ref.bus_comp._env_gr_db,ref._bpm_beat_phase,ref._bpm_pulse_remaining,ref._shuffler_space_last])
            state_delta=np.maximum(state_delta,np.abs(state-wanted))
        label=("synthetic" if signals is None else "ambience")+("-reference" if reference else "-safe")
        np.save(output/f"master-{label}-native-{size}.npy",actual,allow_pickle=False)
        np.save(output/f"master-{label}-oracle-{size}.npy",expected,allow_pickle=False)
        delta=np.max(np.abs(actual-expected),axis=1)
        if not np.isfinite(actual).all() or not np.isfinite(expected).all() or np.max(delta)>TOLERANCE or np.max(state_delta)>TOLERANCE:
            raise RuntimeError(f"Master mismatch {label}/{size}: audio {delta}, state {state_delta}")
        if not reference and np.max(np.abs(actual))>.98: raise RuntimeError("Safe master exceeded limiter ceiling")
        lib.master_command(handle,2)
        if lib.master_process(handle,ptr(pad),None,size,120,0,0): raise RuntimeError("Stopped master resumed")
        if any(np.any(np.ctypeslib.as_array(lib.master_channel(handle,c),shape=(size,))) for c in (0,1)): raise RuntimeError("STOP output not silent")
        return {"fixture":label,"frames":total,"block_frames":size,"peak_difference":delta.tolist(),"state_difference":state_delta.tolist(),
                "peak_output":float(np.max(np.abs(actual))),"terminal_stop":True}
    finally: lib.master_delete(handle)

def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--output-dir",type=Path); parser.add_argument("--ambience-dir",type=Path)
    args=parser.parse_args()
    if args.output_dir: output=args.output_dir.expanduser().resolve(); output.mkdir(mode=0o700,exist_ok=False)
    else: output=Path(tempfile.mkdtemp(prefix="stave-master-"))
    print(f"Evidence: {output}",flush=True)
    paths=[base.ROOT/p for p in ("native_v2/include/stave/lookahead_limiter.hpp","native_v2/include/stave/stage_master.hpp",
        "native_v2/src/master_filters.hpp","native_v2/src/stage_master.cpp","native_v2/src/stereo_faust_unit.hpp",
        "native_v2/tests/master_probe.cpp","native_v2/tests/test_stage_master.cpp","tools/compare_stage_master.py","tools/compare_native_v2.py","faust/faust_cprelude.h")]
    if args.ambience_dir: paths.extend(args.ambience_dir/f"ambience-sources-native-{size}.npy" for size in (512,256))
    hashes=lambda:{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report={"status":"started","host":platform.platform(),"reference":base.REFERENCE,"runs":[],"limiter_runs":[],"tolerance":TOLERANCE,
            "dependencies":{name:importlib.metadata.version(name) for name in ("numpy","scipy","cffi")},"python":platform.python_version()}
    try:
        report["source_sha256"]=hashes(); libraries,candidate,guard,compiler,faust=build(output)
        report.update(compiler=compiler,faust=faust); report["guards"]=base.run([str(guard)],timeout=60).stdout; print(report["guards"],flush=True)
        classes,create,report["oracle_sha256"]=oracles(output,libraries); lib=api(candidate)
        compressor=classes["BusCompressor"](); compressor.knee_db=0
        try:
            compressor._target_gr_db(compressor.threshold_db)
        except ZeroDivisionError:
            report["original_zero_knee_exact_threshold"]="ZeroDivisionError reproduced; native continuous limit checked by C++ guard"
        else:
            raise RuntimeError("Original exact-threshold zero-knee defect not reproduced")
        for size in (512,256):
            report["limiter_runs"].append(limiter_compare(lib,classes,size))
            inputs=[None]
            if args.ambience_dir:
                signal=np.load(args.ambience_dir/f"ambience-sources-native-{size}.npy",allow_pickle=False)
                if signal.ndim!=2 or signal.shape[0]!=6 or signal.shape[1]%512 or not np.isfinite(signal).all(): raise RuntimeError("Expected finite six-channel ambience evidence")
                inputs.append(signal)
            for signal in inputs:
                for reference in (1,0):
                    result=compare(output,lib,create,size,reference,signal); report["runs"].append(result); print(f"PASS: {result}",flush=True)
        (output/"fixture.json").write_text(json.dumps(fixture(),indent=2)+"\n")
        if hashes()!=report["source_sha256"]: raise RuntimeError("Inputs changed during comparison")
        report["status"]="passed_master_pre_bridge_only"
    except Exception as error: report.update(status="failed",error=str(error))
    report["commands"]=base.COMMANDS
    report["artifact_sha256"]={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir()) if p.is_file()}
    (output/"report.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k in ("status","error","limiter_runs")},indent=2))
    return 0 if report["status"].startswith("passed") else 1

if __name__=="__main__": raise SystemExit(main())
