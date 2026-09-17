#!/usr/bin/env python3
"""Device-free native pad/filter routing plus piano room vs pinned v1.2.

Explicit scope: global LFO/drift/wobble and ping-pong disabled on both sides.
No app imports, audio/MIDI/network, installation or production state access.
Requires existing compilers, Faust, NumPy, CFFI. NEW evidence directory only.
"""
from __future__ import annotations
import argparse
import ast
import ctypes as ct
import hashlib
import json
from pathlib import Path
import platform
import shutil
import tempfile
from types import SimpleNamespace
import numpy as np
from cffi import FFI
import compare_native_v2 as base
import compare_native_room as room

ROOT = base.ROOT
TOLERANCE = 1e-6
PD = ct.POINTER(ct.c_double)
CONFIG_NAMES = ("filter_cutoff", "filter_resonance", "filter_range_min", "filter_range_max", "slope24",
                "osc1_filter_enabled", "osc2_filter_enabled", "osc1_indep_cutoff", "osc2_indep_cutoff",
                "filter_highpass_hz", "osc1_reverb_send", "osc2_reverb_send", "osc1_fx_bypass", "osc2_fx_bypass",
                "_haas_delay_samples", "shimmer_enabled", "shimmer_high", "shimmer_mix", "shimmer_send",
                "piano_room_enabled", "reverb_dry_wet", "piano_room_size", "piano_room_damp")
DEFAULT = np.array([8000,.707,150,20000,0,1,1,20000,20000,20,1,1,0,0,960,0,0,.5,1,1,.4,.5,.6], dtype=np.float64)


def build(output):
    cc, cxx, faust = (shutil.which(n) for n in ("cc", "c++", "faust"))
    if not all((cc,cxx,faust)): raise RuntimeError("Installed C/C++ and Faust required")
    include=Path(faust).resolve().parent.parent / "include"
    common=["-O2","-ffp-contract=off","-fPIC","-I",str(include)]
    objects=[]; refs=[]
    for name, cls, double in (("pad_bus","StavePadBus",True),("piano_room","StavePianoRoom",False)):
        dsp, generated, obj=(output/f"{name}.{ext}" for ext in ("dsp","c","o"))
        dsp.write_text(base.source_from_reference(f"faust/{name}.dsp"))
        base.run([faust, *(["-double"] if double else []), "-lang","c","-cn",cls,"-o",str(generated),str(dsp)])
        base.run([cc,"-std=c11",*common,*(["-DFAUSTFLOAT=double"] if double else []),
                  "-include",str(ROOT/"faust/faust_cprelude.h"),"-c",str(generated),"-o",str(obj)])
        reference=output/f"reference_{name}.so"
        base.run([cc,"-shared",str(obj),"-o",str(reference)])
        objects.append(str(obj)); refs.append(reference)
    candidate, guard=output/"native_buses.so",output/"test_stage_buses"
    for dest,test,extra in ((candidate,"stage_buses_probe.cpp",["-shared"]),
                            (guard,"test_stage_buses.cpp",["-fsanitize=undefined","-fno-sanitize-recover=all"])):
        base.run([cxx,"-std=c++17",*common,"-Wall","-Wextra","-Werror",*extra,
                  "-I",str(ROOT/"native_v2/include"),str(ROOT/"native_v2/src/pad_bus.cpp"),
                  str(ROOT/"native_v2/src/piano_room.cpp"),str(ROOT/"native_v2/tests"/test),
                  *objects,"-o",str(dest)])
    return refs,candidate,guard,base.run([cxx,"--version"]).stdout,base.run([faust,"--version"]).stdout


def pad_oracle(library, fallback_types=None, with_delay=False, motion_env=None, merged_motion=False):
    source=base.source_from_reference("stave_synth/faust_pad_bus.py")
    definitions=[n.value.args[0].value for n in ast.parse(source).body if isinstance(n,ast.Expr)
                 and isinstance(n.value,ast.Call) and ast.unparse(n.value.func)=="_ffi.cdef"]
    if len(definitions)!=1: raise RuntimeError("Pinned pad ABI inventory mismatch")
    ffi=FFI(); ffi.cdef(definitions[0])
    ns=base.declarations(source,["FaustPadBus","_install_ui_callbacks"],
                         {"np":np,"_ffi":ffi,"_lib":ffi.dlopen(str(library)),"_HAAS_MAX":4095,"_N_OUT":9})
    engine=base.source_from_reference("stave_synth/synth_engine.py")
    cls=next(n for n in ast.parse(engine).body if isinstance(n,ast.ClassDef) and n.name=="SynthEngine")
    method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=="_render_locked")
    body=method.body
    def assignment(nodes,name):
        matches=[i for i,n in enumerate(nodes) if isinstance(n,ast.Assign) and any(ast.unparse(t)==name for t in n.targets)]
        if len(matches)!=1: raise RuntimeError(f"Pinned assignment boundary mismatch: {name}")
        return matches[0]
    def condition(nodes,text):
        matches=[i for i,n in enumerate(nodes) if isinstance(n,ast.If) and ast.unparse(n.test)==text]
        if len(matches)!=1: raise RuntimeError(f"Pinned branch boundary mismatch: {text}")
        return matches[0]
    # Actual scalar logic, including original clamps/thresholds/NumPy order.
    first=assignment(body,"alpha_s"); last=assignment(body,"filter_comp")
    scalar=body[first:last+1]
    pad_candidates=[n for n in body if isinstance(n,ast.If) and ast.unparse(n.test)=="use_pad_bus"
                    and any(isinstance(x,ast.Call) and ast.unparse(x.func)=="self._faust_pad_bus.set_block_params"
                            for x in ast.walk(n))]
    if len(pad_candidates)!=1: raise RuntimeError("Pinned pad render boundary mismatch")
    pad=pad_candidates[0].body
    merged=condition(pad,"use_merged")
    # Omit bank invocation/LFO branch only: source stems are explicit input;
    # global LFO is disabled in this scope. Still use original scalar zone push.
    pad_setup=pad[:merged]
    dry_copy=pad[merged+1:]
    carve_start=assignment(body,"bypass_ratio")
    carve_end=condition(body,"self.osc1_fx_bypass or self.osc2_fx_bypass")
    carve=body[carve_start:carve_end+1]
    send_start=assignment(body,"s1")
    send_end=condition(body,"abs(s1 - 1.0) < 1e-06 and abs(s2 - 1.0) < 1e-06")
    sends=body[send_start:send_end+1]
    shimmer=body[send_end+1]
    if not isinstance(shimmer,ast.If) or ast.unparse(shimmer.test)!="use_pad_bus":
        raise RuntimeError("Pinned shimmer-add boundary mismatch")
    intro=ast.parse("""
n_samples=signal.shape[1]
render_osc2=bool(flags & 1)
voice_idx=1 if flags & 2 else 0
haas_active=bool(flags & 4)
render_shimmer=self.shimmer_enabled and self.shimmer_mix > 0.001
use_pad_bus=native_active
filter_buf=np.zeros((2,n_samples))
osc1_indep_buf=np.zeros((2,n_samples))
osc2_indep_buf=np.zeros((2,n_samples))
pad_out=np.zeros((9,n_samples))
output_l=np.zeros(n_samples)
output_r=np.zeros(n_samples)
pad_bus_send_l=None
pad_bus_send_r=None
self._osc1_pre_l=signal[0]
self._osc1_pre_r=signal[1]
self._osc2_pre_l=signal[2]
self._osc2_pre_r=signal[3]
""").body
    compute=ast.parse("pad_out=self._faust_pad_bus.process(np.ascontiguousarray(signal[:5]))").body
    before_route=[]; after_route=[]
    if motion_env is not None:
        # Retain original clock/filter bodies above, and original dry modulation
        # bodies below. Optional merged test exercises actual Faust pad LFO
        # zones/state handoff on the SAME explicitly supplied source stems.
        before_route=ast.parse("use_faust=native_active\nuse_merged=native_active and "+str(merged_motion)).body
        before_route+=body[assignment(body,"all_recv"):assignment(body,"faust_lfo2")+1]
        helpers=[n for n in body if isinstance(n,ast.FunctionDef) and n.name in ("_amp_gate","_fill_ramp","_smooth_one_pole","_osc_amp_pan")]
        if len(helpers)!=4: raise RuntimeError("Motion helper boundary changed")
        after_route=helpers+[body[condition(body,"all_recv")]]+body[assignment(body,"bus_amul_l"):assignment(body,"self._lfo2_mod_b_last")+1]
        after_route+=ast.parse("self._bus_amul_l=bus_amul_l\nself._bus_amul_r=bus_amul_r\nself._effective_cutoff=effective_cutoff").body
        if merged_motion:
            mb=pad[merged].body
            push=mb[:3]
            if not (isinstance(push[2],ast.Expr) and ast.unparse(push[2].value.func)=="self._faust_pad_bus.push_lfo_block"):
                raise RuntimeError("Merged LFO push boundary changed")
            sync=[n for n in mb if isinstance(n,ast.If) and ast.unparse(n.test) in ("faust_lfo1","faust_lfo2")]
            if len(sync)!=2: raise RuntimeError("Merged LFO sync boundary changed")
            compute=push+compute+sync
    tail=ast.parse("""
if bypass_l is None:
    bypass_l=np.zeros(n_samples)
    bypass_r=np.zeros(n_samples)
result=np.stack([output_l,output_r,reverb_in_l,reverb_in_r,bypass_l,bypass_r,pad_out[6],pad_out[7],pad_out[8]])
state=np.array([self._filter_cutoff_cur,self._osc1_indep_cutoff_cur,self._osc2_indep_cutoff_cur,
                self._filter_highpass_cur,self._shimmer_mix_cur,self._filter_cutoff_last_set,
                self._filter_res_last_set,bypass_ratio])
return result,state
""").body
    fn=ast.parse("def render(self,signal,flags,native_active=True):\n    pass\n").body[0]
    route=ast.If(test=ast.Name(id="use_pad_bus",ctx=ast.Load()),body=pad_setup+compute+dry_copy,
                 orelse=pad_candidates[0].orelse)
    capture=ast.parse("self._pre_fx_snapshot=np.array([output_l,output_r])").body if with_delay else []
    delay=ast.parse("self._process_ping_pong(output_l,output_r)").body if with_delay else []
    fn.body=intro+scalar+before_route+[route]+after_route+capture+carve+delay+sends+[shimmer]+tail
    env={"np":np,"_Q24_S1_RATIO":.5412/.707,"_Q24_S2_RATIO":1.3066/.707}
    if motion_env is not None:
        env.update(motion_env)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),"pinned-pad-scalar-routing", "exec"),env)
    def create():
        obj=SimpleNamespace(sample_rate=48000,_faust_pad_bus=ns["FaustPadBus"](48000),
            _filter_cutoff_cur=8000.,_osc1_indep_cutoff_cur=20000.,_osc2_indep_cutoff_cur=20000.,
            _filter_highpass_cur=20.,_shimmer_mix_cur=.5,_filter_cutoff_last_set=-1.,_filter_res_last_set=-1.,
            lfo_depth=0.,lfo2_depth=0.,motion_mix=0.,lfo_active=False,lfo2_active=False,
            osc1_recv_lfo1=True,osc1_recv_lfo2=True,osc2_recv_lfo1=True,osc2_recv_lfo2=True,
            lfo_target="amp",lfo2_target="amp",filter_drift_cents=0.,filter_wobble_amount=0.,
            reverb_filter_enabled=False,_advance_lfo=lambda *a,**k:(0.,0.),
            _reverb_in_l=np.zeros(512),_reverb_in_r=np.zeros(512),_bypass_snap_l=np.zeros(512),_bypass_snap_r=np.zeros(512))
        # Old Python coefficient setters are irrelevant to active Faust output;
        # preserve the original scalar branch without importing fallback DSP.
        for name in ("filter_l","filter_r","filter2_l","filter2_r","osc1_indep_filter_l","osc1_indep_filter_r",
                     "osc2_indep_filter_l","osc2_indep_filter_r","_rev_send_filter_l","_rev_send_filter_r",
                     "_rev_send_filter2_l","_rev_send_filter2_r"):
            if fallback_types:
                setattr(obj,name,fallback_types[0](20000 if "indep" in name else 8000,.707,48000))
            else: setattr(obj,name,SimpleNamespace(set_params=lambda *args:None))
        if fallback_types:
            obj.filter_hp_l=fallback_types[1](20,.707,48000)
            obj.filter_hp_r=fallback_types[1](20,.707,48000)
        return obj
    return create,env["render"],{p:hashlib.sha256(s.encode()).hexdigest() for p,s in (("wrapper",source),("engine",engine))}


def native_api(path):
    lib=ct.CDLL(str(path))
    for name,result,args in (("create",ct.c_void_p,[ct.c_uint]),("delete",None,[ct.c_void_p]),
        ("configure",ct.c_int,[ct.c_void_p,PD,ct.c_uint]),("process",ct.c_int,[ct.c_void_p,ct.POINTER(PD),ct.c_uint]),
        ("stem",PD,[ct.c_void_p,ct.c_uint]),("state",None,[ct.c_void_p,PD]),
        ("clear",None,[ct.c_void_p]),("stop",None,[ct.c_void_p])):
        fn=getattr(lib,"buses_"+name); fn.restype=result; fn.argtypes=args
    return lib


def fixture():
    # Indexed by canonical512-frame block. Each cadence has its own reference.
    changes={0:{15:1},8:{0:300,1:1.5},16:{4:1},24:{5:0,7:1200},32:{6:0,8:500},
        40:{0:16000,9:120},48:{10:.2,11:1.4},56:{12:1},64:{12:0,13:1},72:{12:1},
        80:{12:0,13:0,10:1,11:1},88:{5:1,6:1,4:0},96:{4:1,17:0},104:{17:.8,16:1},
        112:{18:0},120:{18:1.7},128:{15:0},136:{15:1,16:0},144:{9:20},
        152:{14:240},160:{14:1920},168:{19:0},176:{19:1,20:.9,21:.9,22:.2},
        184:{20:0},192:{20:.7},200:{4:0,0:20000,1:.1},208:{0:20,4:1,1:10},
        216:{1:.707,2:20000,3:20},224:{2:150,3:20000,5:0,6:0},
        232:{7:20,8:20000},240:{10:.9999995,11:1.0000005},248:{10:.999998,11:1.000002},
        256:{5:1,6:1,0:8000,9:5000},272:{9:25},280:{9:20},288:{17:.001},296:{17:.00101},
        304:{17:.5,18:.001},312:{18:.00101},320:{18:1,10:1,11:1},
        328:{0:8000.05},336:{0:8000.2},344:{12:1},352:{12:0,13:1},360:{13:0}}
    flags={0:3,36:7,60:3,62:7,92:6,100:7,130:5,140:7,170:3,180:7,260:0,264:7,368:7}
    return changes,flags,{384,576}


def compare(output,lib,pcreate,prender,rcreate,rrender,frames,signal,label):
    native=lib.buses_create(frames)
    if not native: raise RuntimeError("Native buses unavailable")
    pad, piano=pcreate(),rcreate()
    actual=np.zeros((11,signal.shape[1])); expected=np.zeros_like(actual)
    states=[]; wanted_states=[]; configs=[]
    config=DEFAULT.copy(); flags=3
    changes,flag_changes,clears=fixture()
    try:
        for start in range(0,signal.shape[1],frames):
            canonical=start//512
            if start%512==0:
                for index,value in changes.get(canonical,{}).items(): config[index]=value
                flags=flag_changes.get(canonical,flags)
                if canonical in clears:
                    lib.buses_clear(native); pad._faust_pad_bus.clear(); piano._piano_room.clear()
            if not lib.buses_configure(native,config.ctypes.data_as(PD),len(config)): raise RuntimeError("Configuration rejected")
            for name,value in zip(CONFIG_NAMES,config): setattr(pad,name,float(value))
            pad.filter_slope=24 if config[4] else 12
            piano.piano_room_enabled=bool(config[19]); piano.reverb_dry_wet=config[20]
            piano._piano_room.set_zone("size",config[21]); piano._piano_room.set_zone("damp",config[22])
            chunk=np.ascontiguousarray(signal[:,start:start+frames])
            inputs=(PD*7)(*[c.ctypes.data_as(PD) for c in chunk])
            if not lib.buses_process(native,inputs,flags): raise RuntimeError(f"Render failed at {label}:{frames}:{start}")
            for c in range(11): actual[c,start:start+frames]=np.ctypeslib.as_array(lib.buses_stem(native,c),shape=(frames,))
            state=np.zeros(9); lib.buses_state(native,state.ctypes.data_as(PD)); states.append(state)
            expected[:9,start:start+frames],pstate=prender(pad,chunk,flags)
            expected[9:,start:start+frames]=rrender(piano,chunk[5],chunk[6],frames)
            wanted_states.append(np.append(pstate,piano._piano_room_wet_cur))
            configs.append([start,flags,*config.tolist()])
        lib.buses_stop(native)
        if any(np.any(np.ctypeslib.as_array(lib.buses_stem(native,c),shape=(frames,))) for c in range(11)):
            raise RuntimeError("Stop failed to silence all internal buses")
    finally: lib.buses_delete(native)
    states=np.asarray(states); wanted_states=np.asarray(wanted_states)
    for suffix,value in (("native",actual),("reference",expected),("state-native",states),("state-reference",wanted_states)):
        np.save(output/f"{label}-{frames}-{suffix}.npy",value,allow_pickle=False)
    (output/f"{label}-{frames}-fixture.json").write_text(json.dumps({"config_names":CONFIG_NAMES,
        "blocks_start_flags_config":configs,"clear_canonical_512":sorted(clears)},indent=2)+"\n")
    if not all(np.isfinite(v).all() for v in (actual,expected,states,wanted_states)): raise RuntimeError("Nonfinite result")
    deltas=np.max(np.abs(actual-expected),axis=1); state_deltas=np.max(np.abs(states-wanted_states),axis=0)
    if np.any(deltas>TOLERANCE) or np.any(state_deltas>1e-8):
        raise RuntimeError(f"Mismatch {label}/{frames}: audio={deltas.tolist()}, state={state_deltas.tolist()}")
    # All sources end at512canonical blocks, clear576 then require exact silence.
    if np.any(actual[:,576*512:]): raise RuntimeError("Post-clear tail was not flushed")
    return {"fixture":label,"frames":frames,"samples_per_channel":signal.shape[1],
            "max_absolute_delta_by_channel":deltas.tolist(),"state_delta":state_deltas.tolist(),
            "tolerance":TOLERANCE,"preclear_tail_peak":float(np.max(np.abs(actual[:,512*512:576*512]))),
            "post_clear_exact_silence":True,"terminal_stop_exact_silence":True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir",type=Path)
    parser.add_argument("--source-dir",type=Path,help="Existing source comparator directory, both native cadence .npy files")
    args=parser.parse_args()
    if args.output_dir:
        output=args.output_dir.expanduser().resolve(); output.mkdir(mode=0o700,exist_ok=False)
    else: output=Path(tempfile.mkdtemp(prefix="stave-native-buses-"))
    print(f"Evidence: {output}",flush=True)
    paths=[ROOT/p for p in ("native_v2/include/stave/pad_bus.hpp","native_v2/include/stave/stage_buses.hpp",
        "native_v2/include/stave/piano_room.hpp","native_v2/src/pad_bus.cpp","native_v2/src/piano_room.cpp",
        "native_v2/tests/stage_buses_probe.cpp","native_v2/tests/test_stage_buses.cpp",
        "native_v2/tests/bus_fixture.hpp",
        "tools/compare_native_buses.py","tools/compare_native_room.py","tools/compare_native_v2.py","faust/faust_cprelude.h")]
    source_paths={}
    if args.source_dir:
        source_paths={n:args.source_dir.expanduser().resolve()/f"sources-native-{n}.npy" for n in (512,256)}
        paths.extend(source_paths.values())
    def hashes(): return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report={"status":"started","reference":base.REFERENCE,"host":platform.platform(),"checks":[],
        "scope":"pad/main filter and room, global LFO/drift/wobble/ping-pong off, no master/reverb/driver"}
    try:
        report["source_sha256"]=hashes()
        refs,native,guard,compiler,faust=build(output); report.update(compiler=compiler,faust=faust)
        report["guard_stdout"]=base.run([str(guard)],timeout=60).stdout
        print(report["guard_stdout"],flush=True)
        pcreate,prender,phashes=pad_oracle(refs[0]); rcreate,rrender,rhashes=room.oracle(refs[1])
        report["oracle_sha256"]={"pad":phashes,"room":rhashes}; lib=native_api(native)
        t=np.arange(640*512)/48000
        signal=np.stack([.1*np.sin(2*np.pi*f*t)+.02*np.sin(2*np.pi*f*3*t) for f in (130.8,164.8,196,261.6,523.2,329.6,392)])
        signal*=np.exp(-3*(t%.8)); signal[:,512*512:]=0
        signal[:,0]=np.array([.2,-.1,.3,-.2,.5,.25,-.1])
        np.save(output/"synthetic-input.npy",signal,allow_pickle=False)
        for frames in (512,256): report["checks"].append(compare(output,lib,pcreate,prender,rcreate,rrender,frames,signal,"synthetic"))
        for frames,path in source_paths.items():
            stems=np.load(path,allow_pickle=False)
            if stems.ndim!=2 or stems.shape[0]!=7 or not np.isfinite(stems).all(): raise RuntimeError("Expected seven finite source stems")
            signal=np.zeros((7,640*512)); n=min(stems.shape[1],512*512); signal[:,:n]=stems[:,:n]
            report["checks"].append(compare(output,lib,pcreate,prender,rcreate,rrender,frames,signal,"source-stems"))
        if hashes()!=report["source_sha256"]: raise RuntimeError("Source changed during comparison")
        report["status"]="passed_downstream_slice_only"
    except Exception as error: report["status"]="failed"; report["error"]=str(error)
    report["commands"]=base.COMMANDS
    report["artifact_sha256"]={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir()) if p.is_file()}
    (output/"report.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k in ("status","error","checks")},indent=2))
    return 0 if report["status"]=="passed_downstream_slice_only" else 1


if __name__=="__main__": raise SystemExit(main())
