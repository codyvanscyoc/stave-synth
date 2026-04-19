"""Faust-native port of FeedbackDelayReverb.

Mirrors the Python class's public API (`process`, `set_decay`, `set_low_cut`,
`set_high_cut`, `set_predelay`, `set_space`, `set_freeze`, `panic`) so it can
drop into `synth_engine.py` behind a flag.

float32 boundary — Faust compiles with FAUSTFLOAT=float to match its stdlib.
The engine uses float64 internally, so we convert at the edges.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np
from cffi import FFI

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent.parent / "faust"
_LIB = _HERE / "libstave_reverb.so"


_ffi = FFI()
_ffi.cdef("""
typedef struct StaveReverb StaveReverb;

/* Subset of UIGlue we actually use — all other widget callbacks can be NULL
 * because our reverb.dsp only uses hslider and open/close Vertical/Horizontal. */

typedef void (*openBoxFn)(void* ui, const char* label);
typedef void (*closeBoxFn)(void* ui);
typedef void (*addBtnFn)(void* ui, const char* label, float* zone);
typedef void (*addSliderFn)(void* ui, const char* label, float* zone,
                            float init, float min, float max, float step);
typedef void (*addBarFn)(void* ui, const char* label, float* zone,
                         float min, float max);
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

StaveReverb* newStaveReverb(void);
void deleteStaveReverb(StaveReverb*);
void initStaveReverb(StaveReverb*, int sample_rate);
void instanceClearStaveReverb(StaveReverb*);
int  getNumInputsStaveReverb(StaveReverb*);
int  getNumOutputsStaveReverb(StaveReverb*);
void buildUserInterfaceStaveReverb(StaveReverb*, UIGlue* ui);
void computeStaveReverb(StaveReverb*, int count, float** inputs, float** outputs);
""")

try:
    _lib = _ffi.dlopen(str(_LIB))
except OSError as e:
    raise RuntimeError(f"Failed to load {_LIB}: {e}. Run stave-synth/faust/build.sh.")


# ═══════════════════════════════════════════════════════════════════════
# Reverb type presets. Keys are semantic user-facing params; set_type()
# maps them onto whichever backend is active (FDN or plate). When both
# shimmer_fb and noise_mod are 0 the FDN sound is mathematically identical
# to the original WASH.
# ═══════════════════════════════════════════════════════════════════════
REVERB_PRESETS = {
    # Classic FDN wash — the sound we shipped. Don't touch these values.
    "wash":  {"backend": "fdn",   "decay_seconds": 6.0,  "predelay_ms": 25.0, "low_cut_hz": 80,  "high_cut_hz": 7000,  "damp": 0.50, "er_scale": 0.4, "shimmer_fb": 0.0, "noise_mod": 0.0},
    "hall":  {"backend": "fdn",   "decay_seconds": 9.0,  "predelay_ms": 45.0, "low_cut_hz": 120, "high_cut_hz": 8500,  "damp": 0.35, "er_scale": 0.6, "shimmer_fb": 0.0, "noise_mod": 0.0},
    "room":  {"backend": "fdn",   "decay_seconds": 1.5,  "predelay_ms": 8.0,  "low_cut_hz": 200, "high_cut_hz": 10000, "damp": 0.70, "er_scale": 0.8, "shimmer_fb": 0.0, "noise_mod": 0.0},
    "plate": {"backend": "plate", "decay_seconds": 3.0,  "predelay_ms": 5.0,  "low_cut_hz": 150, "high_cut_hz": 11000, "damp": 0.30},
    "bloom": {"backend": "fdn",   "decay_seconds": 7.0,  "predelay_ms": 30.0, "low_cut_hz": 150, "high_cut_hz": 7000,  "damp": 0.55, "er_scale": 0.3, "shimmer_fb": 0.35, "noise_mod": 0.0},
    # DRONE — dedicated resonator topology (4 tuned high-Q bandpasses in
    # parallel, no cross-feedback, no FDN modulation). Root at A2 (110Hz) so
    # the drone sits as a bass pedal; low_cut dropped to 50Hz to let the
    # fundamental through. decay_seconds maps via _drone_fb_from_seconds.
    "drone": {"backend": "drone", "decay_seconds": 10.0, "predelay_ms": 15.0, "low_cut_hz": 50, "high_cut_hz": 4000, "damp": 0.30, "drone_key": 0},
    # GHOST: heavier noise mod than initially shipped so the tail clearly
    # "breathes" — differentiates it from DRONE (which is a pitched drone
    # now, not a modulated wash).
    "ghost": {"backend": "fdn",   "decay_seconds": 8.0,  "predelay_ms": 20.0, "low_cut_hz": 100, "high_cut_hz": 6500,  "damp": 0.50, "er_scale": 0.3, "shimmer_fb": 0.0, "noise_mod": 0.70},
}


def _plate_decay_from_seconds(seconds: float) -> float:
    """Map decay_seconds (shared UI param) → Dattorro plate decay coefficient
    (0..0.97). Not the same mapping as FDN feedback because plate topology
    has a fundamentally different loop gain structure. Tuned by ear: seconds
    value roughly matches subjective RT60."""
    return float(max(0.0, min(0.97, 0.35 + seconds * 0.08)))


def _drone_fb_from_seconds(seconds: float) -> float:
    """Map decay_seconds → per-resonator feedback for the drone topology.
    Reference: at the root freq (110 Hz A2), sustain_seconds ≈
    -3 / (log10(fb) × freq). Capped at ~0.999 so the longest sustain tops
    out around 13 s — long enough to feel infinite musically, short enough
    that notes can clear without panic/freeze. (Lower ref freq ⇒ slower
    cycles ⇒ fb needs to be a touch higher for same sustain time.)"""
    if seconds <= 0.05:
        return 0.0
    fb = 10.0 ** (-3.0 / (seconds * 110.0))
    return float(min(fb, 0.999))


class FaustReverb:
    """Public API mirrors FeedbackDelayReverb. Internally backed by the Faust DSP."""

    def __init__(self, decay_seconds: float = 6.0, sample_rate: int = 48000):
        self.sample_rate = int(sample_rate)
        self._dsp = _lib.newStaveReverb()
        if self._dsp == _ffi.NULL:
            raise RuntimeError("newStaveReverb() returned NULL")
        _lib.initStaveReverb(self._dsp, self.sample_rate)

        # Mirror Python state for parity with FeedbackDelayReverb callers
        self.dry_wet = 0.75
        self.wet_gain = 1.0
        self.decay_seconds = decay_seconds
        self.low_cut_hz = 80.0
        self.high_cut_hz = 7000.0
        self.space = 0.0
        self.predelay_ms = 25.0
        self.frozen = False

        # Discover parameter zones by calling buildUserInterface with a
        # capturing UIGlue. Callbacks must stay alive for the lifetime of
        # the object — store them on self to prevent GC.
        self._zones: dict[str, _ffi.CData] = {}
        self._keepalive = _install_ui_callbacks(self._dsp, self._zones)

        # Scratch float32 buffers for the FFI boundary; resize as needed
        self._buf_n = 0
        self._in_l_f32 = np.empty(0, dtype=np.float32)
        self._in_r_f32 = np.empty(0, dtype=np.float32)
        self._out_l_f32 = np.empty(0, dtype=np.float32)
        self._out_r_f32 = np.empty(0, dtype=np.float32)
        self._in_ptrs = _ffi.new("float*[2]")
        self._out_ptrs = _ffi.new("float*[2]")

        # Feedback smoothing is now handled inside Faust (si.smoo); Python
        # only computes the target feedback value from decay_seconds.
        self._normal_feedback = 0.0
        self._normal_damp = 0.5
        self._feedback_target = 0.9
        self._damp_target = 0.5
        self._freeze_capture_remaining = 0

        # Type switching (v1: hard-swap backend, no crossfade). Plate + drone
        # are lazy — first time each is selected, the .so loads.
        # Must initialise before the set_*() calls below because they read
        # self._plate / self._drone.
        self.type = "wash"
        self._plate: "FaustPlate | None" = None
        self._drone: "FaustDrone | None" = None

        # Probe plate/drone .so availability up-front so set_type() can fall
        # back gracefully without crashing the audio thread on first user-tap.
        # We import the modules but don't instantiate (saves RAM until used);
        # an OSError from dlopen happens at module-import time so a successful
        # import here means the .so loaded.
        self.plate_available = False
        self.drone_available = False
        try:
            from . import faust_plate  # noqa: F401
            self.plate_available = True
        except Exception as e:
            logger.warning("plate reverb unavailable (%s); PLATE will fall back to WASH", e)
        try:
            from . import faust_drone  # noqa: F401
            self.drone_available = True
        except Exception as e:
            logger.warning("drone reverb unavailable (%s); DRONE will fall back to WASH", e)

        self.set_decay(decay_seconds)
        self.set_low_cut(self.low_cut_hz)
        self.set_high_cut(self.high_cut_hz)
        self.set_predelay(self.predelay_ms)

    def available_types(self) -> dict[str, bool]:
        """Return availability map for every reverb type. main.py broadcasts
        this so the UI can grey-out PLATE / DRONE when their .so failed to
        load. WASH/HALL/ROOM/BLOOM/GHOST always run on the FDN backend."""
        return {
            "wash": True,
            "hall": True,
            "room": True,
            "bloom": True,
            "ghost": True,
            "plate": self.plate_available,
            "drone": self.drone_available,
        }

    # ─────────────── lifecycle ───────────────
    def __del__(self):
        try:
            if getattr(self, "_dsp", None):
                _lib.deleteStaveReverb(self._dsp)
                self._dsp = None
        except Exception:
            pass

    def panic(self):
        """Flush all internal state. Mirrors FeedbackDelayReverb.panic()."""
        self.frozen = False
        self._feedback_target = self._normal_feedback if self._normal_feedback else self._feedback_target
        self._damp_target = self._normal_damp
        self._freeze_capture_remaining = 0
        _set_zone(self._zones, "feedback", self._feedback_target)
        _set_zone(self._zones, "damp", self._damp_target)
        _set_zone(self._zones, "freeze_input", 1.0)
        _set_zone(self._zones, "er_scale", 0.4)
        _lib.instanceClearStaveReverb(self._dsp)

    # ─────────────── parameter setters ───────────────
    def set_decay(self, seconds: float):
        self.decay_seconds = seconds
        if seconds > 0:
            # Same loop-gain-per-second formula as Python implementation
            delay_times_ms = [63.7, 79.3, 95.3, 111.7, 131.9, 153.1, 177.7, 200.9]
            avg_delay = sum(delay_times_ms) * self.sample_rate / 1000.0 / len(delay_times_ms)
            loops_per_sec = self.sample_rate / avg_delay
            fb = 10.0 ** (-3.0 / (seconds * loops_per_sec))
            # DRONE type intentionally runs near self-oscillation for sustained
            # resonance. Cap higher (0.9985) when drone is active so decay_seconds
            # actually translates to perceptible multi-second tails.
            fb_cap = 0.9985 if getattr(self, "type", "wash") == "drone" else 0.985
            fb = min(fb, fb_cap)
        else:
            fb = 0.0
        self._feedback_target = fb
        if not self.frozen:
            _set_zone(self._zones, "feedback", fb)
        # Drone backend uses its own fb mapping (resonator topology, not FDN
        # delay-loop math). Sync whenever the user moves the Decay slider.
        if self._drone is not None:
            self._drone.set_zone("feedback", _drone_fb_from_seconds(seconds))

    def set_low_cut(self, freq_hz: float):
        self.low_cut_hz = max(20.0, float(freq_hz))
        _set_zone(self._zones, "low_cut_hz", self.low_cut_hz)

    def set_high_cut(self, freq_hz: float):
        self.high_cut_hz = min(20000.0, float(freq_hz))
        _set_zone(self._zones, "high_cut_hz", self.high_cut_hz)

    def set_predelay(self, ms: float):
        self.predelay_ms = max(0.0, min(150.0, float(ms)))
        _set_zone(self._zones, "predelay_ms", self.predelay_ms)
        if self._plate is not None:
            self._plate.set_zone("predelay_ms", self.predelay_ms)
        if self._drone is not None:
            self._drone.set_zone("predelay_ms", self.predelay_ms)

    def set_type(self, name: str):
        """Apply a reverb-type preset. 'plate' and 'drone' lazy-load dedicated
        .so files and process() routes through them. Other types stay on the
        FDN with param variants (shimmer_fb/noise_mod pick up BLOOM/GHOST).

        If the requested type's .so is unavailable, falls back to WASH and
        logs a warning rather than crashing the audio thread."""
        preset = REVERB_PRESETS.get(name)
        if preset is None:
            logger.warning("Unknown reverb type %r — keeping %r", name, self.type)
            return

        backend = preset["backend"]

        # Guard plate/drone against missing .so — fall back to WASH.
        if backend == "plate" and not self.plate_available:
            logger.warning("PLATE reverb unavailable — falling back to WASH")
            name = "wash"
            preset = REVERB_PRESETS["wash"]
            backend = preset["backend"]
        elif backend == "drone" and not self.drone_available:
            logger.warning("DRONE reverb unavailable — falling back to WASH")
            name = "wash"
            preset = REVERB_PRESETS["wash"]
            backend = preset["backend"]

        # Canonical shared params (all backends understand these zones).
        self.set_predelay(preset["predelay_ms"])
        self.set_low_cut(preset["low_cut_hz"])
        self.set_high_cut(preset["high_cut_hz"])

        damp = preset.get("damp", 0.5)
        self._damp_target = damp
        if not self.frozen:
            _set_zone(self._zones, "damp", damp)

        # FDN-only modifiers. Plate/drone ignore them (zones exist for parity).
        _set_zone(self._zones, "er_scale", preset.get("er_scale", 0.4))
        _set_zone(self._zones, "shimmer_fb", preset.get("shimmer_fb", 0.0))
        _set_zone(self._zones, "noise_mod", preset.get("noise_mod", 0.0))

        # Feedback / decay handling splits by backend:
        #   FDN  → compute feedback from decay_seconds via loop formula
        #   Plate → compute Dattorro decay via plate-specific mapping
        #   Drone → use preset's direct `feedback` (resonator loop gain)
        if backend == "drone":
            # Lazy-load drone engine on first selection.
            if self._drone is None:
                try:
                    from .faust_drone import FaustDrone
                    self._drone = FaustDrone(self.sample_rate)
                    logger.info("drone reverb lazy-loaded (libstave_drone.so)")
                except Exception as e:
                    logger.warning("drone reverb load failed (%s) — falling back to WASH", e)
                    self.drone_available = False
                    name = "wash"
                    preset = REVERB_PRESETS["wash"]
                    backend = preset["backend"]
            if self._drone is not None:
                self._drone.set_zone("drone_key", float(preset.get("drone_key", 0)))
                self._drone.set_zone("predelay_ms", self.predelay_ms)
                self._drone.set_zone("low_cut_hz", self.low_cut_hz)
                self._drone.set_zone("high_cut_hz", self.high_cut_hz)
                self._drone.set_zone("damp", damp)
            # set_decay owns the fb mapping; track the preset's seconds value.
            self.set_decay(preset["decay_seconds"])
        else:
            # FDN / plate variants take decay_seconds.
            self.set_decay(preset["decay_seconds"])

        if backend == "plate":
            if self._plate is None:
                try:
                    from .faust_plate import FaustPlate
                    self._plate = FaustPlate(self.sample_rate)
                    logger.info("plate reverb lazy-loaded (libstave_plate.so)")
                except Exception as e:
                    logger.warning("plate reverb load failed (%s) — falling back to WASH", e)
                    self.plate_available = False
                    name = "wash"
                    backend = "fdn"

        # Mirror common params into plate so switching back feels instant.
        if self._plate is not None:
            self._plate.set_zone("predelay_ms", self.predelay_ms)
            self._plate.set_zone("low_cut_hz", self.low_cut_hz)
            self._plate.set_zone("high_cut_hz", self.high_cut_hz)
            self._plate.set_zone("damp", damp)
            if "decay_seconds" in preset:
                self._plate.set_zone("feedback",
                                     _plate_decay_from_seconds(preset["decay_seconds"]))

        self.type = name
        logger.info("reverb type: %s (backend=%s)", name, backend)

    def set_space(self, value: float):
        self.space = max(0.0, min(1.0, float(value)))
        # Shuffler runs in JackEngine on master bus; parity-only mirror.

    def set_freeze(self, enabled: bool):
        if enabled and not self.frozen:
            self._normal_feedback = self._feedback_target
            self._normal_damp = self._damp_target
            self._feedback_target = 0.999
            self._damp_target = 0.05
            self._freeze_capture_remaining = int(2.0 * self.sample_rate)
            self.frozen = True
            _set_zone(self._zones, "feedback", 0.999)
            _set_zone(self._zones, "damp", 0.05)
            _set_zone(self._zones, "er_scale", 0.0)
            # freeze_input stays open during capture window
        elif not enabled and self.frozen:
            self._feedback_target = self._normal_feedback
            self._damp_target = self._normal_damp
            self.frozen = False
            _set_zone(self._zones, "feedback", self._normal_feedback)
            _set_zone(self._zones, "damp", self._normal_damp)
            _set_zone(self._zones, "freeze_input", 1.0)
            _set_zone(self._zones, "er_scale", 0.4)

    # ─────────────── main process ───────────────
    def process(self, samples: np.ndarray) -> np.ndarray:
        """Stereo (2, n) float64 → stereo (2, n) float64 wet output."""
        if samples.ndim == 2:
            input_l = samples[0]
            input_r = samples[1]
            n = input_l.shape[0]
        else:
            input_l = samples
            input_r = samples
            n = samples.shape[0]

        if n == 0:
            return np.zeros((2, 0), dtype=np.float64)

        # Freeze capture-window bookkeeping — seal loop when timer elapses
        if self.frozen and self._freeze_capture_remaining > 0:
            self._freeze_capture_remaining -= n
            if self._freeze_capture_remaining <= 0:
                _set_zone(self._zones, "freeze_input", 0.0)

        # Resize scratch if block size changed
        if n != self._buf_n:
            self._in_l_f32 = np.empty(n, dtype=np.float32)
            self._in_r_f32 = np.empty(n, dtype=np.float32)
            self._out_l_f32 = np.empty(n, dtype=np.float32)
            self._out_r_f32 = np.empty(n, dtype=np.float32)
            self._in_ptrs[0] = _ffi.cast("float*", self._in_l_f32.ctypes.data)
            self._in_ptrs[1] = _ffi.cast("float*", self._in_r_f32.ctypes.data)
            self._out_ptrs[0] = _ffi.cast("float*", self._out_l_f32.ctypes.data)
            self._out_ptrs[1] = _ffi.cast("float*", self._out_r_f32.ctypes.data)
            self._buf_n = n

        # Route to plate or drone if active; the FDN always runs but we don't
        # use its output during those types (keeps switching back instant).
        if self.type == "plate" and self._plate is not None:
            return self._plate.process(samples)
        if self.type == "drone" and self._drone is not None:
            return self._drone.process(samples)

        np.copyto(self._in_l_f32, input_l, casting="unsafe")
        np.copyto(self._in_r_f32, input_r, casting="unsafe")

        _lib.computeStaveReverb(self._dsp, n, self._in_ptrs, self._out_ptrs)

        out = np.empty((2, n), dtype=np.float64)
        np.copyto(out[0], self._out_l_f32, casting="unsafe")
        np.copyto(out[1], self._out_r_f32, casting="unsafe")
        return out


# ────────────────────────────────────────────────────────────────────────
# Internal helpers — UI callback glue
# ────────────────────────────────────────────────────────────────────────

def _install_ui_callbacks(dsp, zones: dict):
    """Call buildUserInterfaceStaveReverb with a capturing UIGlue; populate
    `zones` mapping label → float* zone pointer. Returns keepalive refs."""
    keepalive = []

    @_ffi.callback("void(void*, const char*)")
    def _open(ui, label):  # noqa: ARG001
        pass

    @_ffi.callback("void(void*)")
    def _close(ui):  # noqa: ARG001
        pass

    @_ffi.callback("void(void*, const char*, float*)")
    def _button(ui, label, zone):  # noqa: ARG001
        zones[_ffi.string(label).decode()] = zone

    @_ffi.callback(
        "void(void*, const char*, float*, float, float, float, float)"
    )
    def _slider(ui, label, zone, init, lo, hi, step):  # noqa: ARG001
        zones[_ffi.string(label).decode()] = zone

    @_ffi.callback("void(void*, const char*, float*, float, float)")
    def _bar(ui, label, zone, lo, hi):  # noqa: ARG001
        pass

    @_ffi.callback("void(void*, const char*, const char*, void**)")
    def _sf(ui, label, url, sf):  # noqa: ARG001
        pass

    @_ffi.callback("void(void*, float*, const char*, const char*)")
    def _decl(ui, zone, key, value):  # noqa: ARG001
        pass

    glue = _ffi.new("UIGlue*")
    glue.uiInterface = _ffi.NULL
    glue.openTabBox = _open
    glue.openHorizontalBox = _open
    glue.openVerticalBox = _open
    glue.closeBox = _close
    glue.addButton = _button
    glue.addCheckButton = _button
    glue.addVerticalSlider = _slider
    glue.addHorizontalSlider = _slider
    glue.addNumEntry = _slider
    glue.addHorizontalBargraph = _bar
    glue.addVerticalBargraph = _bar
    glue.addSoundfile = _sf
    glue.declare = _decl

    _lib.buildUserInterfaceStaveReverb(dsp, glue)

    # Keep callbacks + glue alive
    keepalive.extend([_open, _close, _button, _slider, _bar, _sf, _decl, glue])
    return keepalive


def _set_zone(zones: dict, label: str, value: float):
    z = zones.get(label)
    if z is None:
        logger.warning("FaustReverb: zone %r not found in DSP", label)
        return
    z[0] = float(value)
