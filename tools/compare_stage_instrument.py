#!/usr/bin/env python3
"""Owned offline sources -> room/sends -> pad/ambience -> master comparison.

Actual Faust/FluidSynth, explicit SF2, fixed48k/512/256. No app imports,
devices, network, production state or Pi contact. Still no global modulation,
independent beds/organ, driver or live command protocol. Output fader is included.
"""
from __future__ import annotations
import argparse
import ast
import ctypes as ct
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import tempfile
from types import SimpleNamespace
import numpy as np
import compare_native_v2 as base
import compare_native_sources as sources
import compare_native_core as core
import compare_native_buses as buses
import compare_native_room as room
import compare_source_mix as mix
import compare_shared_effects as effects
import compare_pad_ambience as ambience
import compare_stage_master as master
import compare_stage_output as output_stage
import compare_stage_splits as splits

PD=sources.PD
# Keep the original strict sound gate. Both independent and common-piano
# diagnostic runs finish so the report can distinguish a routing mismatch from
# propagation of tiny upstream differences. Collecting diagnostics is NOT
# permission to mark a failed independent sound comparison as passed.

def piano_routing_oracle():
    source=base.source_from_reference("stave_synth/jack_engine.py")
    tree=ast.parse(source)
    engine=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=="JackEngine")
    loop=next(n for n in engine.body if isinstance(n,ast.FunctionDef) and n.name=="_render_loop")
    def conditional(test):
        hits=[n for n in ast.walk(loop) if isinstance(n,ast.If) and ast.unparse(n.test)==test]
        if len(hits)!=1: raise RuntimeError("Piano routing boundary changed: "+test)
        return hits[0]
    filt=conditional("self.organ_filter_enabled")
    class PreparedConstants(ast.NodeTransformer):
        count=0
        def visit_ImportFrom(self,node):
            if node.level!=1 or node.module!="synth_engine" or [x.name for x in node.names]!=["_Q24_S1_RATIO","_Q24_S2_RATIO"]:
                raise RuntimeError("Unexpected piano-route import")
            self.count+=1; return ast.copy_location(ast.Pass(),node)
    transform=PreparedConstants(); filt=transform.visit(filt)
    if transform.count!=1: raise RuntimeError("Piano filter constants boundary changed")
    fn=ast.parse("def route(self,piano_pre):\n    pass\n").body[0]
    fn.body=ast.parse("bs=piano_pre.shape[1]").body+[filt,conditional("piano_pre.size")]+ast.parse("reverb_send_ext=None\ndelay_send_ext=None").body+[
        conditional("piano_pre is not None and self.piano_reverb_send > 0.001"),
        conditional("piano_pre is not None and self.piano_delay_send > 0.001")]+ast.parse("return piano_pre,reverb_send_ext,delay_send_ext").body
    env={"np":np,"_Q24_S1_RATIO":.5412/.707,"_Q24_S2_RATIO":1.3066/.707}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),"pinned-piano-routing","exec"),env)
    return env["route"],hashlib.sha256(source.encode()).hexdigest()

class Integration(core.Integration):
    channels=10
    def __init__(self,lib,pcreate,prender,rcreate,rrender,advance,classes,mcreate,filter_type,size,output_reference,common_piano=False,overload=False):
        super().__init__(lib,pcreate,prender,rcreate,rrender,advance)
        self.pad._synth_diagnostics=None; self.pad._dry_wet_cur=.75
        self.pad.reverb=classes["FaustReverb"]()
        self.replay_reverb=classes["FaustReverb"]()
        self.replay_peak_difference=0.
        reverb_process=self.pad.reverb.process
        def capture_reverb(x):
            self.reference_send=x.copy()
            y=reverb_process(x)
            self.reference_wet=y.copy()
            return y
        self.pad.reverb.process=capture_reverb
        self.pad._stereo_out=np.zeros((2,size)); self.pad._dry_bus_scratch=np.zeros((2,size)); self.pad._fx_bus_scratch=np.zeros((2,size))
        for name in ("reverb_filter_l","reverb_filter_r","reverb_filter2_l","reverb_filter2_r"):
            setattr(self.pad,name,filter_type(8000,.707,48000))
        self.delay=classes["DelayOwner"](); self.delay.sample_rate=48000
        self.delay._faust_ping_pong=classes["FaustPingPong"]()
        self.pad._process_ping_pong=self.delay._process_ping_pong
        self.render_mix=ambience.mix_oracle(); self.master=mcreate(size,False)
        self.route,_=piano_routing_oracle()
        self.router=SimpleNamespace(synth=self.pad,_send_scratch=np.empty((2,size)))
        for name in ("_organ_filter_l","_organ_filter_r","_organ_filter2_l","_organ_filter2_r"):
            setattr(self.router,name,filter_type(8000,.707,48000))
        self.d=effects.DEFAULT.copy(); self.m=master.DEFAULT.copy()
        self.dchanges,self.events=effects.fixtures(); self.mchanges=master.fixture()
        self.routing=np.array([.75,1,0,0,0,0,120,0,0],dtype=np.float64)
        self.soft_clipped=0; self.wet_filter_blocks=0; self.piano_filter_blocks=0
        self.send_blocks=[0,0]; self.peak_before_clip=0
        self.common_piano=common_piano; self.native_piano=None
        self.overload=overload
        self.error_metrics={}
        self.trace_metrics={}; self.trace_frame=0
        self.output_reference=output_stage.Reference(output_reference)
        self.output_independent=output_stage.Reference(output_reference)
        self.output_same_input_peak=0.; self.output_independent_peak=0.; self.volume=.85; self.btl=0
    def validate_audio(self,actual,expected):
        error=actual-expected
        peak=np.max(np.abs(error),axis=1); rms=np.sqrt(np.mean(error*error,axis=1))
        self.error_metrics={"peak_difference":peak.tolist(),"rms_difference":rms.tolist(),
            "peak_difference_frame":np.argmax(np.abs(error),axis=1).tolist(),
            "samples_exceeding_limit":np.count_nonzero(np.abs(error)>sources.TOLERANCE,axis=1).tolist(),
            "original_1e_6_gate_passed":bool(np.max(peak)<=sources.TOLERANCE),
            "mode":"common-prepared-piano-isolation" if self.common_piano else "independent-full-chain",
            "peak_limit":sources.TOLERANCE}
        # Hard safety, source/ownership checks still fail immediately. Sound
        # acceptance is decided across the complete matrix in main, never hidden.
        return bool(np.max(np.abs(actual[:2]))<=.98)
    def native_before_reference(self,handle,size):
        self.native_trace=np.array([np.ctypeslib.as_array(self.lib.instrument_trace(handle,c),shape=(size,)) for c in range(6)])
        self.native_pcm=np.array([np.ctypeslib.as_array(self.lib.instrument_pcm(handle,c),shape=(size,)) for c in (0,1)])
        native_tap=np.array([np.ctypeslib.as_array(self.lib.instrument_tap(handle,c),shape=(size,)) for c in (0,1)])
        native_master=np.array([np.ctypeslib.as_array(self.lib.sources_stem(handle,c),shape=(size,)) for c in (0,1)])
        expected,tap=self.output_reference.process(native_master,self.volume,self.btl)
        self.output_same_input_peak=max(self.output_same_input_peak,float(np.max(np.abs(expected-self.native_pcm))))
        if not np.array_equal(expected,self.native_pcm) or not np.array_equal(tap,native_tap): raise RuntimeError("Connected output/tap differs from original gain loop on identical input")
        if self.common_piano:
            self.native_piano=np.array([np.ctypeslib.as_array(self.lib.sources_stem(handle,c),shape=(size,)) for c in (8,9)])
    def fixture(self):
        initial,patches,events,weights=super().fixture()
        # Explicit piano bus overload fixture exercises the original soft knee
        # downstream of the real instrument, not a substituted sine source.
        if self.overload:
            patches.setdefault(12,{}).update({32:1,35:1,38:24})
            patches.setdefault(100,{}).update({32:.8,38:2})
        return initial,patches,events,weights
    def prepare(self,handle,bank,values,frame,frames):
        skip=super().prepare(handle,bank,values,frame,frames)
        tick=frame//512; ptr=lambda x:x.ctypes.data_as(PD)
        self.volume=((tick//40)%11)/10; self.btl=int(200<=tick<250)
        if not self.lib.instrument_output(handle,self.volume,self.btl): raise RuntimeError("Output configuration rejected")
        if frame%512==0:
            for i,v in self.dchanges.get(tick,{}).items(): self.d[i]=v
            for i,v in self.mchanges.get(tick,{}).items(): self.m[i]=v
            for action,value in self.events.get(tick,[]):
                if action==9: continue
                if not self.lib.instrument_reverb(handle,action,value): raise RuntimeError("Reverb command rejected")
                for reverb in (self.pad.reverb,self.replay_reverb):
                    if action<7: getattr(reverb,effects.SETTERS[action])(value)
                    elif action==7: reverb.set_type(effects.TYPES[value])
                    elif action==8: reverb.set_freeze(bool(value))
            if tick in (129,196,441):
                if not self.lib.instrument_reverb(handle,10,0): raise RuntimeError("BPM retrigger rejected")
                self.master._bpm_beat_phase=0.; self.master._bpm_pulse_remaining=2400
        # Both sends individually/off/together, including exact admission gate.
        self.routing[:6]=[(tick%3)/2 if tick>=75 else .75,1+(tick%4)/2,
            (0,.001,.00101,.3,.8)[(tick//25)%5],(0,.5,.001,.00101)[(tick//35)%4],
            1 if 45<=tick<205 or 240<=tick<560 else 0,1 if 105<=tick<230 or 400<=tick<510 else 0]
        self.routing[6:]=[97 if tick<400 else 143,0 if tick%64<16 else .7,np.sin(frame/48000*3)]
        if not self.lib.instrument_effects(handle,ptr(self.d),ptr(self.m),ptr(self.routing),len(self.routing)):
            raise RuntimeError("Instrument configuration rejected")
        for i,name in enumerate(effects.DELAY_NAMES):
            setattr(self.delay,name,effects.DIVISIONS[int(self.d[i])] if i in (3,4) else self.d[i])
        self.pad.reverb.dry_wet,self.pad.reverb.wet_gain=self.routing[:2]
        self.router.piano_reverb_send,self.router.piano_delay_send=self.routing[2:4]
        self.pad.reverb_filter_enabled=bool(self.routing[4]); self.router.organ_filter_enabled=bool(self.routing[5])
        self.wet_filter_blocks+=bool(self.routing[4]); self.piano_filter_blocks+=bool(self.routing[5])
        for i in range(2): self.send_blocks[i]+=int(self.routing[2+i]>.001)
        master.configure(self.master,self.m)
        self.master.synth.bpm,self.master.synth.lfo_depth,self.master.synth._lfo_mod_a_last=self.routing[6:]
        return skip
    def process(self,signal,voices):
        piano=self.rrender(self.piano,signal[5],signal[6],signal.shape[1])
        self.peak_before_clip=max(self.peak_before_clip,float(np.max(np.abs(piano))))
        # The route oracle owns original piano filtering, soft clipping and sends.
        piano,rev,delay=self.route(self.router,piano)
        self.soft_clipped+=int(np.count_nonzero(np.abs(piano)>.85))
        original_piano=piano
        if self.common_piano:
            # Diagnostic isolation ONLY: identical downstream piano input.
            # Keep the independent piano output in the reported comparison.
            piano=self.native_piano
            rev=piano*self.router.piano_reverb_send if self.router.piano_reverb_send>.001 else None
            delay=piano*self.router.piano_delay_send if self.router.piano_delay_send>.001 else None
        self.delay._external_delay_send=delay
        p=self.prepared; flags=int(bool(p[7]))|int(voices>0)*2|int(bool(p[10]))*4
        pad,state=self.prender(self.pad,signal,flags,not p[9])
        dry,fx=self.render_mix(self.pad,pad,state,rev)
        stages=np.vstack((pad[:2],self.reference_send,self.reference_wet))
        replay=self.replay_reverb.process(self.native_trace[2:4].copy())
        self.replay_peak_difference=max(self.replay_peak_difference,float(np.max(np.abs(replay-self.native_trace[4:6]))))
        for name,sl in (("delay",slice(0,2)),("reverb_input",slice(2,4)),("reverb_output",slice(4,6))):
            a=self.native_trace[sl]; b=stages[sl]; diff=np.abs(a-b)
            metric=self.trace_metrics.setdefault(name,{"peak":0.,"first_float32_difference":None})
            metric["peak"]=max(metric["peak"],float(np.max(diff)))
            coords=np.argwhere(a.astype(np.float32)!=b.astype(np.float32))
            if coords.size and metric["first_float32_difference"] is None:
                c,i=min(coords.tolist(),key=lambda x:x[1])
                metric["first_float32_difference"]={"frame":self.trace_frame+i,"channel":c,
                    "native":float(a[c,i]),"reference":float(b[c,i]),
                    "native_float":float(np.float32(a[c,i])),"reference_float":float(np.float32(b[c,i]))}
        self.trace_frame+=signal.shape[1]
        buses_out=np.vstack((self.pad._stereo_out*.85,dry,fx))
        stereo=self.master.process(buses_out,piano)
        pcm,_=self.output_independent.process(stereo,self.volume,self.btl)
        self.output_independent_peak=max(self.output_independent_peak,float(np.max(np.abs(pcm-self.native_pcm))))
        return np.vstack((stereo,buses_out,original_piano))

class SplitIntegration(Integration):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.split_weight,_=splits.oracle()
        self.split_events=0; self.partial_weights=0; self.silent_weights=0
    def fixture(self):
        initial,patches,events,weights=super().fixture()
        # These sounding raw keys cross different zones if wrongly evaluated
        # AFTER transpose. Piano octave is independent of the raw-key ranges.
        patches[60].update({8:12,9:1})
        patches[84]={8:-12,9:-1}
        events[96]=[(0,64,0)]  # velocity-zero note-on must remain a release
        return initial,patches,events,weights
    def prepare(self,handle,bank,values,frame,frames):
        skip=super().prepare(handle,bank,values,frame,frames)
        self.split_config=splits.configuration(frame//512)
        config=(ct.c_int*13)(*self.split_config)
        if not self.lib.instrument_splits(handle,config,13): raise RuntimeError("Split configuration rejected")
        return skip
    def key_event(self,handle,frame,kind,note,value,weights):
        c=self.split_config
        # Original Python function decides reference weights from RAW key;
        # candidate receives only the raw event and its independent config.
        weights[:]=[self.split_weight(note,*c[1+3*z:4+3*z]) if c[0] else 1. for z in range(4)]
        if kind==0 and value>0:
            self.split_events+=1
            self.partial_weights+=int(np.count_nonzero((weights>0)&(weights<1)))
            self.silent_weights+=int(np.count_nonzero(weights==0))
        return self.lib.instrument_key(handle,frame,kind,note,value)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--soundfont",required=True,type=Path)
    parser.add_argument("--fluidsynth-prefix",type=Path,default=Path("/opt/homebrew/opt/fluid-synth"))
    parser.add_argument("--output-dir",type=Path)
    args=parser.parse_args(); font=args.soundfont.expanduser().resolve()
    if not font.is_file(): parser.error("Explicit existing SF2 required")
    if args.output_dir: output=args.output_dir.expanduser().resolve(); output.mkdir(mode=0o700,exist_ok=False)
    else: output=Path(tempfile.mkdtemp(prefix="stave-instrument-"))
    print(f"Evidence: {output}",flush=True)
    modules=(base,sources,core,buses,room,mix,effects,ambience,master,output_stage,splits)
    paths=[*sorted((base.ROOT/"native_v2").rglob("*.hpp")),*sorted((base.ROOT/"native_v2").rglob("*.cpp")),
           *[Path(m.__file__) for m in modules],Path(__file__),base.ROOT/"faust/faust_cprelude.h",font]
    hashes=lambda:{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report={"status":"started","reference":base.REFERENCE,"host":platform.platform(),"runs":[],
            "peak_tolerance":sources.TOLERANCE,
            "dependencies":{name:importlib.metadata.version(name) for name in ("numpy","scipy","cffi")}}
    try:
        report["source_sha256"]=hashes()
        dirs={name:output/name for name in ("sources","buses","effects","master","output")}
        for d in dirs.values(): d.mkdir()
        _,bank,cc,compiler,sg,faust=sources.build(dirs["sources"],args.fluidsynth_prefix)
        report.update(compiler=compiler,faust=faust)
        br,_,bg,_,_=buses.build(dirs["buses"])
        er,_,eg,_,_=effects.build(dirs["effects"])
        mr,_,mg,_,_=master.build(dirs["master"])
        output_reference,_,og,report["output_oracle_sha256"]=output_stage.build(dirs["output"])
        report["component_guards"]={name:base.run([str(guard),*([str(font)] if name=="sources" else [])],timeout=60).stdout
                                    for name,guard in (("sources",sg),("buses",bg),("effects",eg),("master",mg),("output",og))}
        cxx=shutil.which("c++"); include=Path(shutil.which("faust")).resolve().parent.parent/"include"
        objects=[str(d/f"{name}.o") for key,names in (("sources",("osc_bank","piano_chain")),("buses",("pad_bus","piano_room")),
                 ("effects",tuple(x[0] for x in effects.MODULES)),("master",("master_fx","bus_comp"))) for d in (dirs[key],) for name in names]
        candidate,guard=output/"stage_instrument.so",output/"test_stage_instrument"
        for dest,test,extra in ((candidate,"stage_instrument_probe.cpp",["-shared"]),(guard,"test_stage_instrument.cpp",["-fsanitize=undefined","-fno-sanitize-recover=all"])):
            base.run([cxx,"-std=c++17","-O2","-ffp-contract=off","-fPIC","-Wall","-Wextra","-Werror",*extra,
                "-I",str(include),"-I",str(base.ROOT/"native_v2/include"),"-I",str(args.fluidsynth_prefix/"include"),
                *[str(base.ROOT/"native_v2/src"/f"{name}.cpp") for name in ("stage_sources","piano_chain","pad_bus","piano_room","shared_effects","stage_master")],
                str(base.ROOT/"native_v2/tests"/test),*objects,"-L",str(args.fluidsynth_prefix/"lib"),
                "-Wl,-rpath,"+str(args.fluidsynth_prefix/"lib"),"-lfluidsynth","-o",str(dest)])
        report["instrument_guards"]=base.run([str(guard),str(font)],timeout=60).stdout
        print(report["instrument_guards"],flush=True)
        refs=sources.reference_components(dirs["sources"],bank,cc)
        filters=refs[2]()._faust_chain
        pcreate,prender,report["pad_oracle_sha256"]=buses.pad_oracle(br[0],(type(filters.lp[0][0]),type(filters.hp[0][0])),with_delay=True)
        rcreate,rrender,report["room_oracle_sha256"]=room.oracle(br[1])
        advance,report["mix_oracle_sha256"]=mix.oracle()
        classes,report["effects_oracle_sha256"]=effects.oracles(er)
        _,mcreate,report["master_oracle_sha256"]=master.oracles(dirs["master"],mr)
        _,report["piano_routing_sha256"]=piano_routing_oracle()
        lib=sources.native_api(candidate)
        lib.instrument_splits.argtypes=[ct.c_void_p,ct.POINTER(ct.c_int),ct.c_uint]; lib.instrument_splits.restype=ct.c_int
        lib.instrument_key.argtypes=[ct.c_void_p,ct.c_uint64,ct.c_int,ct.c_int,ct.c_int]; lib.instrument_key.restype=ct.c_int
        lib.instrument_trace.argtypes=[ct.c_void_p,ct.c_uint]; lib.instrument_trace.restype=PD
        for name in ("instrument_pcm","instrument_tap"):
            getattr(lib,name).argtypes=[ct.c_void_p,ct.c_uint]; getattr(lib,name).restype=output_stage.PF
        for name,params in (("core_buses",[ct.c_void_p,PD,ct.c_uint,ct.c_int]),
                            ("instrument_effects",[ct.c_void_p,PD,PD,PD,ct.c_uint]),
                            ("instrument_reverb",[ct.c_void_p,ct.c_uint,ct.c_double]),
                            ("instrument_output",[ct.c_void_p,ct.c_double,ct.c_int])):
            getattr(lib,name).argtypes=params; getattr(lib,name).restype=ct.c_int
        tape=np.random.RandomState(base.SEED).uniform(0,1,(512,4)).astype(np.float64)
        np.save(output/"phase-tape.npy",tape,allow_pickle=False)
        source_fixtures={}
        for overload,common_piano,split_routing in ((False,False,False),(False,True,False),(True,False,False),(True,True,False),(False,False,True)):
            label=("raw-key-splits" if split_routing else "overload" if overload else "original-source-fixture")+("-common-piano" if common_piano else "-independent")
            run_dir=output/label; run_dir.mkdir()
            for size in (512,256):
                integration_type=SplitIntegration if split_routing else Integration
                integration=integration_type(lib,pcreate,prender,rcreate,rrender,advance,classes,mcreate,type(filters.lp[0][0]),size,output_reference,common_piano,overload)
                source_fixtures[label]=integration.fixture()
                result=sources.compare(run_dir,lib,refs,args.fluidsynth_prefix,font,size,tape,integration)
                result.update(muted_blocks=integration.muted,wet_filter_blocks=integration.wet_filter_blocks,
                    piano_filter_blocks=integration.piano_filter_blocks,piano_send_blocks=integration.send_blocks,
                    piano_samples_above_soft_knee=integration.soft_clipped,peak_piano_before_routing=integration.peak_before_clip,
                    error_metrics=integration.error_metrics,fixture=label,overload=overload,split_routing=split_routing,trace_metrics=integration.trace_metrics,
                    native_input_reference_reverb_replay_difference=integration.replay_peak_difference,
                    output_same_input_peak_difference=integration.output_same_input_peak,
                    output_independent_peak_difference=integration.output_independent_peak)
                if integration.replay_peak_difference!=0: raise RuntimeError("Reference reverb replay with native input differs")
                if split_routing:
                    result.update(split_note_ons=integration.split_events,partial_split_weights=integration.partial_weights,
                                  silent_split_weights=integration.silent_weights)
                    if not all((integration.split_events,integration.partial_weights,integration.silent_weights)):
                        raise RuntimeError("Split routing fixture coverage missing")
                if not all((integration.muted,integration.wet_filter_blocks,integration.piano_filter_blocks,*integration.send_blocks)) or (overload and not integration.soft_clipped):
                    raise RuntimeError("Required integration branch not exercised")
                report["runs"].append(result); print(f"MEASURED: {result}",flush=True)
        (output/"fixture.json").write_text(json.dumps({"source_variants":source_fixtures,"buses":buses.fixture()[:2],
            "effects":effects.fixtures(),"master":master.fixture(),"routing":"deterministic prepare() recipe in hashed runner"},indent=2)+"\n")
        if hashes()!=report["source_sha256"]: raise RuntimeError("Sources changed during comparison")
        report["independent_sound_gate_passed"]=all(r["error_metrics"]["original_1e_6_gate_passed"] for r in report["runs"] if r["error_metrics"]["mode"]=="independent-full-chain")
        report["original_source_sound_gate_passed"]=all(r["error_metrics"]["original_1e_6_gate_passed"] for r in report["runs"] if not r["overload"] and r["error_metrics"]["mode"]=="independent-full-chain")
        report["common_piano_diagnostic_passed"]=all(r["error_metrics"]["original_1e_6_gate_passed"] for r in report["runs"] if r["error_metrics"]["mode"]=="common-prepared-piano-isolation")
        report["split_routing_sound_gate_passed"]=all(r["error_metrics"]["original_1e_6_gate_passed"] for r in report["runs"] if r["split_routing"])
        report["status"]=("passed_owned_instrument_pcm_only" if all(r["error_metrics"]["original_1e_6_gate_passed"] for r in report["runs"])
                          else "sound_difference_review_required")
    except Exception as error: report.update(status="failed",error=str(error))
    report["commands"]=base.COMMANDS
    report["artifact_sha256"]={str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.rglob("*")) if p.is_file()}
    (output/"report.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k in ("status","error","runs")},indent=2))
    return 0 if report["status"].startswith("passed") else 1

if __name__=="__main__": raise SystemExit(main())
