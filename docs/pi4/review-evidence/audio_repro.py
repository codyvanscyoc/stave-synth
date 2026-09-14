"""Read-only AST-extracted audio logic tests; no app/native/service imports.

Mock native calls verify ownership/control state, NOT DSP sound or performance.
"""
import ast
import logging
import math
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from dataclasses import dataclass, field
import numpy as np

ROOT = Path(__file__).resolve().parents[3]

def extract(file, names, env, skip_init=()):
    tree = ast.parse((ROOT / file).read_text())
    body = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            if node.name in skip_init:
                node.body = [x for x in node.body if not isinstance(x, ast.FunctionDef)
                             or x.name not in ('__init__', '__del__')]
            body.append(node)
    exec(compile(ast.Module(body=body, type_ignores=[]), file, 'exec'), env)

base = dict(np=np, SAMPLE_RATE=48000, threading=threading, math=math,
            logger=logging.getLogger('audio_repro'), dataclass=dataclass, field=field)
organ_env = dict(base, LESLIE_FAST_HZ=6.7, LESLIE_SLOW_HZ=0.8)
organ_lib = SimpleNamespace(computeStaveOrgan=lambda *args: None)
organ_env['_lib'] = organ_lib
extract('stave_synth/faust_organ.py', {'_Voice', 'FaustOrganEngine'}, organ_env,
        {'FaustOrganEngine'})

def organ():
    obj = organ_env['FaustOrganEngine']()
    obj.enabled = True
    obj.sample_rate = 48000
    obj.attack_ms = 0
    obj.release_ms = 100
    obj.click_enabled = False
    obj.click_level = 0
    obj.width = 1
    obj._lock = threading.Lock()
    obj.voices = {}
    obj._slot_to_voice = {}
    obj._free_slots = list(range(16))
    for name in ('freq', 'phase', 'pan', 'gate'):
        setattr(obj, f'_{name}_zones', [[0.] for _ in range(16)])
    obj._buf_n = 512
    obj._in_mono = np.zeros(512, dtype=np.float32)
    obj._out_l = np.zeros(512, dtype=np.float32)
    obj._out_r = np.zeros(512, dtype=np.float32)
    obj._in_ptrs = obj._out_ptrs = obj._dsp = None
    obj._zones = {k: [0.] for k in ('leslie_target_hz', 'leslie_depth', 'drive')}
    obj.leslie_speed = 'slow'
    obj.leslie_depth = obj.drive = 0.
    obj._click_sample = np.zeros(1)
    return obj

o = organ()
o.note_on(60, 1.)
o.render_block(512)
old_slot = o.voices[60].slot
o.note_off(60)
o.note_on(60, 1.)
new_slot = o.voices[60].slot
o.all_notes_off()
for _ in range(20):
    o.render_block(512)
assert not o.voices and o._gate_zones[old_slot][0] == 1.
assert old_slot not in o._free_slots
print('ORGAN_ORPHAN', {'old_slot': old_slot, 'new_slot': new_slot,
                       'active_keys': list(o.voices), 'stuck_gate': o._gate_zones[old_slot][0],
                       'free_slots': len(o._free_slots)})

o = organ()
o.note_on(60, 1.)
o.note_off(60)
o.voices[60].release_remaining = 0
old = o.voices[60]
replacements = []
def retrigger_during_compute(*args):
    o.note_on(60, 1.)
    replacements.append(o.voices[60])
organ_lib.computeStaveOrgan = retrigger_during_compute
o.render_block(512)
assert replacements[0] is not old and 60 not in o.voices
organ_lib.computeStaveOrgan = lambda *args: None
print('ORGAN_STALE_REAP', {'replacement_deleted': True,
                           'old_slot_orphaned': old.slot in o._slot_to_voice})

class Backend:
    def __init__(self):
        self.zones = {k: [v] for k, v in dict(feedback=.8, damp=.5, freeze_input=1.).items()}
        self.clears = 0
    def set_zone(self, k, v):
        self.zones.setdefault(k, [0.])[0] = v
    def clear(self):
        self.clears += 1
    def process(self, samples):
        return np.zeros_like(samples)

rev_env = dict(base, _lib=SimpleNamespace(instanceClearStaveReverb=lambda *a: None))
extract('stave_synth/faust_reverb.py', {'FaustReverb', '_set_zone',
        '_plate_decay_from_seconds', '_drone_fb_from_seconds'}, rev_env, {'FaustReverb'})
r = rev_env['FaustReverb']()
r.sample_rate = 48000
r._dsp = None
r.frozen = False
r.type = 'plate'
r._feedback_target = r._normal_feedback = .8
r._damp_target = r._normal_damp = .5
r._zones = {k: [v] for k, v in dict(feedback=.8, damp=.5, freeze_input=1., er_scale=.4).items()}
r._plate = Backend()
r._drone = Backend()
r._buf_n = 96000
r.decay_seconds = 3.
r.set_freeze(True)
r.process(np.zeros((2, 96000)))
assert r._plate.zones['freeze_input'][0] == 0.
r.panic()
assert not r.frozen and r._zones['freeze_input'][0] == 1.
assert r._plate.zones['freeze_input'][0] == r._drone.zones['freeze_input'][0] == 0.
print('PANIC_FREEZE', {'fdn_input': r._zones['freeze_input'][0],
                      'plate_input': r._plate.zones['freeze_input'][0],
                      'drone_input': r._drone.zones['freeze_input'][0]})
r.set_freeze(True)
r.set_decay(10.)
frozen_plate_fb = r._plate.zones['feedback'][0]
r.set_freeze(False)
print('FREEZE_DECAY', {'plate_fb_changed_while_frozen': frozen_plate_fb,
                      'fdn_fb_after_unfreeze': r._zones['feedback'][0],
                      'desired_fdn_fb_10s': 10 ** (-3 / (10 * 48000 / (sum([63.7,79.3,95.3,111.7,131.9,153.1,177.7,200.9])*48/8)))})

class IdentityFilter:
    def __init__(self, *a): pass
    def set_params(self, *a): pass
    def reset(self): pass
    def process(self, x): return x

sample_env = dict(base, BiquadLowpass=IdentityFilter)
extract('stave_synth/synth_engine.py', {'SamplePlayer'}, sample_env)
p = sample_env['SamplePlayer']()
p.samples_l = p.samples_r = np.arange(8, dtype=np.float64)
p.loaded = p.active = True
p.length = 8
p.xfade_len = 2
p.env = p.env_target = 1.
a = np.zeros(512)
b = np.zeros(512)
p.process(512, a, b)
assert p.read_pos >= p.length
print('SAMPLE_TINY', {'read_pos': p.read_pos, 'length': p.length,
                      'samples_at_clipped_last_index': int(np.count_nonzero(a == a[-1]))})
p.length = 0
p.samples_l = p.samples_r = np.zeros(0)
try:
    p.process(512, np.zeros(512), np.zeros(512))
except IndexError:
    print('SAMPLE_EMPTY', 'IndexError')
else:
    raise AssertionError('Expected empty pad failure')

piano_env = dict(base, SOUNDFONT_DIR=ROOT / 'docs/pi4/review-evidence/no_soundfonts', Path=Path)
extract('stave_synth/fluidsynth_player.py', {'FluidSynthPlayer'}, piano_env, {'FluidSynthPlayer'})
f = piano_env['FluidSynthPlayer']()
with patch('pathlib.Path.exists', return_value=False), patch('os.path.exists', side_effect=lambda p: p.endswith('/default-GM.sf2')):
    try:
        f._find_soundfont('FluidR3_GM')
    except RecursionError:
        print('SOUNDFONT_FALLBACK', 'RecursionError despite available default-GM.sf2')
    else:
        raise AssertionError('Expected recursive missing-font failure')

adsr_env = dict(base, _EXP_FACTOR_CACHE={}, _EXP_FACTOR_CACHE_MAX=64)
extract('stave_synth/synth_engine.py', {'ADSRConfig', 'ADSREnvelope', '_exp_factors'}, adsr_env)
e = adsr_env['ADSREnvelope'](adsr_env['ADSRConfig'](attack_ms=0, decay_ms=0, sustain_percent=0))
e.trigger()
envelope = e.process(512)
assert envelope.max() > 0 and envelope[-1] == 0
print('ADSR_END_GATE', {'python_peak': float(envelope.max()), 'faust_gate': float(envelope[-1])})

print('PASS: seven isolated state/control reproductions; no native DSP or acoustic tests.')
