#!/usr/bin/env python3
"""Device-free M2 source graph versus pinned v1.2 components, not live sound.

Builds only in a NEW private directory. Explicit existing SF2 required. Never
imports the application or opens audio/MIDI/network/control-state paths.
"""
from __future__ import annotations
import argparse
import ast
from collections import deque
from contextlib import nullcontext
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

ROOT = base.ROOT
TOLERANCE = 1e-6
PD = ct.POINTER(ct.c_double)


def build(output, prefix):
    cc, cxx, faust = (shutil.which(name) for name in ("cc", "c++", "faust"))
    if not all((cc, cxx, faust)): raise RuntimeError("Installed compilers/Faust required; nothing installed")
    faust_version = base.run([faust, "--version"]).stdout
    fp = Path(faust).resolve().parent.parent
    cflags = ["-O2", "-ffp-contract=off", "-fPIC", "-I", str(fp / "include")]
    objects = []
    for name, cls, double in (("osc_bank", "StaveOscBank", False), ("piano_chain", "StavePianoChain", True)):
        source = base.source_from_reference(f"faust/{name}.dsp")
        if name == "osc_bank":
            if source.splitlines().count("NVOICES = 24;") != 1: raise RuntimeError("Unexpected bank topology")
            source = source.replace("NVOICES = 24;", "NVOICES = 12;", 1)
        dsp, generated, obj = (output / f"{name}.{ext}" for ext in ("dsp", "c", "o"))
        dsp.write_text(source)
        base.run([faust, *(["-double"] if double else []), "-lang", "c", "-cn", cls, "-o", str(generated), str(dsp)])
        base.run([cc, "-std=c11", *cflags, *(["-DFAUSTFLOAT=double"] if double else []),
                  "-include", str(ROOT / "faust/faust_cprelude.h"), "-c", str(generated), "-o", str(obj)])
        objects.append(str(obj))
    reference_bank = output / "reference_osc.so"
    base.run([cc, "-shared", objects[0], "-o", str(reference_bank)])
    library = output / "stage_sources.so"
    base.run([cxx, "-std=c++17", *cflags, "-Wall", "-Wextra", "-Werror", "-shared",
              "-I", str(ROOT / "native_v2/include"), "-I", str(prefix / "include"),
              *[str(ROOT / p) for p in ("native_v2/src/piano_chain.cpp", "native_v2/src/stage_sources.cpp",
                                       "native_v2/tests/stage_sources_probe.cpp")], *objects,
              "-L", str(prefix / "lib"), "-Wl,-rpath," + str(prefix / "lib"), "-lfluidsynth", "-o", str(library)])
    guard = output / "test_stage_sources"
    base.run([cxx, "-std=c++17", *cflags, "-Wall", "-Wextra", "-Werror",
              "-fsanitize=undefined", "-fno-sanitize-recover=all",
              "-I", str(ROOT / "native_v2/include"), "-I", str(prefix / "include"),
              *[str(ROOT / p) for p in ("native_v2/src/piano_chain.cpp", "native_v2/src/stage_sources.cpp",
                                       "native_v2/tests/test_stage_sources.cpp")], *objects,
              "-L", str(prefix / "lib"), "-Wl,-rpath," + str(prefix / "lib"), "-lfluidsynth", "-o", str(guard)])
    return library, reference_bank, cc, base.run([cxx, "--version"]).stdout, guard, faust_version


def native_api(path):
    lib = ct.CDLL(str(path))
    for name, result, args in (
        ("create", ct.c_void_p, [ct.c_char_p, ct.c_uint, PD, ct.c_uint]),
        ("delete", None, [ct.c_void_p]), ("patch", ct.c_int, [ct.c_void_p, PD, ct.c_uint]),
        ("command", ct.c_int, [ct.c_void_p, ct.c_uint64, ct.c_int, ct.c_int, ct.c_int, PD]),
        ("render", ct.c_int, [ct.c_void_p]), ("stem", PD, [ct.c_void_p, ct.c_uint]),
        ("raw", ct.POINTER(ct.c_short), [ct.c_void_p]), ("stop", None, [ct.c_void_p]),
        ("voices", ct.c_uint, [ct.c_void_p]), ("frame", ct.c_uint64, [ct.c_void_p]),
        ("phases", ct.c_uint, [ct.c_void_p]), ("stats", ct.c_int, [ct.c_void_p, ct.POINTER(ct.c_uint64)])):
        fn = getattr(lib, "sources_" + name); fn.restype = result; fn.argtypes = args
    return lib


class Fluid:
    def __init__(self, prefix, font):
        path = prefix / "lib" / ("libfluidsynth.dylib" if platform.system() == "Darwin" else "libfluidsynth.so")
        self.lib = ct.CDLL(str(path)); self.settings = self.synth = None
        for name, result, args in (
            ("fluid_version_str", ct.c_char_p, []),
            ("new_fluid_settings", ct.c_void_p, []), ("delete_fluid_settings", None, [ct.c_void_p]),
            ("fluid_settings_setnum", ct.c_int, [ct.c_void_p, ct.c_char_p, ct.c_double]),
            ("fluid_settings_setint", ct.c_int, [ct.c_void_p, ct.c_char_p, ct.c_int]),
            ("new_fluid_synth", ct.c_void_p, [ct.c_void_p]), ("delete_fluid_synth", None, [ct.c_void_p]),
            ("fluid_synth_sfload", ct.c_int, [ct.c_void_p, ct.c_char_p, ct.c_int]),
            ("fluid_synth_program_select", ct.c_int, [ct.c_void_p, ct.c_int, ct.c_int, ct.c_int, ct.c_int]),
            ("fluid_synth_noteon", ct.c_int, [ct.c_void_p, ct.c_int, ct.c_int, ct.c_int]),
            ("fluid_synth_noteoff", ct.c_int, [ct.c_void_p, ct.c_int, ct.c_int]),
            ("fluid_synth_cc", ct.c_int, [ct.c_void_p, ct.c_int, ct.c_int, ct.c_int]),
            ("fluid_synth_pitch_bend", ct.c_int, [ct.c_void_p, ct.c_int, ct.c_int]),
            ("fluid_synth_write_s16", ct.c_int, [ct.c_void_p, ct.c_int, ct.c_void_p, ct.c_int, ct.c_int, ct.c_void_p, ct.c_int, ct.c_int])):
            fn = getattr(self.lib, name); fn.restype = result; fn.argtypes = args
        try:
            self.settings = self.lib.new_fluid_settings()
            if not self.settings: raise RuntimeError("Reference Fluid settings unavailable")
            for name, value in (("sample-rate", 48000), ("gain", 1)):
                if self.lib.fluid_settings_setnum(self.settings, f"synth.{name}".encode(), value) != 0:
                    raise RuntimeError("Reference Fluid numeric setting failed")
            for name, value in (("polyphony", 32), ("dynamic-sample-loading", 0), ("reverb.active", 0),
                                ("chorus.active", 0), ("cpu-cores", 1), ("lock-memory", 0)):
                if self.lib.fluid_settings_setint(self.settings, f"synth.{name}".encode(), value) != 0:
                    raise RuntimeError("Reference Fluid integer setting failed")
            self.synth = self.lib.new_fluid_synth(self.settings)
            if not self.synth: raise RuntimeError("Reference Fluid unavailable")
            sf = self.lib.fluid_synth_sfload(self.synth, str(font).encode(), 0)
            if sf < 0 or self.lib.fluid_synth_program_select(self.synth, 0, sf, 0, 0) != 0:
                raise RuntimeError("Reference piano asset unavailable")
        except Exception:
            self.close(); raise
    def close(self):
        if self.synth: self.lib.delete_fluid_synth(self.synth); self.synth = None
        if self.settings: self.lib.delete_fluid_settings(self.settings); self.settings = None
    def noteon(self, channel, note, velocity): return self.lib.fluid_synth_noteon(self.synth, channel, note, velocity)
    def noteoff(self, channel, note): return self.lib.fluid_synth_noteoff(self.synth, channel, note)
    def cc(self, channel, number, value): return self.lib.fluid_synth_cc(self.synth, channel, number, value)
    def pitch_bend(self, channel, offset): return self.lib.fluid_synth_pitch_bend(self.synth, channel, offset + 8192)
    def render(self, frames):
        out = np.empty(frames * 2, dtype=np.int16)
        if self.lib.fluid_synth_write_s16(self.synth, frames, out.ctypes.data, 0, 2, out.ctypes.data, 1, 2) != 0:
            raise RuntimeError("Reference piano render failed")
        return out


def reference_components(output, bank_path, cc):
    source = base.source_from_reference("stave_synth/faust_osc_bank.py")
    tree = ast.parse(source)
    definitions = [node.value.args[0].value for node in tree.body if isinstance(node, ast.Expr)
                   and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == "_ffi.cdef"]
    if len(definitions) != 1: raise RuntimeError("Pinned Faust ABI inventory mismatch")
    ffi = FFI(); ffi.cdef(definitions[0])
    waveforms = ("sine", "square", "saw", "triangle", "saturated")
    ns = base.declarations(source, ["FaustOscBank", "_install_ui_callbacks"],
                           {"np": np, "_ffi": ffi, "_lib": ffi.dlopen(str(bank_path)), "NVOICES": 12,
                            "SUPPORTED_UNISON": 3, "_WF_INDEX": {s:i for i,s in enumerate(waveforms)}})
    _, voices = base.voice_oracle_components()
    create_piano, piano_hashes = base.piano_oracle(output, cc)
    player = ast.parse(base.source_from_reference("stave_synth/fluidsynth_player.py"))
    cls = next(n for n in player.body if isinstance(n, ast.ClassDef) and n.name == "FluidSynthPlayer")
    methods = {n.name:n for n in cls.body if isinstance(n, ast.FunctionDef)}
    batch = methods["_apply_midi_batch_impl_locked"]
    event_loop = next(n for n in batch.body if isinstance(n, ast.For))
    branch = next(n for n in event_loop.body if isinstance(n, ast.If))
    if ast.unparse(branch.test) != "kind == 'note_on'": raise RuntimeError("Pinned note-on boundary mismatch")
    fn = ast.parse("def note_on(self, note, velocity):\n    pass\n").body[0]; fn.body = branch.body
    pns = {"logger": logging.getLogger("native-source-piano-oracle")}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn, methods["_release_all_native_locked"]], type_ignores=[])),
                 "pinned-source-piano-events", "exec"), pns)
    jack = ast.parse(base.source_from_reference("stave_synth/jack_engine.py"))
    cls = next(n for n in jack.body if isinstance(n, ast.ClassDef) and n.name == "JackEngine")
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in ("_midi_loop_body", "panic")]
    jns = {"ctypes": ct, "time": SimpleNamespace(sleep=lambda _: None), "_MIDI_POLL_S": .002,
           "SAMPLE_RATE": 48000, "logger": logging.getLogger("native-source-key-oracle"),
           "SynthEngine": SimpleNamespace(split_weight=lambda *args: 1.)}
    exec(compile(ast.Module(body=methods, type_ignores=[]), "pinned-source-key-owner", "exec"), jns)
    helpers = base.declarations(base.source_from_reference("tests/test_midi_notes.py"), ["_Bridge", "_Synth", "_Engine"],
                                {"_METHODS": jns, "deque": deque, "SimpleNamespace": SimpleNamespace})
    return ns["FaustOscBank"], voices, create_piano, pns, helpers["_Engine"], jns, piano_hashes


def fixture():
    # Times are canonical512-frame block numbers; both runs use same times.
    events = {4:[(0,n,90) for n in (48,52,55)], 20:[(2,0,1)], 24:[(1,n,0) for n in (48,52,55)],
              28:[(0,n,100) for n in (53,57,60)], 36:[(3,0,1)], 40:[(1,n,0) for n in (53,57,60)],
              44:[(2,0,0)], 52:[(3,0,0)], 62:[(0,60,45),(0,60,100)], 68:[(1,60,0)],
              76:[(0,n,90) for n in range(48,63)], 80:[(1,n,0) for n in range(48,63)],
              84:[(0,n,70) for n in (48,55,60,64)], 92:[(0,60,115)], 100:[(2,0,1)],
              104:[(1,n,0) for n in (48,55,60,64)], 112:[(0,60,100)], 116:[(1,60,0)],
              120:[(2,0,0)], 132:[(0,n,65) for n in (60,64,67)], 152:[(4,0,0)],
              168:[(0,n,110) for n in (36,43,48,55,60)], 192:[(4,0,0)]}
    patches = {0:{}, 16:{15:.4,16:.6,22:1}, 32:{24:1,25:1.7,26:.4}, 48:{28:1,29:2.3,30:.3,31:1},
               60:{11:2,12:3,32:.65,35:1,36:-24,38:2,39:3,40:.6}, 72:{0:0,1:100,2:60,3:150},
               88:{19:-.3,20:.3,21:1.25}, 96:{21:-.75,23:1,41:1,42:.7},
               108:{8:2,9:1}, 124:{8:0,9:0,21:0,43:5.5,44:.4,45:2},
               144:{32:.8,33:60,34:12000}, 160:{32:.5,15:.7,16:.3,11:4,12:0},
               180:{32:.1,15:.1,16:.1}, 208:{32:0}, 224:{32:.5}}
    initial = [200,1500,80,500,200,1500,80,500,0,0,10,0,1,0,0,.6,.4,.07,.85,0,0,0,0,0,
               0,1,0,0,0,1,0,0,.5,20,20000,0,-20,3,0,0,1,0,.5,0,0,1,0]
    weights = {0:[1,1,1,1], 62:[.25,1,.6,.5], 76:[1,1,1,1],
               84:[1,0,.2,.3], 92:[0,1,.9,1], 132:[0,0,0,1], 168:[1,1,1,0]}
    return initial, patches, events, weights


def compare(output, lib, refs, prefix, font, size, tape, integration=None):
    Bank, vn, create_piano, pn, Keys, jns, _ = refs
    bank = Bank(48000); phase_index = 0
    original_set_voice = bank.set_voice
    initial, patches, events, weight_changes = integration.fixture() if integration else fixture()
    values = np.array(initial, dtype=np.float64)
    ptr = lambda a: a.ctypes.data_as(PD)
    fluid = Fluid(prefix, font)
    handle = lib.sources_create(str(font).encode(), size, ptr(tape), len(tape))
    if not handle: fluid.close(); raise RuntimeError("Native source graph creation failed")
    player = create_piano(); player.fs = fluid
    player._vel_tracker = .7; player._note_on_count = player._active_notes = player._silent_blocks = 0
    def checked(label, function, *args, **kwargs):
        if function(*args) != 0: raise RuntimeError("Reference native call failed: " + label)
        return True
    player._native_call_checked = checked
    def phase(slot):
        nonlocal phase_index
        if phase_index >= len(tape): raise RuntimeError("Reference phase tape exhausted")
        for field, value in zip(("_osc1_phase_zones","_osc2_phase_zones","_lfo1_phase_zones","_lfo2_phase_zones"), tape[phase_index]):
            getattr(bank, field)[slot][0] = float(value)
        phase_index += 1
    bank.randomize_phase = phase; bank.randomize_lfo_phase = lambda slot: None
    def gate(slot, note, a, b):
        hz = 440. * 2. ** ((note - 69) / 12.)
        if values[21] != 0: hz *= 2. ** (values[21] / 12.)
        original_set_voice(slot, hz, a, b)
    bank.set_voice = gate
    voice = SimpleNamespace(voices=[], max_voices=12, _age_counter=0, lfo_key_sync=False, lfo2_key_sync=False,
                            _faust_osc_bank=bank, _faust_slot_free=list(range(12)), _faust_nvoices=12,
                            unison_voices=3, _render_lock=nullcontext())
    voice._voice_pool = [vn["Voice"](adsr_osc1=vn["ADSREnvelope"](vn["ADSRConfig"]()),
                                     adsr_osc2=vn["ADSREnvelope"](vn["ADSRConfig"]())) for _ in range(12)]
    keys = Keys(); keys.min_velocity = 10; keys.split_enabled = True
    weights = np.ones(4, dtype=np.float64)
    class Synth:
        def note_on(self,n,v,*w): vn["_note_on_locked"](voice,n,v,*w)
        def note_off(self,n): vn["note_off"](voice,n)
        def all_notes_off(self): vn["all_notes_off"](voice)
        def compute_split_weights(self,n): return tuple(weights[:3])
    keys.synth = Synth(); jns["SynthEngine"].split_weight = lambda *args: weights[3]
    def piano_event(kind, note, velocity):
        if kind == "note_on": pn["note_on"](player, note, velocity)
        elif kind == "note_off": fluid.noteoff(0, note)  # unmatched already-stolen voice is nonfatal
        elif kind == "all_notes_off": pn["_release_all_native_locked"](player)
        else: raise RuntimeError("Unexpected reference piano event: " + kind)
    keys.piano_callback = piano_event
    total = 640 * 512
    channels = getattr(integration,"channels",11) if integration else 7
    actual, expected = np.empty((channels,total)), np.empty((channels,total))
    reference_source = np.empty((7,size))
    raw_max_delta = 0
    idle_raw_peak = 0
    try:
        for frame in range(0,total,size):
            tick = frame // 512
            if frame % 512 == 0 and tick in patches:
                for i,v in patches[tick].items(): values[i] = v
                if not lib.sources_patch(handle,ptr(values),len(values)): raise RuntimeError("Native patch rejected")
                first, second = vn["ADSRConfig"](*values[:4]), vn["ADSRConfig"](*values[4:8])
                for v in voice.voices + voice._voice_pool: v.adsr_osc1.config, v.adsr_osc2.config = first,second
                keys.transpose, keys.piano_octave, keys.min_velocity = map(int,values[8:11])
                waveforms = ("sine","square","saw","triangle","saturated")
                bank.set_osc_params(waveforms[int(values[11])],waveforms[int(values[12])], *values[15:17],
                                    int(values[13]),int(values[14]), *values[17:21])
                bank.set_shimmer_params(bool(values[22]),bool(values[23]))
                shapes = ("sine","triangle","square","saw","ramp","peak","sh")
                for n in range(2):
                    k = 24+4*n; bank.set_lfo_params(n+1,active=bool(values[k]),rate_hz=values[k+1],depth=values[k+2],shape=shapes[int(values[k+3])])
                for field,value in zip(("volume","lowcut_hz","highcut_hz","comp_enabled","comp_threshold_db","comp_ratio",
                                        "comp_makeup_db","comp_drive_db","comp_wet","vel_bright_enabled","vel_bright_amount",
                                        "tremolo_hz","tremolo_depth","velocity_curve"), values[32:46]): setattr(player,field,value)
                player.comp_attack_ms=10; player.comp_release_ms=80; player.comp_knee_db=18
                player.eq_bands = [dict(zip(("freq_hz","gain_db","q","enabled"), row)) for row in
                                   ((150,2,.8,True),(300,-2.5,1,True),(2800,-3,1.5,True),(10000,-1.5,.7,True))]
            skip = integration.prepare(handle, bank, values, frame, size) if integration else False
            if frame % 512 == 0:
                if tick in weight_changes: weights[:]=weight_changes[tick]
                for kind,note,value in events.get(tick,[]):
                    if integration is not None and hasattr(integration,"key_event"):
                        if not integration.key_event(handle,frame,kind,note,value,weights): raise RuntimeError("Native raw-key event rejected")
                    elif not lib.sources_command(handle,frame,kind,note,value,ptr(weights)): raise RuntimeError("Native event rejected")
                    message = ((0x90,note,value) if kind==0 else (0x80,note,0) if kind==1 else
                               (0xB0,64,127*value) if kind==2 else (0xB0,66,127*value) if kind==3 else (0xB0,123,0))
                    keys.feed(message)
            vn["begin"](voice,size,skip)
            if not skip: vn["prepare"](voice)
            reference_source[:5] = 0 if skip else bank.process(size)
            vn["end"](voice)
            raw = fluid.render(size)
            if frame < 4*512: idle_raw_peak=max(idle_raw_peak,int(np.max(np.abs(raw.astype(np.int32)))))
            reference_source[5:] = player.process_raw(raw,size)
            render_first = integration is not None and hasattr(integration,"native_before_reference")
            if render_first:
                if not lib.sources_render(handle): raise RuntimeError("Native source render failed")
                integration.native_before_reference(handle,size)
            expected[:,frame:frame+size] = integration.process(reference_source, len(voice.voices)) if integration else reference_source
            if not render_first and not lib.sources_render(handle): raise RuntimeError("Native source render failed")
            for c in range(channels): actual[c,frame:frame+size] = np.ctypeslib.as_array(lib.sources_stem(handle,c),shape=(size,))
            native_raw = np.ctypeslib.as_array(lib.sources_raw(handle),shape=(size*2,)).astype(np.int32)
            raw_max_delta = max(raw_max_delta,int(np.max(np.abs(native_raw-raw.astype(np.int32)))))
            if lib.sources_voices(handle) != len(voice.voices) or lib.sources_phases(handle) != phase_index:
                raise RuntimeError(f"Voice/phase ownership mismatch at{frame}")
        if not np.isfinite(actual).all() or not np.isfinite(expected).all(): raise RuntimeError("Nonfinite stem output")
        if voice.voices or lib.sources_voices(handle): raise RuntimeError("Release window left oscillator voices alive")
        np.save(output/f"sources-native-{size}.npy",actual,allow_pickle=False)
        np.save(output/f"sources-reference-{size}.npy",expected,allow_pickle=False)
        peak_delta = np.max(np.abs(actual-expected),axis=1)
        audio_ok = (integration.validate_audio(actual,expected) if integration is not None and hasattr(integration,"validate_audio")
                    else not np.any(peak_delta > TOLERANCE))
        if raw_max_delta or not audio_ok:
            raise RuntimeError(f"Source parity failed at{size}: raw={raw_max_delta}, stems={peak_delta.tolist()}")
        stats = (ct.c_uint64*6)(); lib.sources_stats(handle,stats)
        if any(stats[i] for i in (0,1,3,4,5)): raise RuntimeError(f"Unexpected graph counters: {list(stats)}")
        # Boundary-only admission is deliberate; no silent rounding or reset.
        for offset in (-1,1,size):
            if lib.sources_command(handle,total+offset,0,60,100,ptr(weights)): raise RuntimeError("Noncurrent event accepted")
        lib.sources_stop(handle)
        if lib.sources_render(handle) or lib.sources_command(handle,total,0,60,100,ptr(weights)):
            raise RuntimeError("Terminal stop resumed")
        if any(np.any(np.ctypeslib.as_array(lib.sources_stem(handle,c),shape=(size,))) for c in range(channels)):
            raise RuntimeError("Stop not silent")
        return {"frames":total,"block_frames":size,"peak_delta_by_stem":peak_delta.tolist(),
                "peak_by_stem":np.max(np.abs(actual),axis=1).tolist(),"raw_piano_max_lsb_difference":raw_max_delta,
                "reference_idle_piano_peak_lsb":idle_raw_peak,
                "phase_starts":phase_index,"unmatched_piano_offs":int(stats[2]),"boundary_and_terminal_stop_guards":True,
                "final_active_oscillator_voices":0,"fluid_version":fluid.lib.fluid_version_str().decode()}
    finally:
        lib.sources_delete(handle); fluid.close(); bank.__del__()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--soundfont",required=True,type=Path)
    parser.add_argument("--fluidsynth-prefix",type=Path,default=Path("/opt/homebrew/opt/fluid-synth"))
    parser.add_argument("--output-dir",type=Path)
    args=parser.parse_args()
    font=args.soundfont.resolve()
    if not font.is_file(): parser.error("Existing SoundFont required")
    output=args.output_dir.absolute() if args.output_dir else Path(tempfile.mkdtemp(prefix="stave-sources-"))
    if args.output_dir: output.mkdir(mode=0o700,exist_ok=False)
    paths=[Path(__file__),Path(base.__file__),*sorted((ROOT/"native_v2").rglob("*.hpp")),*sorted((ROOT/"native_v2").rglob("*.cpp")),ROOT/"faust/faust_cprelude.h"]
    hashes=lambda:{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report={"status":"running","reference":base.REFERENCE,"source_hashes":hashes(),"host":platform.platform(),
            "soundfont_sha256":hashlib.sha256(font.read_bytes()).hexdigest(),"absolute_tolerance":TOLERANCE,
            "whole_instrument_parity":False,"pi4_qualified":False,"commands":base.COMMANDS,"runs":[]}
    print(f"Offline source graph artifacts: {output}",flush=True)
    try:
        library,bank,cc,report["compiler"],guard,report["faust_version"]=build(output,args.fluidsynth_prefix)
        report["guards"]=base.run([str(guard),str(font)],timeout=60).stdout
        report["sanitizer_scope"]="UBSan C++ graph/piano/components; generated C and external Fluid library uninstrumented; no ASan"
        print(report["guards"],flush=True)
        refs=reference_components(output,bank,cc); lib=native_api(library)
        tape=np.random.RandomState(base.SEED).uniform(0,1,(512,4)).astype(np.float64)
        np.save(output/"phase-tape.npy",tape,allow_pickle=False)
        (output/"fixture.json").write_text(json.dumps(fixture(),indent=2)+"\n")
        report["phase_tape_sha256"]=hashlib.sha256(tape.tobytes()).hexdigest()
        for size in (512,256):
            report["runs"].append(compare(output,lib,refs,args.fluidsynth_prefix,font,size,tape))
            print(f"PASS: seven native source stems at{size}",flush=True)
        if hashes()!=report["source_hashes"] or hashlib.sha256(font.read_bytes()).hexdigest()!=report["soundfont_sha256"]:
            raise RuntimeError("Sources/assets changed during comparison")
        report["status"]="passed_source_slice_only"
    except Exception as exc:
        report["status"]="failed"; report["error"]=str(exc); print(str(exc),flush=True)
    finally:
        (output/"report.json").write_text(json.dumps(report,indent=2)+"\n")
        print(json.dumps(report["runs"],indent=2)); print(f"Report: {output/'report.json'}")
    return 0 if report["status"]=="passed_source_slice_only" else 1


if __name__=="__main__": raise SystemExit(main())
