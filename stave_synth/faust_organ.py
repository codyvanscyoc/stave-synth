"""Faust-native B3 organ (drop-in for OrganEngine).

Faust owns tonewheel synthesis, drive, Leslie, tone shaping. Python owns:
- Envelope (attack + release linear ramps) — written to gate_v%i per block
- Click sample generation + mixing (passed to Faust as mono input)
- Drawbar amps + crosstalk pre-mix (9 effective amps per block)
- Leslie speed target (slow/fast → Hz)
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np
from cffi import FFI

try:
    from scipy.signal import lfilter
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

from .config import SAMPLE_RATE
from .audio_io.platform import LIB_SUFFIX

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent.parent / "faust"
_LIB = _HERE / f"libstave_organ{LIB_SUFFIX}"

TWO_PI = 2.0 * np.pi
N_SLOTS = 16

# Matches Python HARMONIC_RATIOS in organ_engine.py
HARMONIC_RATIOS = [0.5, 1.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0]
CROSSTALK_LEVEL = 0.008  # matches organ_engine.py

ORGAN_PRESETS = {
    "mellow":  [8, 0, 6, 4, 0, 0, 0, 0, 0],
    "full":    [8, 6, 8, 8, 6, 6, 4, 4, 4],
    "gospel":  [8, 8, 8, 6, 4, 4, 2, 2, 2],
    "jazz":    [8, 0, 8, 0, 0, 0, 0, 0, 0],
}

LESLIE_SLOW_HZ = 0.8
LESLIE_FAST_HZ = 6.5


_ffi = FFI()
_ffi.cdef("""
typedef struct StaveOrgan StaveOrgan;

typedef void (*openBoxFn)(void* ui, const char* label);
typedef void (*closeBoxFn)(void* ui);
typedef void (*addBtnFn)(void* ui, const char* label, float* zone);
typedef void (*addSliderFn)(void* ui, const char* label, float* zone,
                            float init, float min, float max, float step);
typedef void (*addBarFn)(void* ui, const char* label, float* zone, float min, float max);
typedef void (*addSFFn)(void* ui, const char* label, const char* url, void** sf);
typedef void (*declareFn)(void* ui, float* zone, const char* key, const char* value);

typedef struct {
    void* uiInterface;
    openBoxFn openTabBox;
    openBoxFn openHorizontalBox;
    openBoxFn openVerticalBox;
    closeBoxFn closeBox;
    addBtnFn addButton;
    addBtnFn addCheckButton;
    addSliderFn addVerticalSlider;
    addSliderFn addHorizontalSlider;
    addSliderFn addNumEntry;
    addBarFn addHorizontalBargraph;
    addBarFn addVerticalBargraph;
    addSFFn addSoundfile;
    declareFn declare;
} UIGlue;

StaveOrgan* newStaveOrgan(void);
void deleteStaveOrgan(StaveOrgan*);
void initStaveOrgan(StaveOrgan*, int sample_rate);
void instanceClearStaveOrgan(StaveOrgan*);
void buildUserInterfaceStaveOrgan(StaveOrgan*, UIGlue* ui);
void computeStaveOrgan(StaveOrgan*, int count, float** inputs, float** outputs);
""")

try:
    _lib = _ffi.dlopen(str(_LIB))
except OSError as e:
    raise RuntimeError(f"Failed to load {_LIB}: {e}. Run faust/build.sh.")


class _Voice:
    """Lightweight per-note voice state. Slot is the Faust voice index."""
    __slots__ = ('note', 'slot', 'velocity', 'attack_remaining', 'attack_total',
                 'release_remaining', 'release_total', 'releasing',
                 'click_remaining')

    def __init__(self, note: int, slot: int, velocity: float,
                 attack_samples: int, click_samples: int):
        self.note = note
        self.slot = slot
        self.velocity = velocity
        self.attack_remaining = attack_samples
        self.attack_total = attack_samples
        self.release_remaining = 0
        self.release_total = 0
        self.releasing = False
        self.click_remaining = click_samples

    def start_release(self, release_samples: int):
        if not self.releasing:
            self.releasing = True
            self.release_total = max(1, release_samples)
            self.release_remaining = self.release_total


def _generate_click_sample(sample_rate: int) -> np.ndarray:
    """Generate B3 key-click sample: filtered noise with fast decay. Matches
    organ_engine.py._generate_click_sample exactly (same seed → same burst)."""
    click_len = int(0.003 * sample_rate)
    raw_noise = np.random.default_rng(42).standard_normal(click_len)
    if HAS_SCIPY:
        w_hp = TWO_PI * 1000.0 / sample_rate
        a_hp = 1.0 / (1.0 + w_hp)
        raw_noise = lfilter(np.array([a_hp, -a_hp]), np.array([1.0, -a_hp]), raw_noise)
        w_lp = TWO_PI * 4000.0 / sample_rate
        a_lp = w_lp / (1.0 + w_lp)
        raw_noise = lfilter(np.array([a_lp]), np.array([1.0, -(1.0 - a_lp)]), raw_noise)
    decay = np.exp(-np.arange(click_len, dtype=np.float64) / (0.0008 * sample_rate))
    return (raw_noise * decay).astype(np.float64)


class FaustOrganEngine:
    """Drop-in replacement for OrganEngine backed by libstave_organ.so."""

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = int(sample_rate)
        self.enabled = False
        self.volume = 0.5

        # Drawbars
        self.preset = "mellow"
        self.drawbars = list(ORGAN_PRESETS["mellow"])

        # Drive
        self.drive = 0.05

        # Click
        self.click_enabled = True
        self.click_level = 0.3
        self._click_sample = _generate_click_sample(self.sample_rate)

        # Envelope
        self.attack_ms = 0.0
        self.release_ms = 10.0

        # Leslie
        self.leslie_speed = "slow"
        self.leslie_depth = 0.3

        # Tone
        self.highcut_hz = 8000.0
        self.lowcut_hz = 40.0
        # Tone tilt (fader TONE in UI): 0=warm, 0.5=flat, 1=bright. Volume-neutral.
        self.tone_tilt = 0.5

        # Keyboard stereo spread — like a piano: low notes lean left, high lean
        # right. 0=mono (all voices center), 1=full spread across ±1 at ±24 st
        # from middle C. Independent of Leslie depth, so both can combine.
        self.width = 0.7

        # ── Faust DSP instance ──
        self._dsp = _lib.newStaveOrgan()
        _lib.initStaveOrgan(self._dsp, self.sample_rate)
        self._zones: dict[str, _ffi.CData] = {}
        self._keepalive = _install_ui_callbacks(self._dsp, self._zones)

        self._freq_zones = [self._zones[f"freq_v{i}"] for i in range(N_SLOTS)]
        self._gate_zones = [self._zones[f"gate_v{i}"] for i in range(N_SLOTS)]
        self._phase_zones = [self._zones[f"phase_v{i}"] for i in range(N_SLOTS)]
        self._pan_zones = [self._zones[f"pan_v{i}"] for i in range(N_SLOTS)]
        self._amp_zones = [self._zones[f"amp_d{h}"] for h in range(9)]

        # ── Voice tracking ──
        self.voices: dict[int, _Voice] = {}
        self._free_slots = list(range(N_SLOTS))
        self._slot_to_voice: dict[int, _Voice] = {}
        self._lock = threading.Lock()

        # ── I/O buffers (allocated on first block) ──
        self._buf_n = 0
        self._in_mono = np.empty(0, dtype=np.float32)
        self._out_l = np.empty(0, dtype=np.float32)
        self._out_r = np.empty(0, dtype=np.float32)
        self._in_ptrs = _ffi.new("float*[1]")
        self._out_ptrs = _ffi.new("float*[2]")

        # Push initial params
        self._push_drawbar_amps()
        self._zones["drive"][0] = self.drive
        self._zones["leslie_depth"][0] = self.leslie_depth
        self._zones["leslie_target_hz"][0] = LESLIE_SLOW_HZ
        self._zones["highcut_hz"][0] = self.highcut_hz
        self._zones["lowcut_hz"][0] = self.lowcut_hz
        self._zones["volume"][0] = self.volume
        self._zones["tone_tilt"][0] = self.tone_tilt

    def __del__(self):
        try:
            if getattr(self, "_dsp", None):
                _lib.deleteStaveOrgan(self._dsp)
                self._dsp = None
        except Exception:
            pass

    # ── Drawbar crosstalk pre-mix ──
    def _push_drawbar_amps(self):
        """Compute effective drawbar amps (main + crosstalk from neighbors)
        and push to Faust. Matches organ_engine.py crosstalk formulation:
          effective[h] = amps[h] + CROSSTALK * (amps[h-1] + amps[h+1])
        with edges clamped (no wrap)."""
        raw = np.array(self.drawbars, dtype=np.float64) / 8.0
        total = raw.sum()
        if total > 0:
            raw *= (1.0 / max(total, 1.0))
        # Neighbor shifts with edge=0
        up = np.roll(raw, -1)
        up[-1] = 0.0
        dn = np.roll(raw, 1)
        dn[0] = 0.0
        effective = raw + CROSSTALK_LEVEL * (up + dn)
        for h in range(9):
            self._amp_zones[h][0] = float(effective[h])

    # ── Parameter setters ──
    def set_volume(self, v: float):
        self.volume = max(0.0, min(1.0, float(v)))
        self._zones["volume"][0] = self.volume

    def set_highcut(self, freq_hz: float):
        self.highcut_hz = max(200.0, min(12000.0, float(freq_hz)))
        self._zones["highcut_hz"][0] = self.highcut_hz

    def set_lowcut(self, freq_hz: float):
        self.lowcut_hz = max(20.0, min(500.0, float(freq_hz)))
        self._zones["lowcut_hz"][0] = self.lowcut_hz

    def set_tone_tilt(self, t: float):
        self.tone_tilt = max(0.0, min(1.0, float(t)))
        self._zones["tone_tilt"][0] = self.tone_tilt

    def set_width(self, w: float):
        """Keyboard stereo spread (0=mono, 1=full). Re-pushes active voices'
        pan values so the change is heard immediately on held notes."""
        self.width = max(0.0, min(1.0, float(w)))
        with self._lock:
            for v in self.voices.values():
                self._pan_zones[v.slot][0] = self._pan_for_note(v.note)

    def _pan_for_note(self, midi_note: int) -> float:
        """Map MIDI note → pan [-1, +1], scaled by width.
        Middle C (60) is center; ±24 semitones covers full spread."""
        pos = max(-1.0, min(1.0, (midi_note - 60) / 24.0))
        return float(pos * self.width)

    def set_preset(self, name: str):
        if name in ORGAN_PRESETS:
            self.preset = name
            self.drawbars = list(ORGAN_PRESETS[name])
            self._push_drawbar_amps()

    # ── MIDI ──
    def note_on(self, note: int, velocity: float):
        if not self.enabled:
            return
        attack_samples = int(self.attack_ms * 0.001 * self.sample_rate)
        click_samples = int(0.002 * self.sample_rate) if self.click_enabled else 0
        with self._lock:
            if note in self.voices and not self.voices[note].releasing:
                # Re-trigger same-note voice (refresh click + attack, bump velocity)
                v = self.voices[note]
                v.velocity = float(velocity)
                v.click_remaining = click_samples
                v.attack_remaining = attack_samples
                v.attack_total = attack_samples
                return

            # Allocate a slot (steal oldest non-releasing if none free)
            if self._free_slots:
                slot = self._free_slots.pop(0)
            else:
                # Steal the oldest voice — pick any voice (dicts are insertion-ordered)
                victim_note = next(iter(self.voices))
                victim = self.voices.pop(victim_note)
                self._slot_to_voice.pop(victim.slot, None)
                slot = victim.slot

            v = _Voice(note, slot, float(velocity), attack_samples, click_samples)
            self.voices[note] = v
            self._slot_to_voice[slot] = v

            # Write freq + random phase offset + keyboard pan for this slot
            freq_hz = 440.0 * (2.0 ** ((note - 69) / 12.0))
            self._freq_zones[slot][0] = float(freq_hz)
            self._phase_zones[slot][0] = float(np.random.uniform(0.0, 1.0))
            self._pan_zones[slot][0] = self._pan_for_note(note)

    def note_off(self, note: int):
        with self._lock:
            v = self.voices.get(note)
            if v is not None:
                v.start_release(max(1, int(self.release_ms * 0.001 * self.sample_rate)))

    def all_notes_off(self):
        rs = max(1, int(self.release_ms * 0.001 * self.sample_rate))
        with self._lock:
            for v in self.voices.values():
                v.start_release(rs)

    def midi_callback(self, event_type: str, note: int, velocity: float):
        if event_type == "note_on":
            self.note_on(note, velocity)
        elif event_type == "note_off":
            self.note_off(note)
        elif event_type == "all_notes_off":
            self.all_notes_off()

    # ── Block render ──
    def render_block(self, n_samples: int) -> np.ndarray:
        if not self.enabled or n_samples == 0:
            return np.zeros((2, max(n_samples, 0)), dtype=np.float64)

        # Resize I/O buffers if block size changed
        if n_samples != self._buf_n:
            self._in_mono = np.zeros(n_samples, dtype=np.float32)
            self._out_l = np.empty(n_samples, dtype=np.float32)
            self._out_r = np.empty(n_samples, dtype=np.float32)
            self._in_ptrs[0] = _ffi.cast("float*", self._in_mono.ctypes.data)
            self._out_ptrs[0] = _ffi.cast("float*", self._out_l.ctypes.data)
            self._out_ptrs[1] = _ffi.cast("float*", self._out_r.ctypes.data)
            self._buf_n = n_samples
        else:
            self._in_mono.fill(0.0)

        # Leslie speed target
        target_hz = LESLIE_FAST_HZ if self.leslie_speed == "fast" else LESLIE_SLOW_HZ
        self._zones["leslie_target_hz"][0] = float(target_hz)
        self._zones["leslie_depth"][0] = float(self.leslie_depth)
        self._zones["drive"][0] = float(self.drive)

        # ── Per-voice envelope + click mixing ──
        click_sample = self._click_sample
        click_len = len(click_sample)
        click_level = self.click_level if self.click_enabled else 0.0
        dead_notes = []

        with self._lock:
            voices = list(self.voices.items())

        for note, v in voices:
            # Compute final gate for this block (end-of-block value, velocity
            # scaled, with attack + release envelopes applied multiplicatively).
            gate = v.velocity

            # Attack ramp (linear 0 → 1 over attack_total)
            if v.attack_remaining > 0 and v.attack_total > 0:
                total = v.attack_total
                done = total - v.attack_remaining
                end_idx = min(v.attack_remaining, n_samples)
                # Block-end attack factor
                attack_factor = min(1.0, (done + end_idx) / total)
                gate *= attack_factor
                v.attack_remaining = max(0, v.attack_remaining - n_samples)

            # Release ramp (linear remaining/release_total → 0).
            # Faust sees one scalar gate per block which it then 1ms-smooths
            # internally, so for the partial-block release case we write the
            # time-weighted *average* gate over the block rather than 0 —
            # otherwise short release_ms values cut to silence in one step
            # and produce an audible click before si.smooth can catch up.
            if v.releasing:
                release_samples = max(1, v.release_total)
                if v.release_remaining <= 0:
                    dead_notes.append(note)
                    self._gate_zones[v.slot][0] = 0.0
                    continue
                if v.release_remaining >= n_samples:
                    end_factor = max(0.0, (v.release_remaining - n_samples) / release_samples)
                    v.release_remaining -= n_samples
                else:
                    # Partial-block release: linear ramp from start_factor → 0
                    # over `r` samples, then silence for the rest of the block.
                    # Time-weighted average = (start_factor / 2) × (r / n_samples).
                    r = v.release_remaining
                    start_factor = r / release_samples
                    end_factor = (start_factor * 0.5) * (r / n_samples)
                    v.release_remaining = 0
                    dead_notes.append(note)
                gate *= end_factor

            self._gate_zones[v.slot][0] = float(max(0.0, min(1.0, gate)))

            # Click contribution (velocity-independent level, attack-scaled)
            if v.click_remaining > 0 and click_level > 0.0:
                click_start = click_len - v.click_remaining
                if click_start < 0:
                    click_start = 0
                click_end = min(click_start + min(v.click_remaining, n_samples), click_len)
                n = click_end - click_start
                if n > 0:
                    # Attack-scale the click too (matches Python behavior)
                    click_scale = click_level
                    if v.attack_total > 0 and v.attack_remaining > 0:
                        # Approximate: use block-start attack factor
                        prior_done = v.attack_total - v.attack_remaining - n_samples
                        click_scale *= max(0.0, min(1.0, (prior_done + n) / v.attack_total))
                    self._in_mono[:n] += (
                        click_sample[click_start:click_end] * click_scale
                    ).astype(np.float32)
                v.click_remaining = max(0, v.click_remaining - n_samples)

        # ── Faust compute ──
        _lib.computeStaveOrgan(self._dsp, n_samples, self._in_ptrs, self._out_ptrs)

        # ── Reap dead voices ──
        if dead_notes:
            with self._lock:
                for note in dead_notes:
                    v = self.voices.pop(note, None)
                    if v is not None:
                        self._slot_to_voice.pop(v.slot, None)
                        self._free_slots.append(v.slot)
                        # Safety: clear gate + freq on freed slot so any stale
                        # residual envelope doesn't bleed into the next note.
                        self._gate_zones[v.slot][0] = 0.0

        out = np.empty((2, n_samples), dtype=np.float64)
        np.copyto(out[0], self._out_l, casting="unsafe")
        np.copyto(out[1], self._out_r, casting="unsafe")
        return out

    # ── OrganEngine-compatible param dispatch ──
    def update_params(self, params: dict):
        if "volume" in params:
            self.set_volume(float(params["volume"]))
        if "enabled" in params:
            self.enabled = bool(params["enabled"])
        if "preset" in params:
            self.set_preset(params["preset"])
        if "drawbars" in params:
            db = params["drawbars"]
            if isinstance(db, list) and len(db) == 9:
                self.drawbars = [max(0, min(8, int(x))) for x in db]
                self._push_drawbar_amps()
        if "leslie_speed" in params:
            speed = params["leslie_speed"]
            if speed in ("slow", "fast"):
                self.leslie_speed = speed
        if "leslie_depth" in params:
            self.leslie_depth = max(0.0, min(1.0, float(params["leslie_depth"])))
        if "click_enabled" in params:
            self.click_enabled = bool(params["click_enabled"])
        if "click_level" in params:
            self.click_level = max(0.0, min(1.0, float(params["click_level"])))
        if "attack_ms" in params:
            self.attack_ms = max(0.0, min(1000.0, float(params["attack_ms"])))
        if "release_ms" in params:
            self.release_ms = max(5.0, min(1000.0, float(params["release_ms"])))
        if "drive" in params:
            self.drive = max(0.0, min(1.0, float(params["drive"])))
        if "filter_highcut_hz" in params:
            self.set_highcut(float(params["filter_highcut_hz"]))
        if "filter_lowcut_hz" in params:
            self.set_lowcut(float(params["filter_lowcut_hz"]))
        if "tone_tilt" in params:
            self.set_tone_tilt(float(params["tone_tilt"]))
        if "width" in params:
            self.set_width(float(params["width"]))

    def get_params(self) -> dict:
        return {
            "enabled": self.enabled,
            "preset": self.preset,
            "drawbars": list(self.drawbars),
            "leslie_speed": self.leslie_speed,
            "leslie_depth": self.leslie_depth,
            "click_enabled": self.click_enabled,
            "click_level": self.click_level,
            "attack_ms": self.attack_ms,
            "release_ms": self.release_ms,
            "drive": self.drive,
            "filter_highcut_hz": self.highcut_hz,
            "filter_lowcut_hz": self.lowcut_hz,
            "volume": self.volume,
            "tone_tilt": self.tone_tilt,
            "width": self.width,
        }


def _install_ui_callbacks(dsp, zones: dict):
    keepalive = []

    @_ffi.callback("void(void*, const char*)")
    def _open(u, l): pass  # noqa: E701

    @_ffi.callback("void(void*)")
    def _close(u): pass  # noqa: E701

    @_ffi.callback("void(void*, const char*, float*)")
    def _btn(u, l, z): zones[_ffi.string(l).decode()] = z  # noqa: E701

    @_ffi.callback("void(void*, const char*, float*, float, float, float, float)")
    def _sl(u, l, z, i, lo, hi, st): zones[_ffi.string(l).decode()] = z  # noqa: E701

    @_ffi.callback("void(void*, const char*, float*, float, float)")
    def _bar(u, l, z, lo, hi): pass  # noqa: E701

    @_ffi.callback("void(void*, const char*, const char*, void**)")
    def _sf(u, l, url, s): pass  # noqa: E701

    @_ffi.callback("void(void*, float*, const char*, const char*)")
    def _dec(u, z, k, v): pass  # noqa: E701

    glue = _ffi.new("UIGlue*")
    glue.uiInterface = _ffi.NULL
    glue.openTabBox = _open
    glue.openHorizontalBox = _open
    glue.openVerticalBox = _open
    glue.closeBox = _close
    glue.addButton = _btn
    glue.addCheckButton = _btn
    glue.addVerticalSlider = _sl
    glue.addHorizontalSlider = _sl
    glue.addNumEntry = _sl
    glue.addHorizontalBargraph = _bar
    glue.addVerticalBargraph = _bar
    glue.addSoundfile = _sf
    glue.declare = _dec

    _lib.buildUserInterfaceStaveOrgan(dsp, glue)
    keepalive.extend([_open, _close, _btn, _sl, _bar, _sf, _dec, glue])
    return keepalive
