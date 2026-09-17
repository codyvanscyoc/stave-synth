#!/usr/bin/env python3
"""Pinned offline global-LFO clock/ramp/routing comparison; no runtime imports.

Exercises original non-merged control math with synthetic audio, not a whole
instrument or Faust merged-LFO benchmark. Explicit prepared random tape.
"""
from __future__ import annotations
import argparse
import ast
from contextlib import nullcontext
import ctypes as ct
import hashlib
import json
import math
from pathlib import Path
import platform
import shutil
import tempfile
from types import SimpleNamespace
import numpy as np
from scipy.signal import lfilter
import compare_native_v2 as base

PD=ct.POINTER(ct.c_double)
DIVISIONS=("FREE","1/2","1/4.","1/4","1/4T","1/8.","1/8","1/8T","1/16")
SHAPES=("sine","triangle","square","saw","ramp","peak","sh")
TARGETS=("filter","amp","pan","bus")
FIELDS=("rate_hz","rate_multiplier","depth","spread","offset_ms","smooth","rate_mode","shape","target",
        "active","key_sync","invert","haas_compensate","poly")
STATE=("phase","sh_value","sh_value_r","mod_a_last","mod_b_last","smooth_a_state","smooth_b_state")
TOLERANCE=2e-12


class Tape:
    def __init__(self,values): self.values=values; self.index=0
    def uniform(self,low,high):
        if (low,high) not in ((-1.,1.),(-.06,.06),(-.12,.12)) or self.index>=len(self.values):
            raise RuntimeError("Unexpected/exhausted random draw")
        value=float(self.values[self.index])*high; self.index+=1; return value


def oracle(tape):
    source=base.source_from_reference("stave_synth/synth_engine.py")
    tree=ast.parse(source)
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=="SynthEngine")
    methods={n.name:n for n in cls.body if isinstance(n,ast.FunctionDef)}
    body=methods["_render_locked"].body
    def assignment(name,value=None):
        indices=[i for i,n in enumerate(body) if isinstance(n,ast.Assign) and any(ast.unparse(t)==name for t in n.targets)
                 and (value is None or ast.unparse(n.value)==value)]
        if len(indices)!=1: raise RuntimeError("Pinned motion boundary changed: "+name)
        return indices[0]
    start=assignment("(lfo1_a, lfo1_b)"); end=assignment("effective_cutoff","self._filter_cutoff_cur")
    advance=body[start:end]
    helpers=[n for n in body if isinstance(n,ast.FunctionDef) and n.name in ("_amp_gate","_fill_ramp","_smooth_one_pole","_osc_amp_pan")]
    if len(helpers)!=4: raise RuntimeError("Pinned motion helper inventory changed")
    routing=[n for n in body if isinstance(n,ast.If) and ast.unparse(n.test)=="all_recv"]
    if len(routing)!=1: raise RuntimeError("Pinned receive routing changed")
    route=routing[0]; split_test=ast.unparse(route.orelse[0].test)
    bus=body[assignment("bus_amul_l"):assignment("self._lfo2_mod_b_last")+1]
    fn=ast.parse("def render(self,n_samples,use_faust,output_l,output_r):\n    pass\n").body[0]
    fn.body=advance+helpers+[body[assignment("all_recv")]]+ast.parse("merged_lfo_block=False\nsplit_active=not all_recv and ("+split_test+")").body+[route]+bus+ast.parse(
        "if bus_amul_l is None: bus_amul_l=np.ones(n_samples); bus_amul_r=np.ones(n_samples)\n"
        "return np.array([output_l,output_r,bus_amul_l,bus_amul_r]),filter_mod,lfo1_d,lfo2_d,split_active").body
    key=ast.parse("def key(self):\n    pass\n").body[0]
    key.body=methods["_note_on_locked"].body[:2]
    if [ast.unparse(n.test) for n in key.body if isinstance(n,ast.If)]!=["self.lfo_key_sync","self.lfo2_key_sync"]:
        raise RuntimeError("Pinned key-sync boundary changed")
    target=ast.parse("def targets(self,params):\n    pass\n").body[0]
    target.body=[n for n in methods["update_params"].body if isinstance(n,ast.If) and ast.unparse(n.test) in (
        "'lfo_target' in params","'lfo2_target' in params")]
    if len(target.body)!=2: raise RuntimeError("Pinned target-reset boundary changed")
    divisions=next(n.value for n in cls.body if isinstance(n,ast.Assign) and any(ast.unparse(t)=="_DELAY_DIVISIONS" for t in n.targets))
    proxy=SimpleNamespace(**{name:getattr(np,name) for name in ("sin","linspace","multiply","ones","array","abs","float64")},random=tape)
    ns=base.declarations(source,["_osc_modulation_needs_split"],{})
    ns.update(np=proxy,math=math,lfilter=lfilter,TWO_PI=2*np.pi)
    filter_fn=ast.parse("def filter_motion(self,filter_mod):\n    pass\n").body[0]
    filter_fn.body=body[end:assignment("effective_cutoff","max(20.0, min(20000.0, effective_cutoff))")+1]+ast.parse(
        "return [effective_cutoff,self._filter_drift_val,self._filter_wobble_val]").body
    nodes=[methods["_advance_lfo"],fn,key,target,filter_fn]
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),"pinned-motion-oracle","exec"),ns)
    div=eval(compile(ast.Expression(divisions),"pinned-divisions","eval"),{})
    return ns,div,hashlib.sha256(source.encode()).hexdigest()


def make_reference(size,ns,div):
    obj=SimpleNamespace(sample_rate=48000,_DELAY_DIVISIONS=div,_render_lock=nullcontext(),
        _linspace_t_n=0,_filter_cutoff_last_set=8000.,lfo_target="filter",lfo2_target="pan")
    obj._advance_lfo=lambda n,which:ns["_advance_lfo"](obj,n,which)
    for prefix in ("_lfo","_lfo2"):
        for field in STATE: setattr(obj,prefix+"_"+field,0.)
    for name in ("_lfo_ramp_a1","_lfo_ramp_b1","_lfo_ramp_a2","_lfo_ramp_b2"): setattr(obj,name,np.zeros(size))
    obj._smooth_b=np.array([1.]); obj._smooth_a=np.array([1.,0.]); obj._smooth_zi=np.zeros(1)
    return obj


def apply_reference(obj,ns,values,reset_filter_marker=True):
    obj.motion_mix,obj.bpm,obj.haas_delay_ms=values[:3]
    if reset_filter_marker: obj._filter_cutoff_last_set=8000.
    ns["targets"](obj,{"lfo_target":TARGETS[int(values[11])],"lfo2_target":TARGETS[int(values[27])]})
    retune=obj._filter_cutoff_last_set==-1
    for j,prefix in enumerate(("lfo","lfo2")):
        fields=list(values[3+16*j:19+16*j])
        fields[6]=DIVISIONS[int(fields[6])]; fields[7]=SHAPES[int(fields[7])]; fields[8]=TARGETS[int(fields[8])]
        for name,value in zip(FIELDS,fields): setattr(obj,prefix+"_"+name,value)
        setattr(obj,f"osc1_recv_lfo{j+1}",bool(fields[14])); setattr(obj,f"osc2_recv_lfo{j+1}",bool(fields[15]))
    return retune


def fixture():
    configs=[]
    for shape in range(7):
        for division in range(9):
            for target in range(4):
                k=len(configs); config=[(0,.00099,.001,.5,1)[k%5],(40,120,240)[k%3],(5,20,40)[k%3]]
                for j in range(2):
                    config += [(0.05,1,20)[(k+j)%3],(.1,1,10)[(k//3+j)%3],(.001,.5,1)[(k+j)%3],
                        (0,.5,1)[(k+j)%3],(-500,-123,0,57,500)[(k+j)%5],(0,.001,.00101,.15,1)[(k//2+j)%5],
                        division,(shape+j)%7,(target+j)%4,int(k%7!=0),int(k%3==0),int(k%2==0),int(k%4==0),int(k%5==0),
                        int((k+j)%4!=1),int((k+j)%4!=2)]
                configs.append(config)
    # All target pairs and16 receive masks: explicitly exercises stacked
    # amp/pan/bus effects and the twice-stepped shared LFO in selective mode.
    for first in range(4):
        for second in range(4):
            for mask in range(16):
                c=[1,143,20]
                for j,target in enumerate((first,second)):
                    c += [11+2*j,1,.9,.8,(-123,57)[j],.6,0,(first+second+j)%7,target,
                          1,0,j,1,0,(mask>>(j*2))&1,(mask>>(j*2+1))&1]
                configs.append(c)
    # Persistent fast S&H includes >one nominal cycle per block. Preserve
    # legacy new_phase<previous wrap detection rather than changing draw count.
    for division in (0,8):
        for flags in ((1,1,1),(0,1,1),(1,0,1),(1,1,0)):
            c=[1,240,40]
            for j in range(2): c += [20,10,1,1,-500,.75,division,6,1 if j==0 else 3,*flags[:1],0,0,1,0,*flags[1:]]
            configs.append(c)
    return configs


def api(path):
    lib=ct.CDLL(str(path))
    for name,args,result in (("create",[ct.c_uint,PD,ct.c_uint],ct.c_void_p),("delete",[ct.c_void_p],None),
        ("config",[ct.c_void_p,PD,ct.c_uint],ct.c_int),("process",[ct.c_void_p,ct.c_int],ct.c_int),
        ("key",[ct.c_void_p],ct.c_int),("retune",[ct.c_void_p],ct.c_int),("stop",[ct.c_void_p],None),
        ("channel",[ct.c_void_p,ct.c_uint],PD),("state",[ct.c_void_p,PD],ct.c_int)):
        f=getattr(lib,"motion_"+name); f.argtypes=args; f.restype=result
    lib.filter_motion_config.argtypes=[ct.c_void_p,ct.c_double,ct.c_double]; lib.filter_motion_config.restype=ct.c_int
    lib.filter_motion_process.argtypes=[ct.c_void_p,ct.c_double,ct.c_double,ct.c_double,PD]; lib.filter_motion_process.restype=ct.c_int
    return lib


def compare_filter(lib,tape,output):
    # Runs on exact prepared normalized draws; original receives the scaled
    # .06/.12 values, preserving random-walk math and consumption independently.
    tape=tape.copy(); tape[:128]=1.; tape[128:384]=-1.
    np.save(output/"filter-random-tape.npy",tape,allow_pickle=False)
    random=Tape(tape); ns,_,_=oracle(random)
    obj=SimpleNamespace(_filter_drift_val=0.,_filter_wobble_val=0.)
    handle=lib.motion_create(512,tape.ctypes.data_as(PD),len(tape))
    if not handle: raise RuntimeError("Filter construction failed")
    expected=[]; actual=[]
    try:
        for block in range(6000):
            obj.filter_drift_cents=(0,.01,.010001,2,40)[(block//11)%5]
            obj.filter_wobble_amount=(0,.001,.00101,.25,1)[(block//17)%5]
            obj.filter_resonance=(.1,.5,.500001,.707,1,10)[block%6]
            obj._filter_cutoff_cur=(20.,np.exp(np.log(20.)),8000.,np.exp(np.log(20000.)),20000.)[block%5]
            modulation=(-1.4,-.00101,-.001,0,.001,.00101,1.4)[block%7]
            if not lib.filter_motion_config(handle,obj.filter_drift_cents,obj.filter_wobble_amount): raise RuntimeError("Filter config rejected")
            reference=ns["filter_motion"](obj,modulation)+[random.index]
            candidate=np.zeros(4)
            if not lib.filter_motion_process(handle,obj._filter_cutoff_cur,modulation,obj.filter_resonance,candidate.ctypes.data_as(PD)):
                raise RuntimeError("Filter process rejected")
            expected.append(reference); actual.append(candidate)
        expected=np.array(expected); actual=np.array(actual)
        np.save(output/"filter-reference.npy",expected,allow_pickle=False); np.save(output/"filter-native.npy",actual,allow_pickle=False)
        peak=np.max(np.abs(actual-expected),axis=0)
        if not np.isfinite(actual).all() or peak[0]>2e-9 or np.max(peak[1:3])>2e-12 or peak[3]!=0:
            raise RuntimeError("Filter-motion difference: "+str(peak.tolist()))
        if not all(np.any(expected[:,c]==sign) for c in (1,2) for sign in (-1.,1.)):
            raise RuntimeError("Filter random-walk clamps not exercised")
        return dict(blocks=len(actual),peak_difference=peak.tolist(),random_draws=random.index,
                    cutoff_tolerance=2e-9,state_tolerance=2e-12,both_random_walk_clamps_exercised=True)
    finally: lib.motion_delete(handle)


def compare(lib,size,configs,tape,output):
    random=Tape(tape); ns,div,_=oracle(random); obj=make_reference(size,ns,div)
    handle=lib.motion_create(size,tape.ctypes.data_as(PD),len(tape))
    if not handle: raise RuntimeError("Motion construction failed")
    peaks=np.zeros(2); frames=0; split_blocks=0; resets=0; key_resets=0; states=[]
    actual_hash=hashlib.sha256(); reference_hash=hashlib.sha256()
    try:
        for index,c in enumerate(configs):
            values=np.array(c,dtype=np.float64)
            if len(c)!=35 or not lib.motion_config(handle,values.ctypes.data_as(PD),len(c)): raise RuntimeError("Valid config rejected")
            retune=apply_reference(obj,ns,c)
            if bool(lib.motion_retune(handle))!=retune: raise RuntimeError("Target-change invalidation mismatch")
            resets+=retune
            for block in range(24 if index<len(configs)-8 else 96):
                if block==7 and index%3==0:
                    if not lib.motion_key(handle): raise RuntimeError("Key trigger refused")
                    ns["key"](obj); key_resets+=1
                use_faust=(index+block)%3!=0
                axis=(frames+np.arange(size))/48000
                signal=np.array([np.sin(2*np.pi*137*axis),.7*np.cos(2*np.pi*191*axis)])
                # Independently supplied magnitudes, including silent ratios.
                a,b=(0.,0.) if block%11==0 else ((.2,.8) if block%2 else (.8,.2))
                obj._osc1_pre_l=signal[0]*a; obj._osc1_pre_r=signal[1]*a
                obj._osc2_pre_l=signal[0]*b; obj._osc2_pre_r=signal[1]*b
                expected,filt,d1,d2,split=ns["render"](obj,size,use_faust,*signal.copy())
                if not lib.motion_process(handle,use_faust): raise RuntimeError("Motion render refused")
                state=np.zeros(19); lib.motion_state(handle,state.ctypes.data_as(PD))
                reference_state=np.array([getattr(obj,p+"_"+f) for p in ("_lfo","_lfo2") for f in STATE]+[d1,d2,filt,split,random.index])
                gain=np.array([np.ctypeslib.as_array(lib.motion_channel(handle,ch),shape=(size,)) for ch in range(10)])
                if bool(state[17])!=split or state[18]!=random.index: raise RuntimeError("Split/random ownership mismatch")
                if split:
                    m1=float(np.abs(obj._osc1_pre_l).sum()+np.abs(obj._osc1_pre_r).sum())
                    m2=float(np.abs(obj._osc2_pre_l).sum()+np.abs(obj._osc2_pre_r).sum())
                    r1=m1/(m1+m2) if m1+m2>1e-6 else .5; r2=m2/(m1+m2) if m1+m2>1e-6 else .5
                    first=signal*r1; second=signal*r2
                    first*=gain[:2]; first*=1+gain[2:4]
                    second*=gain[4:6]; second*=1+gain[6:8]
                    rendered=first+second
                else:
                    rendered=signal*gain[:2]; rendered*=1+gain[2:4]
                actual=np.vstack((rendered,gain[8:]))
                error=np.array([np.max(np.abs(actual-expected)),np.max(np.abs(state-reference_state))])
                if not np.isfinite(actual).all() or not np.isfinite(state).all() or np.max(error)>TOLERANCE:
                    np.save(output/f"failed-native-{size}.npy",actual,allow_pickle=False)
                    np.save(output/f"failed-reference-{size}.npy",expected,allow_pickle=False)
                    raise RuntimeError(f"Motion mismatch {size=} {index=} {block=} peaks={error.tolist()} state_delta={(state-reference_state).tolist()}")
                peaks=np.maximum(peaks,error); states.append(state); split_blocks+=split; frames+=size
                actual_hash.update(actual.tobytes()); reference_hash.update(expected.tobytes())
        lib.motion_stop(handle)
        if lib.motion_process(handle,1) or lib.motion_config(handle,values.ctypes.data_as(PD),35) or lib.motion_key(handle):
            raise RuntimeError("Terminal stop resumed")
        if any(np.any(np.ctypeslib.as_array(lib.motion_channel(handle,ch),shape=(size,))) for ch in range(10)):
            raise RuntimeError("Terminal stop not silent")
        np.save(output/f"states-{size}.npy",np.array(states),allow_pickle=False)
        return dict(block_frames=size,blocks=len(states),frames=frames,peak_audio_difference=float(peaks[0]),
            peak_state_difference=float(peaks[1]),selective_blocks=split_blocks,target_resets=resets,key_triggers=key_resets,
            random_draws=random.index,audio_native_sha256=actual_hash.hexdigest(),audio_reference_sha256=reference_hash.hexdigest())
    finally: lib.motion_delete(handle)


def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--output-dir",type=Path)
    args=parser.parse_args()
    if args.output_dir: output=args.output_dir.expanduser().resolve(); output.mkdir(mode=0o700,exist_ok=False)
    else: output=Path(tempfile.mkdtemp(prefix="stave-motion-"))
    print(f"Evidence: {output}",flush=True)
    paths=[Path(__file__),Path(base.__file__),*[base.ROOT/p for p in ("native_v2/include/stave/stage_motion.hpp",
        "native_v2/include/stave/shared_effects.hpp","native_v2/tests/stage_motion_probe.cpp","native_v2/tests/test_stage_motion.cpp")]]
    hashes=lambda:{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report={"status":"started","reference":base.REFERENCE,"host":platform.platform(),"peak_tolerance":TOLERANCE,"runs":[]}
    try:
        report["source_sha256"]=hashes(); _,_,report["oracle_sha256"]=oracle(Tape([]))
        cxx=shutil.which("c++")
        if not cxx: raise RuntimeError("Existing C++ compiler required")
        candidate=output/"stage_motion.so"; guard=output/"test_stage_motion"
        for dest,test,extra in ((candidate,"stage_motion_probe.cpp",["-shared"]),(guard,"test_stage_motion.cpp",["-fsanitize=undefined","-fno-sanitize-recover=all"])):
            base.run([cxx,"-std=c++17","-O2","-ffp-contract=off","-Wall","-Wextra","-Werror","-fPIC",*extra,
                      "-I",str(base.ROOT/"native_v2/include"),str(base.ROOT/"native_v2/tests"/test),"-o",str(dest)])
        report["guards"]=base.run([str(guard)],timeout=60).stdout
        configs=fixture(); (output/"fixture.json").write_text(json.dumps(configs)+"\n")
        tape=np.random.RandomState(base.SEED).uniform(-1,1,65536).astype(np.float64)
        np.save(output/"random-tape.npy",tape,allow_pickle=False)
        lib=api(candidate); report["runs"]=[compare(lib,size,configs,tape,output) for size in (512,256)]
        report["filter_motion"]=compare_filter(lib,tape,output)
        if hashes()!=report["source_sha256"]: raise RuntimeError("Sources changed during comparison")
        report["status"]="passed_motion_component_only"
    except Exception as error: report.update(status="failed",error=str(error))
    report["commands"]=base.COMMANDS
    report["artifact_sha256"]={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()}
    (output/"report.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k in ("status","error","guards","runs","filter_motion")},indent=2))
    return 0 if report["status"].startswith("passed") else 1


if __name__=="__main__": raise SystemExit(main())
