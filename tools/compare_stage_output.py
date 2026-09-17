#!/usr/bin/env python3
"""Offline PCM/recorder/master-fader/BTL comparison to pinned bridge math.

Extracts only the successful-block gain loop; no JACK, ring, app or devices.
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
import numpy as np
import compare_native_v2 as base

PD=ct.POINTER(ct.c_double); PF=ct.POINTER(ct.c_float)

def build(output):
    cc,cxx=shutil.which("cc"),shutil.which("c++")
    if not cc or not cxx: raise RuntimeError("Existing C/C++ compilers required")
    source=base.source_from_reference("stave_synth/jack_bridge.c")
    start="        float alpha = 1.0f - 0.99584f;"; end="        stat_peak = peak;"
    if source.count(start)!=1 or source.count(end)!=1: raise RuntimeError("Pinned output boundary changed")
    loop=source[source.index(start):source.index(end)+len(end)]
    c=output/"reference_output.c"
    c.write_text("#include <math.h>\n#include <stdint.h>\ntypedef uint32_t jack_nframes_t;\n"
        "void reference_output(float* state,float* meter,const float* src_l,const float* src_r,float* out_l,float* out_r,uint32_t nframes,float vol_target,int btl_mode) {\n"
        "float master_volume_smooth=*state,stat_peak=0;\n"+loop+"\n*state=master_volume_smooth; *meter=stat_peak;\n}\n")
    reference=output/"reference_output.so"
    base.run([cc,"-std=c11","-O2","-ffp-contract=off","-shared","-fPIC",str(c),"-o",str(reference)])
    candidate,guard=output/"stage_output.so",output/"test_stage_output"
    for dest,test,extra in ((candidate,"stage_output_probe.cpp",["-shared"]),(guard,"test_stage_output.cpp",["-fsanitize=undefined","-fno-sanitize-recover=all"])):
        base.run([cxx,"-std=c++17","-O2","-ffp-contract=off","-Wall","-Wextra","-Werror","-fPIC",*extra,
            "-I",str(base.ROOT/"native_v2/include"),str(base.ROOT/"native_v2/tests"/test),"-o",str(dest)])
    return reference,candidate,guard,hashlib.sha256(source.encode()).hexdigest()

class Reference:
    def __init__(self,path):
        self.lib=ct.CDLL(str(path)); self.state=ct.c_float(.85); self.peak=ct.c_float(0)
        self.lib.reference_output.argtypes=[PF,PF,PF,PF,PF,PF,ct.c_uint,ct.c_float,ct.c_int]
        self.lib.reference_output.restype=None
    def process(self,signal,volume,btl):
        tap=np.ascontiguousarray(signal,dtype=np.float32); output=np.empty_like(tap); ptr=lambda a:a.ctypes.data_as(PF)
        self.lib.reference_output(ct.byref(self.state),ct.byref(self.peak),ptr(tap[0]),ptr(tap[1]),ptr(output[0]),ptr(output[1]),tap.shape[1],volume,btl)
        return output,tap

def api(path):
    lib=ct.CDLL(str(path))
    for name,args,result in (("create",[ct.c_uint],ct.c_void_p),("delete",[ct.c_void_p],None),
        ("configure",[ct.c_void_p,ct.c_double,ct.c_int],ct.c_int),("process",[ct.c_void_p,PD,PD],ct.c_int),
        ("channel",[ct.c_void_p,ct.c_uint],PF),("tap",[ct.c_void_p,ct.c_uint],PF),
        ("volume",[ct.c_void_p],ct.c_float),("peak",[ct.c_void_p],ct.c_float),("stop",[ct.c_void_p],None)):
        f=getattr(lib,"output_"+name); f.argtypes=args; f.restype=result
    return lib

def compare(lib,reference,size):
    ref=Reference(reference); handle=lib.output_create(size); max_error=0.
    if not handle: raise RuntimeError("Output construction failed")
    try:
        for block in range(1000):
            t=(block*size+np.arange(size))/48000
            signal=np.array([4*np.sin(2*np.pi*hz*t) for hz in (220,329.63)])
            if block%19==0: signal.fill(0)
            volume=(block%101)/100; btl=int(block%7<3)
            expected,tap=ref.process(signal,volume,btl)
            if not lib.output_configure(handle,volume,btl) or not lib.output_process(handle,signal[0].ctypes.data_as(PD),signal[1].ctypes.data_as(PD)):
                raise RuntimeError("Valid output call refused")
            for c in (0,1):
                actual=np.ctypeslib.as_array(lib.output_channel(handle,c),shape=(size,))
                actual_tap=np.ctypeslib.as_array(lib.output_tap(handle,c),shape=(size,))
                max_error=max(max_error,float(np.max(np.abs(actual-expected[c]))))
                if not np.array_equal(actual,expected[c]) or not np.array_equal(actual_tap,tap[c]): raise RuntimeError("Output/tap not exact")
            if lib.output_volume(handle)!=ref.state.value or lib.output_peak(handle)!=ref.peak.value: raise RuntimeError("Output state not exact")
        lib.output_stop(handle)
        if lib.output_process(handle,signal[0].ctypes.data_as(PD),signal[1].ctypes.data_as(PD)): raise RuntimeError("STOP resumed")
        if any(np.any(np.ctypeslib.as_array(lib.output_channel(handle,c),shape=(size,))) for c in (0,1)): raise RuntimeError("STOP not silent")
        return {"block_frames":size,"blocks":1000,"peak_difference":max_error,"pcm_tap_volume_peak_exact":True,"terminal_stop":True}
    finally: lib.output_delete(handle)

def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--output-dir",type=Path); args=parser.parse_args()
    if args.output_dir: output=args.output_dir.expanduser().resolve(); output.mkdir(mode=0o700,exist_ok=False)
    else: output=Path(tempfile.mkdtemp(prefix="stave-output-"))
    print(f"Evidence: {output}",flush=True)
    paths=[Path(__file__),Path(base.__file__),*[base.ROOT/p for p in ("native_v2/include/stave/stage_output.hpp","native_v2/tests/stage_output_probe.cpp","native_v2/tests/test_stage_output.cpp")]]
    hashes=lambda:{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report={"status":"started","host":platform.platform(),"reference":base.REFERENCE,"runs":[]}
    try:
        report["source_sha256"]=hashes(); reference,candidate,guard,report["oracle_sha256"]=build(output)
        report["guards"]=base.run([str(guard)],timeout=60).stdout
        lib=api(candidate)
        report["runs"]=[compare(lib,reference,size) for size in (512,256)]
        if hashes()!=report["source_sha256"]: raise RuntimeError("Sources changed during comparison")
        report["status"]="passed_output_component_only"
    except Exception as error: report.update(status="failed",error=str(error))
    report["commands"]=base.COMMANDS
    report["artifact_sha256"]={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()}
    (output/"report.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k in ("status","error","runs","guards")},indent=2))
    return 0 if report["status"].startswith("passed") else 1

if __name__=="__main__": raise SystemExit(main())
