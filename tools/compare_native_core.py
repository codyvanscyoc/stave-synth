#!/usr/bin/env python3
"""Offline composed core vs pinned controls/voices/piano/filter/room reference.

Fixed3-unison,48k,256/512, explicit SF2. No shared FX/master/driver/UI.
Reuses original source oracle and actual fallback biquads during all-muted
blocks. Never imports the application, opens devices or contacts the Pi.
"""
from __future__ import annotations
import argparse
import ctypes as ct
import hashlib
import json
from pathlib import Path
import platform
import shutil
import tempfile
from types import SimpleNamespace
import numpy as np
import compare_native_v2 as base
import compare_native_sources as sources
import compare_native_buses as buses
import compare_native_room as room
import compare_source_mix as mix


class Integration:
    def __init__(self,lib,pcreate,prender,rcreate,rrender,advance):
        self.lib=lib; self.pad=pcreate(); self.piano=rcreate()
        self.prender=prender; self.rrender=rrender; self.advance=advance
        self.mixer=SimpleNamespace(sample_rate=48000,_osc1_blend_cur=.6,_osc2_blend_cur=.4)
        self.controls=buses.DEFAULT.copy(); self.changes,_,_=buses.fixture()
        self.muted=0; self.prepared=None
    def fixture(self):
        initial,patches,events,weights=sources.fixture()
        for tick,changes in {52:{15:0,16:0,22:0},76:{15:.7,16:.3,22:1},
                             116:{15:0,16:0,22:0},144:{15:.3,16:.6,22:1},
                             240:{15:0,16:0,22:0},280:{15:.6,16:.4,22:1}}.items():
            patches.setdefault(tick,{}).update(changes)
        return initial,patches,events,weights
    def prepare(self,handle,bank,values,frame,frames):
        tick=frame//512
        if frame%512==0:
            for i,v in self.changes.get(tick,{}).items(): self.controls[i]=v
        # One coherent shimmer source decision; independent bus wet/HP/cloud.
        self.controls[15:17]=values[22:24]
        hard=120<=tick<160
        if not self.lib.core_buses(handle,self.controls.ctypes.data_as(sources.PD),23,int(hard)):
            raise RuntimeError("Core bus configuration rejected")
        for name,value in zip(buses.CONFIG_NAMES,self.controls): setattr(self.pad,name,float(value))
        self.pad.filter_slope=24 if self.controls[4] else 12
        self.piano.piano_room_enabled=bool(self.controls[19]); self.piano.reverb_dry_wet=self.controls[20]
        self.piano._piano_room.set_zone("size",self.controls[21]); self.piano._piano_room.set_zone("damp",self.controls[22])
        for name,value in zip(("osc1_blend","osc2_blend","osc1_pan","osc2_pan","osc_hard_pan","shimmer_enabled",
                               "shimmer_high","shimmer_mix"),(*values[15:17],*values[19:21],hard,*self.controls[15:18])):
            setattr(self.mixer,name,value)
        self.prepared=self.advance(self.mixer,frames)
        p=self.prepared; skip=bool(p[9]); self.muted+=skip
        if not skip:
            waveforms=("sine","square","saw","triangle","saturated")
            bank.set_osc_params(waveforms[int(values[11])],waveforms[int(values[12])],p[2],p[3],
                                int(values[13]),int(values[14]),*values[17:19],p[4],p[5])
            bank.set_shimmer_params(bool(p[8]),bool(p[11]))
        return skip
    def process(self,signal,voices):
        p=self.prepared; flags=int(bool(p[7]))|int(voices>0)*2|int(bool(p[10]))*4
        output,_=self.prender(self.pad,signal,flags,not p[9])
        piano=self.rrender(self.piano,signal[5],signal[6],signal.shape[1])
        if p[9] and np.any(output): raise RuntimeError("Pinned fixed3-unison silent fallback produced audio")
        return np.vstack((output,piano))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--soundfont",required=True,type=Path)
    parser.add_argument("--fluidsynth-prefix",type=Path,default=Path("/opt/homebrew/opt/fluid-synth"))
    parser.add_argument("--output-dir",type=Path)
    args=parser.parse_args(); font=args.soundfont.expanduser().resolve()
    if not font.is_file(): parser.error("Explicit existing SF2 required")
    if args.output_dir:
        output=args.output_dir.expanduser().resolve(); output.mkdir(mode=0o700,exist_ok=False)
    else: output=Path(tempfile.mkdtemp(prefix="stave-native-core-"))
    print(f"Evidence: {output}",flush=True)
    paths=[*sorted((base.ROOT/"native_v2").rglob("*.hpp")),*sorted((base.ROOT/"native_v2").rglob("*.cpp")),
           *[Path(m.__file__) for m in (base,sources,buses,room,mix)],Path(__file__),base.ROOT/"faust/faust_cprelude.h",font]
    def hashes(): return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report={"status":"started","reference":base.REFERENCE,"host":platform.platform(),"runs":[],
            "scope":"source-fader/notes/voices/Fluid/piano/filter/room core; no global modulation/shared FX/master/driver"}
    try:
        report["source_sha256"]=hashes()
        source_dir=output/"source-build"; source_dir.mkdir()
        _,bank,cc,compiler,source_guard,faust=sources.build(source_dir,args.fluidsynth_prefix)
        report.update(compiler=compiler,faust=faust)
        report["source_guards"]=base.run([str(source_guard),str(font)],timeout=60).stdout
        bus_dir=output/"bus-build"; bus_dir.mkdir()
        ref_buses,_,bus_guard,_,_=buses.build(bus_dir)
        report["bus_guards"]=base.run([str(bus_guard)],timeout=60).stdout
        cxx=shutil.which("c++"); faust_include=Path(shutil.which("faust")).resolve().parent.parent/"include"
        objects=[str(source_dir/f"{n}.o") for n in ("osc_bank","piano_chain")]+[str(bus_dir/f"{n}.o") for n in ("pad_bus","piano_room")]
        candidate,guard=output/"stage_core.so",output/"test_stage_core"
        for dest,test,extra in ((candidate,"stage_core_probe.cpp",["-shared"]),
                               (guard,"test_stage_core.cpp",["-fsanitize=undefined","-fno-sanitize-recover=all"])):
            base.run([cxx,"-std=c++17","-O2","-ffp-contract=off","-fPIC","-Wall","-Wextra","-Werror",*extra,
                "-I",str(faust_include),"-I",str(base.ROOT/"native_v2/include"),"-I",str(args.fluidsynth_prefix/"include"),
                *[str(base.ROOT/"native_v2/src"/f"{n}.cpp") for n in ("stage_sources","piano_chain","pad_bus","piano_room")],
                str(base.ROOT/"native_v2/tests"/test),*objects,"-L",str(args.fluidsynth_prefix/"lib"),
                "-Wl,-rpath,"+str(args.fluidsynth_prefix/"lib"),"-lfluidsynth","-o",str(dest)])
        report["core_guards"]=base.run([str(guard),str(font)],timeout=60).stdout
        print(report["core_guards"],flush=True)
        refs=sources.reference_components(source_dir,bank,cc)
        sample_player=refs[2](); filters=sample_player._faust_chain
        pcreate,prender,report["pad_oracle_sha256"]=buses.pad_oracle(ref_buses[0],(type(filters.lp[0][0]),type(filters.hp[0][0])))
        rcreate,rrender,report["room_oracle_sha256"]=room.oracle(ref_buses[1])
        advance,report["mix_oracle_sha256"]=mix.oracle()
        lib=sources.native_api(candidate)
        lib.core_buses.argtypes=[ct.c_void_p,sources.PD,ct.c_uint,ct.c_int]; lib.core_buses.restype=ct.c_int
        tape=np.random.RandomState(base.SEED).uniform(0,1,(512,4)).astype(np.float64)
        np.save(output/"phase-tape.npy",tape,allow_pickle=False)
        for size in (512,256):
            integration=Integration(lib,pcreate,prender,rcreate,rrender,advance)
            result=sources.compare(output,lib,refs,args.fluidsynth_prefix,font,size,tape,integration)
            if integration.muted==0: raise RuntimeError("Muted transition fixture not exercised")
            result["muted_blocks"]=integration.muted; report["runs"].append(result)
            print(f"PASS: composed core {size}, muted blocks {integration.muted}",flush=True)
        (output/"source-fixture.json").write_text(json.dumps(integration.fixture(),indent=2)+"\n")
        (output/"bus-fixture.json").write_text(json.dumps(buses.fixture()[:2],indent=2)+"\n")
        if hashes()!=report["source_sha256"]: raise RuntimeError("Sources/assets changed during comparison")
        report["status"]="passed_composed_core_only"
    except Exception as error: report.update(status="failed",error=str(error))
    report["commands"]=base.COMMANDS
    report["artifact_sha256"]={str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted(output.rglob("*")) if p.is_file()}
    (output/"report.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k in ("status","error","runs")},indent=2))
    return 0 if report["status"]=="passed_composed_core_only" else 1


if __name__=="__main__": raise SystemExit(main())
