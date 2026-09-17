#!/usr/bin/env python3
"""Offline native delay and all shared-reverb types vs pinned v1.2 wrappers.

No runtime imports, devices, listeners, state writes or Pi contact. Original
wrapper classes execute with only relative backend imports replaced by the
explicit already-built offline classes; audio/control code is not rewritten.
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
from cffi import FFI
import compare_native_v2 as base

PD=ct.POINTER(ct.c_double)
TOLERANCE=1e-6
MODULES=(("ping_pong","StavePingPong","FaustPingPong"),("reverb","StaveReverb","FaustReverb"),
         ("plate","StavePlate","FaustPlate"),("drone","StaveDrone","FaustDrone"))
TYPES=("wash","hall","room","plate","bloom","drone","ghost")
DIVISIONS=("FREE","1/2","1/4.","1/4","1/4T","1/8.","1/8","1/8T","1/16")
DELAY_NAMES=("delay_enabled","delay_oblivion","delay_aurora_enabled","delay_time_mode","delay_reverse_window_mode",
             "bpm","delay_time_ms","delay_offset_ms","delay_rate_multiplier","delay_feedback","delay_wet","motion_mix",
             "delay_low_cut_hz","delay_high_cut_hz","delay_drive","delay_width","delay_mod_rate_hz","delay_mod_depth_ms",
             "delay_reverse_amount","delay_reverse_window_ms","delay_reverse_feedback","delay_aurora_seconds")
DEFAULT=np.array([0,0,0,3,0,120,375,0,1,.35,0,1,20,18000,0,1,.5,0,0,500,0,5,0],dtype=np.float64)
SETTERS=("set_decay","set_low_cut","set_high_cut","set_damp","set_shimmer_feedback","set_noise_mod","set_predelay")
ZONES=("feedback","damp","freeze_input","predelay_ms","low_cut_hz","high_cut_hz","er_scale","shimmer_fb","noise_mod","drone_key")

def build(output):
    cc,cxx,faust=(shutil.which(n) for n in ("cc","c++","faust"))
    if not all((cc,cxx,faust)): raise RuntimeError("Existing C/C++ and Faust required")
    include=Path(faust).resolve().parent.parent/"include"
    objects=[]; libraries={}
    for name,cls,_ in MODULES:
        dsp,c,obj=(output/f"{name}.{s}" for s in ("dsp","c","o"))
        dsp.write_text(base.source_from_reference(f"faust/{name}.dsp"))
        base.run([faust,"-lang","c","-cn",cls,"-o",str(c),str(dsp)])
        base.run([cc,"-std=c11","-O2","-ffp-contract=off","-fPIC","-I",str(include),
                  "-include",str(base.ROOT/"faust/faust_cprelude.h"),"-c",str(c),"-o",str(obj)])
        library=output/f"reference_{name}.so"
        base.run([cc,"-shared",str(obj),"-o",str(library)])
        libraries[name]=library; objects.append(str(obj))
    candidate,guard=output/"shared_effects.so",output/"test_shared_effects"
    for dest,test,extra in ((candidate,"shared_effects_probe.cpp",["-shared"]),
                          (guard,"test_shared_effects.cpp",["-fsanitize=undefined","-fno-sanitize-recover=all"])):
        base.run([cxx,"-std=c++17","-O2","-ffp-contract=off","-fPIC","-Wall","-Wextra","-Werror",*extra,
                  "-I",str(include),"-I",str(base.ROOT/"native_v2/include"),
                  str(base.ROOT/"native_v2/src/shared_effects.cpp"),str(base.ROOT/"native_v2/tests"/test),
                  *objects,"-o",str(dest)])
    return libraries,candidate,guard,base.run([cxx,"--version"]).stdout,base.run([faust,"--version"]).stdout

def oracles(libraries):
    classes={}; hashes={}
    # Simple wrappers first; only declarations and FFI ABI are executed.
    for name,_,cls in (MODULES[0],MODULES[2],MODULES[3],MODULES[1]):
        path=f"stave_synth/faust_{name}.py"; source=base.source_from_reference(path)
        hashes[path]=hashlib.sha256(source.encode()).hexdigest(); tree=ast.parse(source)
        defs=[n.value.args[0].value for n in tree.body if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
              and ast.unparse(n.value.func)=="_ffi.cdef"]
        if len(defs)!=1: raise RuntimeError("Pinned effect ABI inventory mismatch")
        ffi=FFI(); ffi.cdef(defs[0]); lib=ffi.dlopen(str(libraries[name]))
        env={"np":np,"_ffi":ffi,"_lib":lib,"logger":logging.getLogger("offline-oracle")}
        if name=="reverb":
            # No lock/diagnostics necessary in this single-owner oracle. Only
            # remove decorator behavior; original method bodies remain intact.
            env.update(_native_owned=lambda f:f,os=SimpleNamespace(environ={}),
                       threading=SimpleNamespace(RLock=lambda:None),**classes)
            assignments=[n for n in tree.body if isinstance(n,ast.Assign) and
                         any(ast.unparse(t)=="REVERB_PRESETS" for t in n.targets)]
            if len(assignments)!=1: raise RuntimeError("Pinned preset inventory mismatch")
            env["REVERB_PRESETS"]=ast.literal_eval(assignments[0].value)
            class RemovePreparedImports(ast.NodeTransformer):
                count=0
                def visit_ImportFrom(self,node):
                    if node.level==1 and node.module in ("faust_plate","faust_drone"):
                        self.count+=1; return ast.copy_location(ast.Pass(),node)
                    raise RuntimeError("Unexpected import in offline reverb declaration")
            transform=RemovePreparedImports()
            selected=[n for n in tree.body if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name in
                      (cls,"_install_ui_callbacks","_set_zone","_plate_decay_from_seconds","_drone_fb_from_seconds")]
            if len(selected)!=5: raise RuntimeError("Pinned reverb declaration mismatch")
            module=ast.fix_missing_locations(transform.visit(ast.Module(body=selected,type_ignores=[])))
            if transform.count!=4: raise RuntimeError("Pinned backend import count changed")
            exec(compile(module,"pinned-offline-reverb","exec"),env)
        else:
            env=base.declarations(source,[cls,"_install_ui_callbacks"],env)
        classes[cls]=env[cls]
    source=base.source_from_reference("stave_synth/synth_engine.py")
    hashes["stave_synth/synth_engine.py"]=hashlib.sha256(source.encode()).hexdigest()
    cls=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=="SynthEngine")
    methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in
             ("_delay_time_samples","_effective_reverse_window_ms","_process_ping_pong")]
    divisions=[n for n in cls.body if isinstance(n,ast.Assign) and any(ast.unparse(t)=="_DELAY_DIVISIONS" for t in n.targets)]
    if len(methods)!=3 or len(divisions)!=1: raise RuntimeError("Pinned delay methods changed")
    cls.body=divisions+methods
    env={"np":np}; exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),"pinned-delay-owner","exec"),env)
    classes["DelayOwner"]=env["SynthEngine"]
    return classes,hashes

def native_api(path):
    lib=ct.CDLL(str(path))
    signatures={"effects_create":([ct.c_uint,ct.c_uint],ct.c_void_p),
        "effects_delete":([ct.c_uint,ct.c_void_p],None),"delay_configure":([ct.c_void_p,PD,ct.c_uint],ct.c_int),
        "effects_process":([ct.c_uint,ct.c_void_p,PD,PD,PD,PD],ct.c_int),
        "effects_channel":([ct.c_uint,ct.c_void_p,ct.c_uint],PD),
        "reverb_command":([ct.c_void_p,ct.c_uint,ct.c_double],ct.c_int),
        "reverb_zone":([ct.c_void_p,ct.c_uint,ct.c_char_p],ct.c_double),
        "reverb_state":([ct.c_void_p,ct.c_uint],ct.c_int),
        "effects_clear":([ct.c_uint,ct.c_void_p],None),"effects_stop":([ct.c_uint,ct.c_void_p],None)}
    for name,(args,result) in signatures.items():
        fn=getattr(lib,name); fn.argtypes=args; fn.restype=result
    return lib

def fixtures():
    delay={0:{0:1,10:.5},48:{7:-200,8:-.1},96:{3:0,6:1,7:200,8:10},160:{0:0},190:{0:1},
           240:{10:0,18:.65,19:50,20:.6},310:{4:5,5:97},390:{14:.8,15:.2,16:3.3,17:8},
           470:{11:0},490:{11:1},550:{1:1,10:.6,18:.3},650:{1:0,8:1,7:0},
           720:{2:1,21:3},1100:{2:0,4:0,19:1000},1250:{18:0,10:0},1300:{10:.0009},
           1310:{10:.001},1400:{10:.5,18:.4},1800:{0:0},1820:{0:1},2100:{2:1,21:15},
           3500:{18:0,10:.1},3800:{0:0}}
    # Repeated type is intentionally a no-op after parameter edits. Exercise
    # all backends frozen, sealed, edited, changed, restored and panicked.
    reverb={0:[(0,4),(3,.4)],30:[(7,1)],60:[(7,2)],90:[(7,4)],120:[(7,6)],150:[(7,3)],
            170:[(0,0)],180:[(7,3)],200:[(7,5)],230:[(7,0)],260:[(8,1)],
            280:[(0,12),(3,.7)],300:[(7,3)],470:[(7,5)],500:[(7,4)],530:[(8,0)],
            600:[(7,5),(8,1)],810:[(0,2),(3,.2)],820:[(8,0)],900:[(7,3),(8,1)],
            1100:[(9,0)],1150:[(7,0)],1250:[(4,.7),(5,.6),(6,150)],
            1450:[(1,400),(2,4000)],1600:[(7,1)],1700:[(7,1)],1900:[(9,0)],
            2000:[(7,6)],2400:[(7,5)],2800:[(7,3)],3200:[(7,4)],3600:[(8,1)],3900:[(8,0)]}
    return delay,reverb

def compare(output,lib,classes,size):
    refs=[classes["DelayOwner"](),classes["FaustReverb"]()]
    refs[0].sample_rate=48000; refs[0]._faust_ping_pong=classes["FaustPingPong"]()
    handles=[lib.effects_create(k,size) for k in (0,1)]
    if not all(handles): raise RuntimeError("Native effect construction failed")
    changes,events=fixtures(); config=DEFAULT.copy(); ptr=lambda a:a.ctypes.data_as(PD)
    total=4096*512; peak=np.zeros(4); delta=np.zeros(4); zone_delta=0; sealed=0
    actual=np.empty((4,total)); expected=np.empty_like(actual)
    try:
        for frame in range(0,total,size):
            tick=frame//512
            if frame%512==0:
                for i,v in changes.get(tick,{}).items(): config[i]=v
                for action,value in events.get(tick,[]):
                    if not lib.reverb_command(handles[1],action,value): raise RuntimeError("Valid reverb command rejected")
                    if action<7: getattr(refs[1],SETTERS[action])(value)
                    elif action==7: refs[1].set_type(TYPES[value])
                    elif action==8: refs[1].set_freeze(bool(value))
                    elif action==9: refs[1].panic()
                if tick==1600: lib.effects_clear(0,handles[0]); refs[0]._faust_ping_pong.clear()
            if not lib.delay_configure(handles[0],ptr(config),len(config)): raise RuntimeError("Delay configuration rejected")
            for i,name in enumerate(DELAY_NAMES):
                setattr(refs[0],name,DIVISIONS[int(config[i])] if i in (3,4) else config[i])
            t=(np.arange(size)+frame)/48000
            signal=np.array([.18*np.sin(2*np.pi*220*t)+.08*np.sin(2*np.pi*329.63*t),.16*np.sin(2*np.pi*164.81*t)])
            if tick%360>90: signal.fill(0)
            if frame==0: signal[:,0]+=[.5,-.4]
            ext=np.array([.09*np.cos(2*np.pi*261.63*t),.07*np.sin(2*np.pi*392*t)]) if tick%500<180 else None
            refs[0]._external_delay_send=ext
            delay=signal.copy(); refs[0]._process_ping_pong(delay[0],delay[1])
            reverb=refs[1].process(signal).copy()
            for k in (0,1):
                ep=[ptr(ext[c]) for c in (0,1)] if k==0 and ext is not None else [None,None]
                if not lib.effects_process(k,handles[k],ptr(signal[0]),ptr(signal[1]),*ep): raise RuntimeError("Native render failed")
                for c in (0,1): actual[k*2+c,frame:frame+size]=np.ctypeslib.as_array(lib.effects_channel(k,handles[k],c),shape=(size,))
            expected[:,frame:frame+size]=np.vstack((delay,reverb))
            ref=refs[1]
            if [lib.reverb_state(handles[1],i) for i in range(3)]!=[TYPES.index(ref.type),int(ref.frozen),ref._freeze_capture_remaining]:
                raise RuntimeError(f"Reverb lifecycle mismatch at {frame}")
            sealed+=int(ref.frozen and ref._freeze_capture_remaining<=0)
            for backend,obj in enumerate((ref,ref._plate,ref._drone)):
                for name in ZONES:
                    native=lib.reverb_zone(handles[1],backend,name.encode()); z=obj._zones.get(name)
                    if z is None:
                        if not np.isnan(native): raise RuntimeError("Unexpected native zone")
                    else:
                        d=abs(float(z[0])-native); zone_delta=max(zone_delta,d)
                        if not np.isfinite(d) or d!=0: raise RuntimeError(f"Zone mismatch {frame}/{backend}/{name}: {z[0]} vs {native}")
        if not np.isfinite(actual).all() or not np.isfinite(expected).all(): raise RuntimeError("Nonfinite comparison")
        delta=np.max(np.abs(actual-expected),axis=1); peak=np.max(np.abs(actual),axis=1)
        np.save(output/f"effects-native-{size}.npy",actual,allow_pickle=False)
        np.save(output/f"effects-reference-{size}.npy",expected,allow_pickle=False)
        if np.max(delta)>TOLERANCE: raise RuntimeError(f"Audio mismatch at {size}: {delta}")
        for k in (0,1):
            lib.effects_stop(k,handles[k])
            if lib.effects_process(k,handles[k],ptr(signal[0]),ptr(signal[1]),None,None): raise RuntimeError("Terminal stop resumed")
            if any(np.any(np.ctypeslib.as_array(lib.effects_channel(k,handles[k],c),shape=(size,))) for c in (0,1)):
                raise RuntimeError("Terminal stop not silent")
        return {"frames":total,"block_frames":size,"peak_difference":delta.tolist(),"peak":peak.tolist(),
                "zone_difference":zone_delta,"sealed_freeze_blocks":sealed,"terminal_stop":True}
    finally:
        for k,h in enumerate(handles): lib.effects_delete(k,h)

def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--output-dir",type=Path)
    args=parser.parse_args()
    if args.output_dir: output=args.output_dir.expanduser().resolve(); output.mkdir(mode=0o700,exist_ok=False)
    else: output=Path(tempfile.mkdtemp(prefix="stave-shared-effects-"))
    print(f"Evidence: {output}",flush=True)
    paths=[base.ROOT/p for p in ("native_v2/include/stave/shared_effects.hpp","native_v2/src/shared_effects.cpp",
        "native_v2/src/stereo_faust_unit.hpp","native_v2/tests/shared_effects_probe.cpp","native_v2/tests/test_shared_effects.cpp",
        "native_v2/tests/delay_fixture.hpp","tools/compare_shared_effects.py","tools/compare_native_v2.py","faust/faust_cprelude.h")]
    hashes=lambda:{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report={"status":"started","host":platform.platform(),"reference":base.REFERENCE,"runs":[],"tolerance":TOLERANCE}
    try:
        report["source_sha256"]=hashes(); libraries,candidate,guard,compiler,faust=build(output)
        report.update(compiler=compiler,faust=faust)
        report["guards"]=base.run([str(guard)],timeout=60).stdout; print(report["guards"],flush=True)
        classes,report["oracle_sha256"]=oracles(libraries); lib=native_api(candidate)
        for size in (512,256):
            result=compare(output,lib,classes,size); report["runs"].append(result); print(f"PASS {size}: {result}",flush=True)
        (output/"fixture.json").write_text(json.dumps(fixtures(),indent=2)+"\n")
        if hashes()!=report["source_sha256"]: raise RuntimeError("Source changed during verification")
        report["status"]="passed_shared_effect_components_only"
    except Exception as error: report.update(status="failed",error=str(error))
    report["commands"]=base.COMMANDS
    report["artifact_sha256"]={str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir()) if p.is_file()}
    (output/"report.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k in ("status","error","runs")},indent=2))
    return 0 if report["status"].startswith("passed") else 1

if __name__=="__main__": raise SystemExit(main())
