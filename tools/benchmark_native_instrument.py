#!/usr/bin/env python3
"""Serial offline build/throughput probe; never opens devices or changes services.

Explicit SF2 and NEW output directory. No dependency installation. Unpaced,
normal-priority renders measure this incomplete graph, not live latency or xruns.
On Pi, temperature/throttle guards stop owned work, never the working synth.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import signal
import shutil
import subprocess
import time

ROOT=Path(__file__).resolve().parents[1]
MODULES=(("osc_bank","StaveOscBank",False),("piano_chain","StavePianoChain",True),
         ("pad_bus","StavePadBus",True),("piano_room","StavePianoRoom",False),
         ("ping_pong","StavePingPong",False),("reverb","StaveReverb",False),
         ("plate","StavePlate",False),("drone","StaveDrone",False),
         ("master_fx","StaveMasterFX",False),("bus_comp","StaveBusComp",False))
SOURCES=("stage_sources","piano_chain","pad_bus","piano_room","shared_effects","stage_master")

def temperature():
    p=Path("/sys/class/thermal/thermal_zone0/temp")
    return float(p.read_text())/1000 if p.exists() else None

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--soundfont",type=Path,required=True)
    parser.add_argument("--output-dir",type=Path,required=True)
    parser.add_argument("--blocks",type=int,default=400)
    parser.add_argument("--source-commit",required=True)
    parser.add_argument("--reuse-dsp-dir",type=Path,help="Prior private report directory; reuse only hash/version-matched DSP objects")
    parser.add_argument("--build-audition",action="store_true",help="Also build/test isolated audition adapter and compile JACK host; never start it")
    args=parser.parse_args()
    if not args.soundfont.is_file() or not 100<=args.blocks<=2000: parser.error("Existing SF2 and100..2000 blocks required")
    out=args.output_dir.resolve(); out.mkdir(mode=0o700,exist_ok=False)
    report={"status":"started","source_commit_label":args.source_commit,"host":platform.platform(),
            "machine":platform.machine(),"pid":os.getpid(),"runs":[],"commands":[],
            "scope":"unpaced offline incomplete graph; no driver/devices; not live qualification"}
    def interrupted(signum,frame):
        raise RuntimeError(f"Interrupted by signal {signum}")
    prior_handlers={s:signal.signal(s,interrupted) for s in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP)}
    def terminate(child):
        try: os.killpg(child.pid,signal.SIGTERM)
        except ProcessLookupError: pass
        try: child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try: os.killpg(child.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            child.wait()
    def run(cmd,timeout=180):
        start=time.monotonic()
        # Poll temperature during owned compiler/probe processes; kill only
        # this child if guard trips. Output goes to one private per-command log.
        log=out/f"command-{len(report['commands']):03}.log"
        with log.open("x") as stream:
            child=subprocess.Popen([str(x) for x in cmd],stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
            reason=None
            try:
                while child.poll() is None:
                    t=temperature()
                    if (t is not None and t>=80) or time.monotonic()-start>timeout:
                        reason="temperature_guard_80C" if t is not None and t>=80 else "timeout"
                        terminate(child); break
                    time.sleep(.25)
            finally:
                if child.poll() is None: terminate(child)
        text=log.read_text()
        report["commands"].append({"argv":[str(x) for x in cmd],"exit":child.returncode,"seconds":time.monotonic()-start,"log":log.name,"stop_reason":reason})
        if reason or child.returncode: raise RuntimeError(f"{reason or 'Command failed'}: {cmd[0]}: {text[-2000:]}")
        return text
    try:
        paths=[*sorted((ROOT/"native_v2/include").rglob("*.hpp")),*[ROOT/f"native_v2/src/{x}.cpp" for x in SOURCES],
               ROOT/"native_v2/tests/benchmark_stage_instrument.cpp",ROOT/"native_v2/tests/test_stage_instrument.cpp",
               ROOT/"native_v2/tests/test_piano_chain.cpp",
               Path(__file__),ROOT/"faust/faust_cprelude.h",*[ROOT/f"faust/{x}.dsp" for x,_,_ in MODULES]]
        if args.build_audition:
            paths += [ROOT/"native_v2/src/audition_jack.cpp",ROOT/"native_v2/tests/test_audition_session.cpp",ROOT/"native_v2/tests/test_audition_jack.cpp"]
        hashes=lambda:{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        report["source_sha256"]=hashes(); report["soundfont_sha256"]=hashlib.sha256(args.soundfont.read_bytes()).hexdigest()
        cc,cxx,faust,pkg=(shutil.which(x) for x in ("cc","c++","faust","pkg-config"))
        if not all((cc,cxx,faust,pkg)): raise RuntimeError("Existing compiler/Faust/pkg-config required")
        report["compiler"]=run([cxx,"--version"]); report["faust"]=run([faust,"--version"])
        report["fluid"]=run([pkg,"--modversion","fluidsynth"])
        fluid=shlex.split(run([pkg,"--cflags","--libs","fluidsynth"]))
        report["temperature_before"]=temperature()
        if shutil.which("vcgencmd"): report["throttle_before"]=run(["vcgencmd","get_throttled"])
        include=Path(faust).resolve().parent.parent/"include"
        flags=["-O2","-ffp-contract=off","-I",str(include)]
        previous=None
        if args.reuse_dsp_dir:
            previous=json.loads((args.reuse_dsp_dir/"report.json").read_text())
            for key in ("compiler","faust","fluid"):
                if previous[key]!=report[key]: raise RuntimeError("DSP reuse toolchain mismatch: "+key)
            for name in ["faust/faust_cprelude.h",*[f"faust/{n}.dsp" for n,_,_ in MODULES]]:
                if previous["source_sha256"][name]!=report["source_sha256"][name]: raise RuntimeError("DSP reuse source mismatch: "+name)
            report["reused_dsp_report_sha256"]=hashlib.sha256((args.reuse_dsp_dir/"report.json").read_bytes()).hexdigest()
        objects=[]
        for name,cls,double in MODULES:
            print("Building",name,flush=True)
            source=(ROOT/f"faust/{name}.dsp").read_text()
            if name=="osc_bank":
                if source.splitlines().count("NVOICES = 24;")!=1: raise RuntimeError("Unexpected oscillator topology")
                source=source.replace("NVOICES = 24;","NVOICES = 12;",1)
            dsp,c,obj=(out/f"{name}.{s}" for s in ("dsp","c","o"))
            if previous is not None:
                if (args.reuse_dsp_dir/dsp.name).read_text()!=source: raise RuntimeError("DSP reuse specialization mismatch")
                for path in (dsp,c,obj):
                    old=args.reuse_dsp_dir/path.name
                    if old.is_symlink() or hashlib.sha256(old.read_bytes()).hexdigest()!=previous["artifact_sha256"][old.name]:
                        raise RuntimeError("DSP reuse artifact mismatch: "+old.name)
                    shutil.copy2(old,path)
                commands=[v["argv"] for v in previous["commands"] if v["exit"]==0 and str(args.reuse_dsp_dir/obj.name) in v["argv"]]
                if len(commands)!=1 or "-O2" not in commands[0] or "-ffp-contract=off" not in commands[0] or "-ffast-math" in commands[0]:
                    raise RuntimeError("DSP reuse compile evidence mismatch")
            else:
                dsp.write_text(source)
                run([faust,*(["-double"] if double else []),"-lang","c","-cn",cls,"-o",c,dsp])
                run([cc,"-std=c11",*flags,*(["-DFAUSTFLOAT=double"] if double else []),"-include",ROOT/"faust/faust_cprelude.h","-c",c,"-o",obj])
            objects.append(obj)
        common=[cxx,"-std=c++17",*flags,"-Wall","-Wextra","-Werror","-I",ROOT/"native_v2/include",
                *[ROOT/f"native_v2/src/{x}.cpp" for x in SOURCES]]
        guard=out/"guard"; bench=out/"benchmark"
        run([*common,"-fsanitize=undefined","-fno-sanitize-recover=all",ROOT/"native_v2/tests/test_stage_instrument.cpp",*objects,*fluid,"-o",guard])
        report["guards"]=run([guard,args.soundfont],timeout=120)
        piano_guard=out/"piano-guard"
        run([*common,"-fsanitize=undefined","-fno-sanitize-recover=all",ROOT/"native_v2/tests/test_piano_chain.cpp",*objects,*fluid,"-o",piano_guard])
        report["piano_guards"]=run([piano_guard],timeout=120)
        if args.build_audition:
            audition_guard=out/"audition-guard"
            run([*common,"-pthread","-fsanitize=undefined","-fno-sanitize-recover=all",ROOT/"native_v2/tests/test_audition_session.cpp",*objects,*fluid,"-o",audition_guard])
            report["audition_guards"]=run([audition_guard,args.soundfont],timeout=120)
            jack=shlex.split(run([pkg,"--cflags","--libs","jack"]))
            jack_headers=shlex.split(run([pkg,"--cflags","jack"]))
            jack_guard=out/"audition-jack-guard"
            run([*common,"-pthread","-fsanitize=undefined","-fno-sanitize-recover=all",ROOT/"native_v2/tests/test_audition_jack.cpp",*objects,*fluid,*jack_headers,"-o",jack_guard])
            report["audition_jack_guards"]=run([jack_guard,args.soundfont],timeout=120)
            run([*common,"-pthread",ROOT/"native_v2/src/audition_jack.cpp",*objects,*fluid,*jack,"-o",out/"audition-jack"])
        run([*common,ROOT/"native_v2/tests/benchmark_stage_instrument.cpp",*objects,*fluid,"-o",bench])
        for size in (512,256):
            for scenario in range(4):
                before=temperature(); value=json.loads(run([bench,args.soundfont,size,scenario,args.blocks],timeout=90))
                value.update(temperature_before=before,temperature_after=temperature())
                report["runs"].append(value); print(json.dumps(value),flush=True)
        if hashes()!=report["source_sha256"]: raise RuntimeError("Source changed during run")
        report["status"]="completed_offline_probe_not_live_qualification"
    except Exception as error: report.update(status="failed_or_guard_stopped",error=str(error))
    report["temperature_after"]=temperature()
    if shutil.which("vcgencmd"):
        report["throttle_after"]=subprocess.run(["vcgencmd","get_throttled"],capture_output=True,text=True,timeout=5).stdout.strip()
    report["artifact_sha256"]={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file()}
    (out/"report.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({k:report[k] for k in ("status","error") if k in report}),flush=True)
    for s,handler in prior_handlers.items(): signal.signal(s,handler)
    return 0 if report["status"].startswith("completed") else 1

if __name__=="__main__": raise SystemExit(main())
