"""Read-only source probes. No application imports, sockets or live data access.

Assertions describe reproduced baseline defects, NOT corrected behavior.
"""
import ast
import ctypes
import json
import logging
import os
import queue
import re
import tempfile
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger('audit')

def methods(file, cls, names, env=None):
    tree = ast.parse((ROOT / file).read_text())
    parent = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    selected = [n for n in parent.body if isinstance(n, ast.FunctionDef) and n.name in names]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *selected], type_ignores=[])
    scope = dict(globals(), **(env or {}))
    exec(compile(ast.fix_missing_locations(module), str(ROOT / file), 'exec'), scope)
    return {name: scope[name] for name in names}

setting = methods('stave_synth/main.py', 'StaveSynth', ['_handle_setting'], {'_EQ_BAND_RE': re.compile(r'^eq_band(\d+)_(freq|gain|q|enabled)$')})['_handle_setting']
obj = NS(state={'master': {'piano_octave': 1}, 'piano': {'eq_bands': []}}, jack=NS(piano_octave=0), piano=None)
ack = setting(obj, {'section':'master', 'param':'piano_octave', 'value':1})
assert ack['type'] == 'setting_ack' and obj.jack.piano_octave == 0
print('REPRODUCED: piano octave setting acknowledged without applying to engine')
setting(obj, {'section':'piano', 'param':'eq_band1000_gain', 'value':0})
assert len(obj.state['piano']['eq_bands']) == 1001
print('REPRODUCED: out-of-range EQ index grows persistent band list to 1001')

jm = methods('stave_synth/jack_engine.py', 'JackEngine', ['_midi_loop_body','panic'], {'_MIDI_POLL_S':0, 'SAMPLE_RATE':48000})
def engine():
    events=[]
    synth=NS(note_on=lambda *a:events.append(('on',a[0])), note_off=lambda n:events.append(('off',n)))
    j=NS(running=True, _midi_events_seen=0, _midi_notes_triggered=0,
         min_velocity=10, transpose=0, piano_octave=0, split_enabled=False,
         bus_comp_retrigger=False, midi_callback=None, midi_clock_enabled=False,
         _sustain_on=False, _sostenuto_on=False, synth=synth,
         piano_callback=lambda *a:events.append(('piano',*a)),
         _note_map={}, _physically_held=set(), _sustained_notes=set(),
         _sostenuto_held=set(), _pad_notes_active=set(), _piano_notes_active=set(),
         _limiter=NS(reset=lambda:None))
    return j,events
def dispatch(j, seq):
    it=iter(seq)
    def read(buf):
        item=next(it,None)
        while callable(item):
            item(); item=next(it,None)
        if item is None:
            j.running=False
            return 0
        for i,b in enumerate(item): buf[i]=b
        return len(item)
    j.running=True
    j._bridge=NS(bridge_read_midi=read, bridge_clear_ring=lambda:0)
    jm['_midi_loop_body'](j)

j,ev=engine()
dispatch(j,[(0x90,60,90),(0xB0,66,127),(0xB0,64,127),(0x80,60,0),(0xB0,64,0)])
assert j._sostenuto_on and ('piano','note_off',60,0) in ev
print('REPRODUCED: sustain release cuts a note still held by sostenuto')
jm['panic'](j)
assert j._sostenuto_on and 60 in j._sostenuto_held
print('REPRODUCED: UI panic leaves stale sostenuto state')
j,ev=engine()
dispatch(j,[(0x90,60,90),(0xB0,64,127),(0x80,60,0),lambda:setattr(j,'piano_octave',1),(0x90,60,90),(0x80,60,0),(0xB0,64,0)])
assert ('piano','note_on',60,90/127) in ev and ('piano','note_off',60,0) not in ev
print('REPRODUCED: sustained-note retrigger after piano octave change strands old pitch')

# Import only Recorder class; never import config or create real app directories.
with tempfile.TemporaryDirectory(prefix='stave-recorder-probe-') as d:
    tree=ast.parse((ROOT/'stave_synth/recorder.py').read_text())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Recorder')
    module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),cls],type_ignores=[])
    fixed=NS(now=lambda:datetime(2026,9,14,12,0,0))
    scope=dict(globals(), RECORDINGS_DIR=Path(d), SAMPLE_RATE=48000, MAX_QUEUE=400, MAX_TAKE_SECONDS=1800, datetime=fixed)
    exec(compile(ast.fix_missing_locations(module),'recorder-source','exec'),scope)
    recorder=scope['Recorder']()
    one=recorder.start({'take':1}); recorder.feed(np.ones(64,dtype=np.float32)*.1,np.zeros(64,dtype=np.float32)); recorder.stop()
    two=recorder.start({'take':2}); recorder.feed(np.ones(32,dtype=np.float32)*.2,np.zeros(32,dtype=np.float32)); recorder.stop()
    with wave.open(two['path']) as wav: frames=wav.getnframes()
    assert one['path']==two['path'] and frames==32
    print('REPRODUCED: two starts in one timestamp second overwrite prior WAV and sidecar')

# Deterministically interleave the real save_state function's shared temp path.
with tempfile.TemporaryDirectory(prefix='stave-state-probe-') as d:
    target=Path(d)/'state.json'
    a_dumped=threading.Event(); b_partial=threading.Event(); release_b=threading.Event()
    failures=[]
    def interleaved_dump(state, f, **kw):
        if state['writer']=='A':
            json.dump(state,f,**kw); f.flush(); a_dumped.set()
            assert b_partial.wait(3)
        else:
            f.write('{"writer":'); f.flush(); b_partial.set()
            assert release_b.wait(3)
            f.write('"B"}')
    tree=ast.parse((ROOT/'stave_synth/config.py').read_text())
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='save_state')
    scope=dict(globals(),STATE_FILE=target,ensure_dirs=lambda:None,json=NS(dump=interleaved_dump))
    exec(compile(ast.Module(body=[fn],type_ignores=[]),'save-state-source','exec'),scope)
    def worker(name):
        try: scope['save_state']({'writer':name})
        except Exception as e: failures.append(type(e).__name__)
    a=threading.Thread(target=worker,args=('A',)); b=threading.Thread(target=worker,args=('B',))
    a.start(); assert a_dumped.wait(3); b.start(); a.join(3)
    try:
        assert not a.is_alive() and target.read_text()=='{"writer":'
        print('REPRODUCED: concurrent save exposes partial JSON at final state path')
    finally:
        release_b.set(); a.join(3); b.join(3)
    assert 'FileNotFoundError' in failures
    print('REPRODUCED: second concurrent writer loses shared temp path and fails rename')
print('8 baseline defect scenarios completed; no production runtime touched.')
