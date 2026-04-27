"""JACK audio engine via C bridge — handles audio output and MIDI input.

python-jack-client's CFFI buffer writing is broken on aarch64/Pi 5.
This module uses a small C shared library (jack_bridge.so) loaded via ctypes
that handles the JACK process callback natively.
"""

import ctypes
import logging
import math
import os
import threading
import time

import numpy as np

from .synth_engine import (SynthEngine, BiquadLowpass, BiquadHighpass,
                           BiquadPeakingEQ, BiquadLowShelf, OnePole6dBHighpass,
                           BusCompressor,
                           fader_to_amplitude, blend_to_amplitude)
from .config import SAMPLE_RATE, BTL_MODE
USE_FAUST_MASTER_FX = os.environ.get("STAVE_FAUST_MASTER_FX", "0") not in ("0", "", "false", "False")
USE_FAUST_BUS_COMP  = os.environ.get("STAVE_FAUST_BUS_COMP",  "0") not in ("0", "", "false", "False")
from .recorder import Recorder

logger = logging.getLogger(__name__)

# Locate the C bridge shared library next to this file
_BRIDGE_PATH = os.path.join(os.path.dirname(__file__), "jack_bridge.so")


class LookaheadLimiter:
    """Brickwall peak limiter with lookahead — stereo-linked.

    Maintains |output| ≤ `ceiling` transparently. Introduces `lookahead_ms` of
    fixed delay (default 1.5 ms ≈ 72 samples at 48 kHz — imperceptible for
    live keyboard work) but preserves transients that a naive tanh soft-clip
    would squash. Gain is derived from max(|L|, |R|) so the stereo image
    doesn't collapse under limiting.

    Replaces the old `np.tanh` soft-clip that was secretly relying on the
    C-bridge hard clipper to do the actual ceiling — any peak above ~0.76
    used to lose transient energy to tanh compression before the bridge
    ever saw it.
    """

    def __init__(self, sample_rate, lookahead_ms=1.5, release_ms=60.0, ceiling=0.98):
        la = max(2, int(round(sample_rate * lookahead_ms / 1000.0)))
        self.lookahead = la
        self.ceiling = float(ceiling)
        # Per-sample release coefficient, exponential toward 1.0 (transparent).
        # 60 ms lets the limiter recover from a peak without pumping sustained
        # material.
        self.release = float(np.exp(-1.0 / (sample_rate * release_ms * 0.001)))
        self._dl = np.zeros(la, dtype=np.float64)
        self._dr = np.zeros(la, dtype=np.float64)
        self._dp = np.zeros(la, dtype=np.float64)
        self._gain = 1.0

    def process_inplace(self, stereo):
        """Limit stereo[0]/stereo[1] in place. stereo shape = (2, n).

        The output is the input delayed by `lookahead` samples with gain
        applied. The first `lookahead` samples emitted right after a reset
        are silence (zero-padded delay line) — acceptable as startup latency.
        """
        n = stereo.shape[1]
        if n == 0:
            return
        la = self.lookahead
        ceiling = self.ceiling
        rel = self.release

        # Stereo-linked peak detect: gain reduction is driven by max(|L|, |R|)
        # so the stereo image doesn't collapse under limiting.
        peak_in = np.maximum(np.abs(stereo[0]), np.abs(stereo[1]))

        # Full stream = stored delay-line tail + incoming samples.
        # For output index i (length n), the sample being emitted was input
        # `la` samples ago; it lives at full[i] and we look ahead `la` more
        # samples to anticipate peaks.
        full_l = np.concatenate([self._dl, stereo[0]])
        full_r = np.concatenate([self._dr, stereo[1]])
        full_p = np.concatenate([self._dp, peak_in])

        # Rolling max over the lookahead window for each output sample.
        from numpy.lib.stride_tricks import sliding_window_view
        windowed = sliding_window_view(full_p, la + 1)  # shape (n, la+1)
        ahead = windowed.max(axis=1)

        # Target gain per sample: scale peaks above ceiling down to ceiling.
        target = np.where(ahead > ceiling,
                          ceiling / np.maximum(ahead, 1e-9),
                          1.0)

        # Envelope: instant attack (gain snaps to target), exponential release.
        # The `la`-sample lookahead means the gain reduction is already in
        # place by the time the actual peak is emitted — no distortion.
        env = np.empty(n, dtype=np.float64)
        gain = self._gain
        one_m_rel = 1.0 - rel
        for i in range(n):
            t = target[i]
            if t < gain:
                gain = t
            else:
                gain = rel * gain + one_m_rel
            env[i] = gain
        self._gain = gain

        # Apply envelope to the DELAYED signal (first n entries of full buffer).
        stereo[0] = full_l[:n] * env
        stereo[1] = full_r[:n] * env

        # Retain the last `la` samples for the next call.
        self._dp[:] = full_p[-la:]
        self._dl[:] = full_l[-la:]
        self._dr[:] = full_r[-la:]

    def reset(self):
        """Flush delay lines and release gain reduction. Used on panic so the
        next note doesn't inherit a pumped-down state from a pre-panic peak."""
        self._dl[:] = 0.0
        self._dr[:] = 0.0
        self._dp[:] = 0.0
        self._gain = 1.0


class JackEngine:
    """Manages JACK audio via C bridge with MIDI input."""

    def __init__(self, synth: SynthEngine, midi_callback=None, piano_callback=None,
                 piano_player=None, cc_callback=None, program_change_callback=None):
        self.synth = synth
        self.midi_callback = midi_callback
        self.piano_callback = piano_callback
        self.cc_callback = cc_callback  # Called with (cc_number, value_0_to_127)
        # Called with one int (program 0..127) on MIDI 0xC0. Wired in main.py
        # to load that index from the active setlist (footswitch advance).
        self.program_change_callback = program_change_callback
        self.piano_player = piano_player  # FluidSynthPlayer for rendered mixing
        self.running = False
        # Last render-loop iteration timestamp (perf_counter). Updated every
        # iteration once the render thread starts; read by the systemd
        # watchdog heartbeat in main.py to detect a wedged audio loop.
        self.last_iter_ts = 0.0

        # Render-ahead ring fill threshold. Matches NORMAL mode (8 = refill
        # when fewer than 8 of 16 slots are queued). set_low_latency_mode()
        # updates this and the C-side g_active_slots together.
        self.ring_threshold = 8

        # Master volume and transpose (set from outside)
        self._master_volume = 0.85
        self._fade_gain = 1.0  # multiplied with master before bridge; 0.0 = silent
        self._fade_target = 1.0  # 1.0 = normal, 0.0 = faded out
        self._fade_thread = None
        self._fade_cancel = threading.Event()
        # Pre-gain into the master comp + limiter. Dropped from 2.0 (+6 dB)
        # to 1.5 (+3.5 dB) in Review #7 — the old value was calibrated
        # against a ~50 % Pi system-volume ghost that used to silently
        # attenuate the output. With the ART USB DI honouring system volume
        # at 100 %, 2.0 was +6 dB hot into the limiter and audibly squashing
        # transients on piano chords.
        self.pre_gain = 1.5
        self.transpose = 0
        self.piano_octave = 0  # -3 to +3 octaves for piano

        # Reverb send for piano/organ bus — 0..1. At 0, piano/organ stays dry
        # (current default). Above 0, a copy is mixed into the pad's reverb
        # input so the selected reverb type tails out the piano/organ too.
        self.piano_reverb_send = 0.0
        # Piano/organ also tap into the ping-pong delay engine when > 0.
        self.piano_delay_send = 0.0

        # MIDI filtering
        self.min_velocity = 10

        # Active note tracking for sympathetic resonance + drone
        self._piano_notes_active = set()  # piano note numbers currently sounding
        self._pad_notes_active = set()    # pad note numbers currently sounding

        # Sustain pedal state — tracks by raw MIDI note so transpose changes
        # between note-on and note-off can't cause hung notes
        self._sustain_on = False
        self._physically_held = set()    # raw MIDI notes with finger on key
        self._sustained_notes = set()    # raw MIDI notes held by sustain pedal
        self._note_map = {}              # raw_note → (pad_note, piano_note)

        # Sostenuto pedal (CC66): captures notes held when pedal is pressed,
        # sustains only those — later notes pass through normally.
        self._sostenuto_on = False
        self._sostenuto_held = set()

        # Kill-switch for MIDI pitch bend (0xE0). MPK-mini-class touch joysticks
        # leave the pitch detuned with no easy way to clear; user toggles this
        # off in Global tab to ignore pitch bend entirely.
        self.pitch_bend_enabled = True

        # MIDI clock follow (0xF8). Off by default — many keyboards spam clock
        # even when you don't want tempo locking. When on: measure inter-tick
        # interval (24 ticks/quarter), low-pass it, push BPM to jack engine
        # so delays/LFOs sync to the band.
        self.midi_clock_enabled = False
        self._midi_clock_last_ts = 0.0
        self._midi_clock_avg_dt = 0.0   # smoothed seconds-per-tick
        self._midi_clock_count = 0      # ticks seen since last BPM update

        # LAYER (split) state — instrument range applies to whichever of
        # piano/organ is active via instrument_mode. OSC1/OSC2/shimmer ranges
        # live on the synth engine itself.
        self.split_enabled = False
        self.instrument_split_low = 0
        self.instrument_split_high = 127
        self.instrument_split_xfade = 0

        # Organ/piano shared filter (tracks synth's main filter when enabled)
        self.organ_filter_enabled = False
        self._organ_filter_l = BiquadLowpass(8000.0, 0.707, SAMPLE_RATE)
        self._organ_filter_r = BiquadLowpass(8000.0, 0.707, SAMPLE_RATE)
        self._organ_filter2_l = BiquadLowpass(8000.0, 0.707, SAMPLE_RATE)
        self._organ_filter2_r = BiquadLowpass(8000.0, 0.707, SAMPLE_RATE)

        # Master parametric EQ (3 configurable bands, applied after pad+piano mix)
        self._master_eq_l = [
            BiquadPeakingEQ(200.0, 0.0, 1.5, SAMPLE_RATE),
            BiquadPeakingEQ(1000.0, 0.0, 1.5, SAMPLE_RATE),
            BiquadPeakingEQ(5000.0, 0.0, 1.5, SAMPLE_RATE),
        ]
        self._master_eq_r = [
            BiquadPeakingEQ(200.0, 0.0, 1.5, SAMPLE_RATE),
            BiquadPeakingEQ(1000.0, 0.0, 1.5, SAMPLE_RATE),
            BiquadPeakingEQ(5000.0, 0.0, 1.5, SAMPLE_RATE),
        ]
        self._master_eq_active = False  # skip processing when all bands flat

        # Master low cut (highpass) — optional, pre-limiter
        self._master_hp_enabled = False
        self._master_hp_slope = 12
        self._master_hp6_l = OnePole6dBHighpass(80.0, SAMPLE_RATE)
        self._master_hp6_r = OnePole6dBHighpass(80.0, SAMPLE_RATE)
        self._master_hp12_l = BiquadHighpass(80.0, 0.707, SAMPLE_RATE)
        self._master_hp12_r = BiquadHighpass(80.0, 0.707, SAMPLE_RATE)
        self._master_hp24_l = [BiquadHighpass(80.0, 0.707, SAMPLE_RATE),
                               BiquadHighpass(80.0, 0.707, SAMPLE_RATE)]
        self._master_hp24_r = [BiquadHighpass(80.0, 0.707, SAMPLE_RATE),
                               BiquadHighpass(80.0, 0.707, SAMPLE_RATE)]

        # SSL Fusion-style stereo shuffler (Blumlein) — low-shelf on side signal.
        # Crossover 250 Hz: inside SSL's documented 40-400 Hz range, targets
        # only true bass so low-mid body (400 Hz-1 kHz) isn't pumped by the
        # reverb's slow internal modulation. Runs pre-EQ/pre-low-cut.
        self._shuffler_shelf = BiquadLowShelf(250.0, 0.0, 0.707, SAMPLE_RATE)
        self._shuffler_space_last = -1.0

        # Saturation (SAT button): optional asymmetric drive before the
        # master limiter. Off by default — the limiter alone is the clean path.
        self.saturation_enabled = False
        self._sat_scratch = np.empty((2, 512), dtype=np.float64)
        # DC block after SAT — the asymmetric drive injects a rectified
        # positive bias (+|x|·0.09) that eats ~1-2 dB of positive-side
        # headroom at the limiter. A 15 Hz one-pole HP strips the DC
        # transparently without touching program content.
        self._sat_dc_l = OnePole6dBHighpass(15.0, SAMPLE_RATE)
        self._sat_dc_r = OnePole6dBHighpass(15.0, SAMPLE_RATE)

        # Brickwall lookahead limiter — replaces the old np.tanh soft-clip.
        # 1.5 ms lookahead keeps transients clean; the bridge hard-clip at
        # ±1.0 remains as belt-and-suspenders if the limiter ever undershoots.
        self._limiter = LookaheadLimiter(
            SAMPLE_RATE, lookahead_ms=1.5, release_ms=60.0, ceiling=0.98,
        )

        # Pre-allocated scratch buffers for the master chain. Sized once at
        # startup and resized-in-place if block size ever grows. Eliminates
        # per-block allocations of piano×send and stereo.astype(float32) that
        # previously went through the glibc allocator every 5 ms at 256-frame
        # blocks (several hundred KB/s pressure on the GC/malloc pool).
        self._send_scratch = np.empty((2, 512), dtype=np.float64)
        self._f32_scratch_l = np.empty(512, dtype=np.float32)
        self._f32_scratch_r = np.empty(512, dtype=np.float32)

        # ═══ Bus compressor (SSL G-style) ═══
        # Sits on the master bus pre-saturation, pre-limiter. Sources:
        # "self" (feedback), "piano" (piano mix), "lfo" (from synth LFO),
        # "bpm" (synthesized pulse train at beat rate).
        self.bus_comp = BusCompressor(SAMPLE_RATE)
        self.bus_comp_source = "self"
        self.bus_comp_fx_bypass = False  # if True, reverb/delay bypass the comp
        self.bus_comp_retrigger = False  # if True, pad note-on retriggers BPM pulse phase

        self._faust_master_fx = None
        if USE_FAUST_MASTER_FX:
            try:
                from .faust_master_fx import FaustMasterFX
                self._faust_master_fx = FaustMasterFX(SAMPLE_RATE)
                logger.info("master fx: Faust native path (STAVE_FAUST_MASTER_FX=1)")
            except Exception as e:
                logger.warning("master fx: Faust init failed (%s); falling back", e)

        self._faust_bus_comp = None
        if USE_FAUST_BUS_COMP:
            try:
                from .faust_bus_comp import FaustBusComp
                self._faust_bus_comp = FaustBusComp(SAMPLE_RATE)
                logger.info("bus comp: Faust native path (STAVE_FAUST_BUS_COMP=1) "
                            "— self-sidechain only; piano/lfo/bpm modes fall back to Python")
            except Exception as e:
                logger.warning("bus comp: Faust init failed (%s); falling back", e)
        self._sc_scratch = np.empty(512, dtype=np.float64)
        # BPM pulse generator state
        self._bpm_beat_phase = 0.0     # 0..1 within current beat
        self._bpm_pulse_remaining = 0  # samples left in current pulse envelope

        # Debug counters
        self._callback_count = 0
        self._peak_output = 0.0
        self._pad_peak = 0.0
        self._piano_peak = 0.0
        self._piano_renders = 0

        # Recorder — master-output WAV writer. Audio thread calls
        # self.recorder.feed(l, r) each render block while recording.
        self.recorder = Recorder(sample_rate=SAMPLE_RATE)
        self._last_error = None
        self._last_traceback = None
        self._midi_events_seen = 0
        self._midi_notes_triggered = 0

        # Load C bridge
        if not os.path.exists(_BRIDGE_PATH):
            raise RuntimeError(
                f"jack_bridge.so not found at {_BRIDGE_PATH}. "
                "Run: gcc -shared -fPIC -O2 -o jack_bridge.so jack_bridge.c -ljack -lpthread"
            )
        self._bridge = ctypes.CDLL(_BRIDGE_PATH)
        self._setup_bridge_types()

    @property
    def master_volume(self):
        return self._master_volume

    @master_volume.setter
    def master_volume(self, val):
        self._master_volume = val
        self._push_master_to_bridge()

    def _push_master_to_bridge(self):
        if hasattr(self, '_bridge'):
            amp = fader_to_amplitude(self._master_volume) * self._fade_gain
            self._bridge.bridge_set_master_volume(ctypes.c_float(amp))

    def start_fade(self, target: float, duration_s: float = 5.0) -> float:
        """Ramp fade_gain to target (0.0 or 1.0) over duration_s with an S-curve.
        Cancels any running fade and continues smoothly from current gain.
        Returns the requested target. The JACK bridge's own 5ms smoother
        handles sample-level zipper-free ramps between our ~20ms updates."""
        target = max(0.0, min(1.0, float(target)))
        self._fade_target = target
        # Cancel prior fade
        self._fade_cancel.set()
        if self._fade_thread and self._fade_thread.is_alive():
            self._fade_thread.join(timeout=0.5)
        cancel = threading.Event()
        self._fade_cancel = cancel
        start_gain = self._fade_gain

        def run():
            steps = max(1, int(duration_s * 50))  # 20ms steps
            step_sec = duration_s / steps
            span = target - start_gain
            for i in range(1, steps + 1):
                if cancel.is_set():
                    return
                t = i / steps
                # Raised-cosine S-curve: smooth ease-in/ease-out
                progress = (1.0 - math.cos(t * math.pi)) * 0.5
                self._fade_gain = start_gain + span * progress
                self._push_master_to_bridge()
                time.sleep(step_sec)
            if not cancel.is_set():
                self._fade_gain = target
                self._push_master_to_bridge()

        self._fade_thread = threading.Thread(target=run, daemon=True)
        self._fade_thread.start()
        return target

    def fade_reset(self):
        """Snap fade to 1.0 immediately (used by panic)."""
        self._fade_cancel.set()
        self._fade_gain = 1.0
        self._fade_target = 1.0
        self._push_master_to_bridge()

    def set_bpm(self, bpm: float):
        """Pass BPM to the synth engine so tempo-synced delay re-computes times."""
        if self.synth:
            self.synth.update_params({"bpm": float(bpm)})

    def set_low_latency_mode(self, enabled: bool):
        """Switch ring depth between Low Latency (6/3) and Normal (16/8).
        The C bridge briefly mutes (~20 ms) while the ring is reset."""
        if enabled:
            slots, threshold = 6, 3
            label = "ON — ring 6/3 (~16ms render-ahead)"
        else:
            slots, threshold = 16, 8
            label = "OFF — ring 16/8 (~43ms render-ahead)"
        try:
            ret = self._bridge.bridge_set_ring_slots(slots)
            if ret != 0:
                logger.warning("bridge_set_ring_slots(%d) returned %d", slots, ret)
                return
        except Exception as e:
            logger.warning("bridge_set_ring_slots failed: %s", e)
            return
        self.ring_threshold = threshold
        logger.info("Low Latency Mode %s", label)

    def _warm_dsp_state(self, n_blocks: int = 16):
        """Push ~85ms of low-level noise through every stateful filter so the
        first real keystroke doesn't excite a cold filter/compressor from DC
        and produce an audible transient. Runs in the main thread before the
        render thread starts — the bridge is not written to."""
        try:
            bs = 256
            rng = np.random.default_rng(42)
            s = self.synth
            for _ in range(n_blocks):
                nl = rng.uniform(-0.01, 0.01, bs).astype(np.float64)
                nr = rng.uniform(-0.01, 0.01, bs).astype(np.float64)
                # SynthEngine filters (probe by name; structure varies with version)
                for name in ("_filter_l", "_filter_r", "_filter2_l", "_filter2_r",
                             "_shimmer_hp"):
                    f = getattr(s, name, None)
                    if f is not None and hasattr(f, "process"):
                        f.process(nr if name.endswith("_r") else nl)
                # Reverb internal biquads (names may vary; probe gracefully)
                rv = getattr(s, "reverb", None)
                if rv is not None:
                    for name in ("_hp_l", "_hp_r", "_lp_l", "_lp_r",
                                 "_fb_hp_l", "_fb_hp_r", "_fb_lp_l", "_fb_lp_r"):
                        f = getattr(rv, name, None)
                        if f is not None and hasattr(f, "process"):
                            f.process(nl if name.endswith("_l") else nr)
                # JackEngine master-chain filters
                self._shuffler_shelf.process((nl - nr) * 0.5)
                for eq_l, eq_r in zip(self._master_eq_l, self._master_eq_r):
                    eq_l.process(nl)
                    eq_r.process(nr)
                self._master_hp6_l.process(nl)
                self._master_hp6_r.process(nr)
                self._master_hp12_l.process(nl)
                self._master_hp12_r.process(nr)
                for f in self._master_hp24_l:
                    f.process(nl)
                for f in self._master_hp24_r:
                    f.process(nr)
                self._organ_filter_l.process(nl)
                self._organ_filter_r.process(nr)
                self._organ_filter2_l.process(nl)
                self._organ_filter2_r.process(nr)
                # Bus comp — enable briefly to warm its scipy lfilter RMS state + HPF.
                was_enabled = self.bus_comp.enabled
                self.bus_comp.enabled = True
                self.bus_comp.process(nl.copy(), nr.copy())
                self.bus_comp.enabled = was_enabled
            # Reset bus comp metering so first real audio starts at 0 GR.
            self.bus_comp._env_gr_db = 0.0
            self.bus_comp._gr_db = 0.0
            logger.info("DSP state warmed (%d blocks)", n_blocks)
        except Exception as e:
            # Warmup is best-effort — never block startup on it.
            logger.warning("DSP warmup skipped: %s", e)

    def _generate_bpm_sidechain(self, n: int) -> np.ndarray:
        """Synthesize a pulse train at the current BPM. Each beat fires a ~50ms
        exp-decay pulse. Vectorized: beat crossings found via numpy diff, not a
        per-sample Python loop."""
        sr = SAMPLE_RATE
        bpm = max(40.0, float(getattr(self.synth, "bpm", 120.0)))
        beat_samples = (60.0 / bpm) * sr
        pulse_len = int(0.05 * sr)  # 50ms
        decay_rate = 4.0 / pulse_len
        if n > self._sc_scratch.shape[0]:
            self._sc_scratch = np.empty(max(n, 1024), dtype=np.float64)
        buf = self._sc_scratch[:n]
        buf[:] = 0.0

        phase_start = self._bpm_beat_phase
        pulse_rem_start = self._bpm_pulse_remaining
        phase_step = 1.0 / beat_samples

        # Finish any pulse from the previous block
        if pulse_rem_start > 0:
            tail = min(pulse_rem_start, n)
            age0 = pulse_len - pulse_rem_start
            buf[:tail] = np.exp(-np.arange(age0, age0 + tail, dtype=np.float64) * decay_rate)

        # Find beat-wrap sample indices: floor(phase) increments when a beat fires.
        # Phase after sample i (0-indexed advance) = phase_start + (i+1) * phase_step.
        phases = phase_start + (np.arange(n, dtype=np.float64) + 1.0) * phase_step
        int_phases = np.floor(phases).astype(np.int64)
        prev_int = int(np.floor(phase_start))
        # first wrap sample where int_phases[i] > int at start
        # vectorized: diff along a prefixed array
        wrap_mask = np.empty(n, dtype=bool)
        wrap_mask[0] = int_phases[0] > prev_int
        wrap_mask[1:] = int_phases[1:] > int_phases[:-1]
        wrap_positions = np.where(wrap_mask)[0]

        last_wrap = -1
        for wp in wrap_positions:
            pulse_tail = min(pulse_len, n - wp)
            buf[wp:wp + pulse_tail] = np.exp(
                -np.arange(pulse_tail, dtype=np.float64) * decay_rate
            )
            last_wrap = int(wp)

        # Update state. Keep phase in [0, 1) between blocks so int_phases stays bounded.
        self._bpm_beat_phase = phases[-1] % 1.0
        if last_wrap >= 0:
            self._bpm_pulse_remaining = max(0, pulse_len - (n - last_wrap))
        else:
            self._bpm_pulse_remaining = max(0, pulse_rem_start - n)
        return buf

    def _get_sidechain(self, source: str, n: int, piano_buf: np.ndarray = None):
        """Return (sc_l, sc_r) buffers (either may be None) for the selected
        sidechain source. None means use self (feedback) in BusCompressor."""
        if source == "piano" and piano_buf is not None:
            return piano_buf[0], piano_buf[1]
        if source == "lfo":
            # Use the LFO's current rectified amplitude as a DC-ish sidechain.
            # Only meaningful when depth > 0 (which is the sole gate now).
            if self.synth.lfo_depth > 0.001:
                # Advance_lfo already ran this block; use the last computed mod value
                amp = abs(self.synth._lfo_mod_a_last) * 0.7
                if n > self._sc_scratch.shape[0]:
                    self._sc_scratch = np.empty(max(n, 1024), dtype=np.float64)
                buf = self._sc_scratch[:n]
                buf[:] = amp
                return buf, None
            return None, None
        if source == "bpm":
            return self._generate_bpm_sidechain(n), None
        return None, None  # self

    def _setup_bridge_types(self):
        b = self._bridge
        b.bridge_start.restype = ctypes.c_int
        b.bridge_stop.restype = None
        b.bridge_write_audio.argtypes = [ctypes.POINTER(ctypes.c_float), ctypes.c_int]
        b.bridge_write_audio.restype = ctypes.c_int
        b.bridge_write_stereo.argtypes = [
            ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float), ctypes.c_int
        ]
        b.bridge_write_stereo.restype = ctypes.c_int
        b.bridge_set_master_volume.argtypes = [ctypes.c_float]
        b.bridge_set_master_volume.restype = None
        b.bridge_read_midi.argtypes = [ctypes.POINTER(ctypes.c_uint8)]
        b.bridge_read_midi.restype = ctypes.c_int
        b.bridge_get_sample_rate.restype = ctypes.c_int
        b.bridge_get_buffer_size.restype = ctypes.c_int
        b.bridge_get_callback_count.restype = ctypes.c_int
        b.bridge_get_peak_output.restype = ctypes.c_float
        b.bridge_get_xrun_count.restype = ctypes.c_int
        b.bridge_get_underrun_count.restype = ctypes.c_int
        b.bridge_get_midi_event_count.restype = ctypes.c_int
        b.bridge_get_ring_fill.restype = ctypes.c_int
        b.bridge_set_btl_mode.argtypes = [ctypes.c_int]
        b.bridge_set_btl_mode.restype = None
        b.bridge_set_ring_slots.argtypes = [ctypes.c_int]
        b.bridge_set_ring_slots.restype = ctypes.c_int
        b.bridge_get_ring_slots.restype = ctypes.c_int
        b.bridge_clear_ring.restype = ctypes.c_int
        b.bridge_is_shutdown.restype = ctypes.c_int

    def start(self):
        """Start the C bridge JACK client."""
        ret = self._bridge.bridge_start()
        if ret != 0:
            raise RuntimeError(f"bridge_start() failed with code {ret}")

        sr = self._bridge.bridge_get_sample_rate()
        bs = self._bridge.bridge_get_buffer_size()
        logger.info("JACK C bridge active: sr=%d, blocksize=%d", sr, bs)

        self._push_master_to_bridge()
        self._bridge.bridge_set_btl_mode(1 if BTL_MODE else 0)
        # Verify BTL mode was set correctly
        self._bridge.bridge_get_btl_mode.restype = ctypes.c_int
        btl_actual = self._bridge.bridge_get_btl_mode()
        logger.info("BTL mode: config=%s, bridge=%d (0=stereo, 1=mono-invert)", BTL_MODE, btl_actual)
        self.running = True

        # Warm DSP state with a brief noise burst so the first keystroke
        # doesn't kick a cold filter/envelope into a click transient.
        self._warm_dsp_state()

        # Start render thread — pushes audio blocks to the C bridge
        self._render_thread = threading.Thread(target=self._render_loop, daemon=True)
        self._render_thread.start()

        # Start MIDI read thread
        self._midi_thread = threading.Thread(target=self._midi_loop, daemon=True)
        self._midi_thread.start()

        # Start idle-GC thread — render thread has GC disabled for pop safety,
        # so cyclic garbage accumulates. This thread sweeps it during lulls.
        self._gc_thread = threading.Thread(target=self._gc_loop, daemon=True)
        self._gc_thread.start()

    def _render_loop(self):
        """Continuously render synth audio and keep the ring buffer full."""
        import gc
        bs = self._bridge.bridge_get_buffer_size()
        sr = self._bridge.bridge_get_sample_rate()
        block_time = bs / sr
        consecutive_errors = 0
        MAX_CONSECUTIVE_ERRORS = 20
        # Starvation detector: if the C-bridge ring buffer runs dry, the bridge
        # outputs silence until Python catches up — audible as a gap/click.
        _in_starvation = False
        _starvation_count = 0
        # Slow-block detector: threshold at 2× budget now (was 3×) so even
        # short GC stalls are visible.
        _slow_threshold_s = block_time * 2.0

        # Audio-thread hardening:
        #   1. Disable Python cyclic GC so generational collections don't fire
        #      unpredictably in the middle of a render. Reference-counting still
        #      works; we mostly hold numpy arrays which don't form cycles.
        #   2. Ask the kernel for SCHED_FIFO real-time scheduling so this thread
        #      gets CPU preferentially over regular work. Needs rtprio limit in
        #      /etc/security/limits.d/audio.conf (installer sets this up).
        try:
            gc.disable()
            logger.info("render thread: Python GC disabled")
        except Exception as e:
            logger.warning("gc.disable failed: %s", e)
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(80))
            logger.info("render thread: SCHED_FIFO priority 80")
        except Exception as e:
            logger.warning("couldn't set realtime priority: %s", e)
        # Pin to core 3 — paired with `isolcpus=3 irqaffinity=0-2` on the
        # kernel cmdline so this thread runs on a core the kernel won't
        # schedule normal tasks or IRQs onto. Soft-fails on non-Linux or
        # boxes that haven't been through install.sh.
        if hasattr(os, "sched_setaffinity"):
            try:
                os.sched_setaffinity(0, {3})
                logger.info("render thread: pinned to CPU 3")
            except Exception as e:
                logger.info("CPU pin skipped (%s) — fine if isolcpus not set", e)

        _last_iter_ts = time.perf_counter()
        # Mirror to instance attr so the systemd-watchdog heartbeat thread in
        # main.py can detect a wedged render loop (no update for >Xs ⇒ no ping
        # to systemd ⇒ systemd restarts us).
        self.last_iter_ts = _last_iter_ts
        while self.running:
            try:
                # Per-iteration gap detector: if the time since last loop start
                # exceeds ~15 ms, something paused us (not just our sleep).
                _now = time.perf_counter()
                _iter_gap = _now - _last_iter_ts
                _last_iter_ts = _now
                self.last_iter_ts = _now
                if _iter_gap > 0.015:  # > 15 ms between iterations = stall
                    logger.warning("iter gap %.1fms (expected ~%.1fms) at %.3f",
                                   _iter_gap * 1000, block_time * 500, time.time())

                # Exit if JACK server disappeared — systemd will restart us
                if self._bridge.bridge_is_shutdown():
                    logger.critical("JACK server shut down — exiting for systemd restart")
                    os._exit(1)

                # Check ring buffer fill level
                fill = self._bridge.bridge_get_ring_fill()

                # STARVATION: ring dropped to 0 or 1 → C bridge is producing
                # (or about to produce) silence. Log the event.
                if fill <= 1:
                    if not _in_starvation:
                        _starvation_count += 1
                        logger.warning("ring starved #%d fill=%d at %.3f",
                                       _starvation_count, fill, time.time())
                        _in_starvation = True
                elif fill >= 4:
                    _in_starvation = False

                if fill < self.ring_threshold:
                    # Snapshot note sets — the MIDI thread mutates these
                    # concurrently, and iterating live sets would raise a
                    # RuntimeError mid-render that gets swallowed silently.
                    if self.synth.sympathetic_enabled and not self.synth._sympathetic_suppress:
                        self.synth.set_sympathetic_notes(self._piano_notes_active.copy())
                    # Drone is now UI-triggered only (pad-player keys). Render-loop
                    # auto-latch to held MIDI notes was removed in 2026-04-17 when the
                    # top-bar DRONE button was replaced with the 12-key pad player.

                    # Ring is getting low — render and push a block.
                    # Piano/organ must render BEFORE the synth so their output
                    # can be tapped into the synth's reverb bus via the
                    # external_reverb_send path.
                    piano_pre = None
                    if self.piano_player:
                        piano_pre = self.piano_player.render_block(bs)
                        self._piano_renders += 1
                        # Piano bus level (for meter)
                        pkp = float(np.abs(piano_pre).max()) if piano_pre.size else 0.0
                        if pkp > self._piano_peak:
                            self._piano_peak = pkp
                        # Route through main filter if shared-filter is enabled
                        # (matches the downstream dry-path behaviour below).
                        if self.organ_filter_enabled:
                            cutoff = self.synth._filter_cutoff_cur
                            res = self.synth.filter_resonance
                            # Match the staggered-Q Butterworth decomposition
                            # used by the synth's main filter (see _Q24_S*_RATIO
                            # in synth_engine). Avoids the biquad² resonance
                            # doubling that 24 dB mode had prior to Review #7.
                            if self.synth.filter_slope == 24:
                                from .synth_engine import _Q24_S1_RATIO, _Q24_S2_RATIO
                                q_s1 = res * _Q24_S1_RATIO
                                q_s2 = res * _Q24_S2_RATIO
                                self._organ_filter_l.set_params(cutoff, q_s1)
                                self._organ_filter_r.set_params(cutoff, q_s1)
                                piano_pre[0] = self._organ_filter_l.process(piano_pre[0])
                                piano_pre[1] = self._organ_filter_r.process(piano_pre[1])
                                self._organ_filter2_l.set_params(cutoff, q_s2)
                                self._organ_filter2_r.set_params(cutoff, q_s2)
                                piano_pre[0] = self._organ_filter2_l.process(piano_pre[0])
                                piano_pre[1] = self._organ_filter2_r.process(piano_pre[1])
                            else:
                                self._organ_filter_l.set_params(cutoff, res)
                                self._organ_filter_r.set_params(cutoff, res)
                                piano_pre[0] = self._organ_filter_l.process(piano_pre[0])
                                piano_pre[1] = self._organ_filter_r.process(piano_pre[1])

                        # Soft-clip piano bus: linear below 0.85, asymptotes
                        # to 1.0 above. Prevents FluidSynth note-start
                        # hammer-strike transients (briefly 1.2–1.8 at high
                        # velocity) from slamming the master tanh limiter
                        # downstream — audible as a tick/glitch on heavy
                        # attacks. Held notes sit below the knee so they
                        # pass through untouched.
                        if piano_pre.size:
                            _THR = 0.85
                            _HEAD = 1.0 - _THR  # 0.15
                            _abs = np.abs(piano_pre)
                            _over = _abs > _THR
                            if _over.any():
                                _abs[_over] = _THR + _HEAD * np.tanh(
                                    (_abs[_over] - _THR) / _HEAD
                                )
                                np.multiply(np.sign(piano_pre), _abs, out=piano_pre)

                    # Build external reverb / delay sends from piano/organ output.
                    # Uses pre-allocated scratch to avoid per-block malloc.
                    # Grow the scratch if block size ever exceeds our cached one.
                    if piano_pre is not None and self._send_scratch.shape[1] < bs:
                        self._send_scratch = np.empty((2, bs), dtype=np.float64)
                    reverb_send_ext = None
                    if piano_pre is not None and self.piano_reverb_send > 0.001:
                        reverb_send_ext = self._send_scratch[:, :bs]
                        np.multiply(piano_pre, self.piano_reverb_send, out=reverb_send_ext)
                    delay_send_ext = None
                    if piano_pre is not None and self.piano_delay_send > 0.001:
                        # Separate buffer needed only if both sends are active.
                        # Reverb send reuses _send_scratch; delay send allocates
                        # just when needed (rare path).
                        if reverb_send_ext is not None:
                            delay_send_ext = piano_pre * self.piano_delay_send
                        else:
                            delay_send_ext = self._send_scratch[:, :bs]
                            np.multiply(piano_pre, self.piano_delay_send, out=delay_send_ext)

                    # If bus comp's FX-bypass mode is on, ask synth for dry + fx
                    # separately so we can route fx around the comp.
                    _render_t0 = time.perf_counter()
                    fx_bus = None
                    if self.bus_comp.enabled and self.bus_comp_fx_bypass:
                        stereo, fx_bus = self.synth.render(
                            bs, separate_fx=True,
                            external_reverb_send=reverb_send_ext,
                            external_delay_send=delay_send_ext,
                        )
                    else:
                        stereo = self.synth.render(
                            bs,
                            external_reverb_send=reverb_send_ext,
                            external_delay_send=delay_send_ext,
                        )
                    _render_dt = time.perf_counter() - _render_t0
                    if _render_dt > _slow_threshold_s:
                        logger.warning("slow render %.1fms (budget %.1fms) fill=%d at %.3f",
                                       _render_dt * 1000, block_time * 1000, fill, time.time())
                    # SYNTH-level peak check — catches clicks at the voice
                    # engine / drone / reverb output, before bus-comp can mask them.
                    if isinstance(stereo, np.ndarray) and stereo.size:
                        synth_peak = float(np.abs(stereo).max())
                        if synth_peak > 4.0 or not np.isfinite(synth_peak):
                            loc = int(np.argmax(np.abs(stereo))) % bs
                            logger.warning("SYNTH_SPIKE synth_peak=%.2f idx=%d at %.3f",
                                           synth_peak, loc, time.time())
                        prev_sp = getattr(self, "_prev_synth_peak", 0.0)
                        if prev_sp > 0.3 and synth_peak > prev_sp * 2.5 and synth_peak > 0.8:
                            loc = int(np.argmax(np.abs(stereo))) % bs
                            logger.warning("SYNTH_JUMP prev=%.3f now=%.3f (%.1fx) idx=%d at %.3f",
                                           prev_sp, synth_peak,
                                           synth_peak / max(prev_sp, 0.001),
                                           loc, time.time())
                        self._prev_synth_peak = synth_peak

                    # Pad/synth bus level (pre-piano, pre-FX) for fader meters
                    pp = float(np.abs(stereo).max()) if stereo.size else 0.0
                    if pp > self._pad_peak:
                        self._pad_peak = pp

                    # Mix the pre-rendered piano/organ dry into the master bus
                    # (the reverb-send copy was already fed to synth.render()).
                    piano_for_sc = None
                    if piano_pre is not None:
                        stereo[0] += piano_pre[0]
                        stereo[1] += piano_pre[1]
                        piano_for_sc = piano_pre

                    # ── SSL-style stereo shuffler (pre-EQ/pre-cut) ──
                    # Split into mid/side, boost side below 800Hz with low-shelf.
                    # Opens the low end of the stereo image without phasey
                    # highs or mono-collapse risk.
                    space = self.synth.reverb.space
                    if space > 0.001:
                        if abs(space - self._shuffler_space_last) > 0.005:
                            self._shuffler_shelf.set_params(250.0, 10.0 * space, 0.707)
                            self._shuffler_space_last = space
                        mid = (stereo[0] + stereo[1]) * 0.5
                        side = self._shuffler_shelf.process((stereo[0] - stereo[1]) * 0.5)
                        stereo[0] = mid + side
                        stereo[1] = mid - side

                    # Faust master FX path: bundles EQ + HP + pre-gain + sat
                    # + limiter in one compute call. Only used when bus comp
                    # is disabled (bus comp stays Python-side; its SSL G-style
                    # ballistics are too load-bearing to port without a
                    # dedicated session). Shuffler stays Python.
                    use_faust_master = (
                        self._faust_master_fx is not None
                        and not self.bus_comp.enabled
                    )
                    if use_faust_master:
                        # Push cached EQ/HP/tail params to Faust zones
                        for idx, (freq, gain, q) in enumerate(
                            getattr(self, "_master_eq_bands_cached", []), start=1
                        ):
                            self._faust_master_fx.set_eq_band(idx, freq, gain, q)
                        self._faust_master_fx.set_hp(
                            enabled=self._master_hp_enabled,
                            freq_hz=80.0,  # matches Python — hardcoded cutoff
                            slope_db_per_oct=self._master_hp_slope,
                        )
                        self._faust_master_fx.set_tail(
                            pre_gain=self.pre_gain,
                            sat_enabled=self.saturation_enabled,
                        )
                        self._faust_master_fx.process_inplace(stereo)
                        # Faust applied EQ, HP, pre_gain, sat, and tanh.
                        # Skip to post-processing (recorder + bridge_write).
                    else:
                        # Master EQ (configurable 3-band parametric)
                        if self._master_eq_active:
                            for eq_l, eq_r in zip(self._master_eq_l, self._master_eq_r):
                                stereo[0] = eq_l.process(stereo[0])
                                stereo[1] = eq_r.process(stereo[1])

                        # Master low cut
                        if self._master_hp_enabled:
                            if self._master_hp_slope == 6:
                                stereo[0] = self._master_hp6_l.process(stereo[0])
                                stereo[1] = self._master_hp6_r.process(stereo[1])
                            elif self._master_hp_slope == 24:
                                for f in self._master_hp24_l:
                                    stereo[0] = f.process(stereo[0])
                                for f in self._master_hp24_r:
                                    stereo[1] = f.process(stereo[1])
                            else:
                                stereo[0] = self._master_hp12_l.process(stereo[0])
                                stereo[1] = self._master_hp12_r.process(stereo[1])

                    if not use_faust_master:
                        # ═══ Pre-gain → bus comp → fx sum → limiter (Review #7) ═══
                        # The bus comp now sits AFTER the pre-gain so it sees
                        # the final summed level — that's the textbook
                        # master-bus placement (comp feeds the limiter, not
                        # the raw EQ output). Apply pre-gain to BOTH the dry
                        # path and the FX bypass bus so the wet/dry ratio is
                        # preserved; otherwise pre-gaining only the dry
                        # shifts the mix toward the dry.
                        stereo *= self.pre_gain
                        if fx_bus is not None:
                            fx_bus[0] *= self.pre_gain
                            fx_bus[1] *= self.pre_gain

                        if self.bus_comp.enabled:
                            # Faust bus comp only handles self-sidechain mode;
                            # external SC (piano/lfo/bpm) stays on Python path.
                            if (self._faust_bus_comp is not None
                                    and self.bus_comp_source == "self"):
                                self._faust_bus_comp.set_params(
                                    enabled=True,
                                    threshold_db=self.bus_comp.threshold_db,
                                    ratio=self.bus_comp.ratio,
                                    attack_ms=self.bus_comp.attack_ms,
                                    release_ms=self.bus_comp.release_ms,
                                    knee_db=self.bus_comp.knee_db,
                                    makeup_db=self.bus_comp.makeup_db,
                                    mix=self.bus_comp.mix,
                                    sc_hpf_hz=self.bus_comp.sidechain_hpf_hz,
                                )
                                self._faust_bus_comp.process_inplace(stereo)
                            else:
                                sc_l, sc_r = self._get_sidechain(self.bus_comp_source, bs, piano_for_sc)
                                self.bus_comp.process(
                                    stereo[0], stereo[1], sc_l, sc_r,
                                    skip_hpf=(self.bus_comp_source == "bpm"),
                                )

                        # FX-bypass routing: sum the untouched (pre-gained) FX
                        # bus back in post-comp so the reverb/delay tail isn't
                        # ducked by the sidechain pump.
                        if fx_bus is not None:
                            stereo[0] += fx_bus[0]
                            stereo[1] += fx_bus[1]
                    elif fx_bus is not None:
                        # Faust master path: bus comp disabled, but if FX-bypass
                        # was enabled in the UI we still need to sum the FX bus.
                        # Faust already applied its own pre_gain internally, so
                        # don't double-gain here.
                        stereo[0] += fx_bus[0]
                        stereo[1] += fx_bus[1]

                    # Track pre-limiter peak for metering (shows how hard limiter is hit)
                    pre_peak = float(np.abs(stereo).max())
                    if pre_peak > self._peak_output:
                        self._peak_output = pre_peak
                    # Pop-diagnostic: catch amplitude spikes + block-to-block
                    # jumps (suggests a click) + NaN/Inf.
                    if not np.isfinite(pre_peak):
                        logger.warning("NON-FINITE peak (NaN/Inf) in stereo at %.3f", time.time())
                        np.nan_to_num(stereo, copy=False, nan=0.0, posinf=1.0, neginf=-1.0)
                    else:
                        # Log absolute spikes above ~+14 dB (peak 5.0+)
                        if pre_peak > 5.0:
                            loc = int(np.argmax(np.abs(stereo))) % bs
                            logger.warning("SPIKE pre-limiter peak=%.2f bs=%d idx=%d at %.3f",
                                           pre_peak, bs, loc, time.time())
                        # Block-to-block peak jump ≥3× when already audible
                        # often correlates with an audible click/pop. Reset
                        # baseline after each log so we don't spam.
                        prev_peak = getattr(self, "_prev_pre_peak", 0.0)
                        if prev_peak > 0.3 and pre_peak > prev_peak * 3.0 and pre_peak > 1.0:
                            loc = int(np.argmax(np.abs(stereo))) % bs
                            logger.warning("JUMP prev=%.2f now=%.2f (%.1fx) idx=%d at %.3f",
                                           prev_peak, pre_peak, pre_peak/max(prev_peak, 0.001),
                                           loc, time.time())
                        self._prev_pre_peak = pre_peak

                    if not use_faust_master:
                        # Optional asymmetric drive (SAT button) — injects 2nd
                        # harmonic for tape/transformer-style warmth. Skipped
                        # when off so the chain stays purely clean.
                        if self.saturation_enabled:
                            scratch = self._sat_scratch[:, :bs]
                            np.abs(stereo, out=scratch)
                            scratch *= 0.09
                            stereo *= 1.01
                            stereo += scratch
                            # Strip the DC that +|x|·0.09 injected so the
                            # limiter sees a symmetric waveform.
                            stereo[0] = self._sat_dc_l.process(stereo[0])
                            stereo[1] = self._sat_dc_r.process(stereo[1])

                        # Brickwall lookahead limiter — keeps |output| ≤ 0.98
                        # transparently. Replaces the old np.tanh soft-clip,
                        # which secretly relied on the bridge hard-clip to do
                        # the actual ceiling and cost ~4 dB of transient clarity.
                        self._limiter.process_inplace(stereo)

                    # Down-cast float64 master into pre-allocated float32 scratch
                    # buffers. astype() used to allocate a fresh array every
                    # block — now resized once if block size grows.
                    if self._f32_scratch_l.shape[0] < bs:
                        self._f32_scratch_l = np.empty(bs, dtype=np.float32)
                        self._f32_scratch_r = np.empty(bs, dtype=np.float32)
                    left_f32 = self._f32_scratch_l[:bs]
                    right_f32 = self._f32_scratch_r[:bs]
                    np.copyto(left_f32, stereo[0], casting="unsafe")
                    np.copyto(right_f32, stereo[1], casting="unsafe")

                    # Tap for the recorder (no-op when not recording).
                    if self.recorder.is_recording():
                        self.recorder.feed(left_f32, right_f32)

                    self._bridge.bridge_write_stereo(
                        left_f32.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                        right_f32.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                        bs
                    )
                    self._callback_count += 1
                else:
                    # Ring is full enough — sleep briefly
                    time.sleep(block_time * 0.5)

                # Reset the consecutive-error counter on a successful pass
                consecutive_errors = 0

            except Exception as e:
                self._last_error = str(e)
                import traceback
                self._last_traceback = traceback.format_exc()
                logger.error("Render error: %s", e)
                consecutive_errors += 1
                # Sleep one block so we don't burn CPU spinning on the error
                time.sleep(block_time)
                # If we've been failing for a while, die so systemd restarts us
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    logger.critical(
                        "Render loop failed %d blocks in a row — exiting "
                        "for systemd restart. Last error: %s",
                        consecutive_errors, e,
                    )
                    os._exit(1)

    def _gc_loop(self):
        """Idle-triggered cyclic GC + malloc arena trim.

        Every 30 s, when no voices and no piano notes are held, runs
        gen-0 gc + malloc_trim(0). Fast (~7 ms gc, ~0.2 ms trim), well
        inside the ring's 127 ms headroom.

        Additionally, once per day at 3 AM local time, runs a full
        gen-2 gc as backstop against long-lived cyclic leaks. Only
        fires after 90 s of confirmed silence — reverb / freeze /
        shimmer tails have fully decayed so the GIL hold produces no
        audible dropout. If 3 AM arrives with active play, skips
        silently and retries tomorrow.
        """
        import gc
        CHECK_INTERVAL_S = 30.0
        FULL_GC_HOUR = 3                 # local 3 AM
        MIN_FULL_GC_INTERVAL_S = 82800   # ≈23 hr — prevents repeat firings in the hour
        SILENCE_CYCLES_REQUIRED = 3      # 3 × 30 s = 90 s of confirmed silence

        # Resolve glibc's malloc_trim() via ctypes. If it's not glibc
        # (musl, alternate libc) we just skip the trim; gen-0 GC still runs.
        _malloc_trim = None
        try:
            _libc = ctypes.CDLL("libc.so.6", use_errno=False)
            _libc.malloc_trim.argtypes = [ctypes.c_size_t]
            _libc.malloc_trim.restype = ctypes.c_int
            _malloc_trim = _libc.malloc_trim
        except Exception as e:
            logger.info("malloc_trim unavailable (not glibc?): %s", e)

        def _rss_kb():
            try:
                with open("/proc/self/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            return int(line.split()[1])
            except Exception:
                pass
            return 0

        _silent_cycles = 0
        _last_full = 0.0
        while self.running:
            time.sleep(CHECK_INTERVAL_S)
            if not self.running:
                return
            idle = (len(self.synth.voices) == 0
                    and len(self._piano_notes_active) == 0)
            if not idle:
                _silent_cycles = 0
                continue
            _silent_cycles += 1
            rss_before = _rss_kb()
            t0 = time.perf_counter()
            try:
                collected = gc.collect(0)
            except Exception as e:
                logger.warning("gc.collect failed: %s", e)
                continue
            gc_ms = (time.perf_counter() - t0) * 1000.0
            trim_ms = 0.0
            if _malloc_trim is not None:
                t1 = time.perf_counter()
                try:
                    _malloc_trim(0)
                except Exception as e:
                    logger.warning("malloc_trim failed: %s", e)
                trim_ms = (time.perf_counter() - t1) * 1000.0
            rss_after = _rss_kb()
            freed_kb = rss_before - rss_after
            logger.info("idle GC gen0: collected=%d freed=%dKB rss=%dKB "
                        "gc=%.1fms trim=%.1fms",
                        collected, freed_kb, rss_after, gc_ms, trim_ms)

            # Daily full sweep at 3 AM — backstop for long-lived cyclic leaks.
            now = time.time()
            if (time.localtime(now).tm_hour == FULL_GC_HOUR
                    and (now - _last_full) > MIN_FULL_GC_INTERVAL_S
                    and _silent_cycles >= SILENCE_CYCLES_REQUIRED):
                t2 = time.perf_counter()
                try:
                    full_collected = gc.collect(2)
                except Exception as e:
                    logger.warning("daily full gc.collect failed: %s", e)
                else:
                    full_ms = (time.perf_counter() - t2) * 1000.0
                    full_trim_ms = 0.0
                    if _malloc_trim is not None:
                        t3 = time.perf_counter()
                        try:
                            _malloc_trim(0)
                        except Exception as e:
                            logger.warning("daily malloc_trim failed: %s", e)
                        full_trim_ms = (time.perf_counter() - t3) * 1000.0
                    _last_full = now
                    logger.info("DAILY full GC: collected=%d rss=%dKB "
                                "gc=%.1fms trim=%.1fms (silence=%ds)",
                                full_collected, _rss_kb(),
                                full_ms, full_trim_ms,
                                _silent_cycles * int(CHECK_INTERVAL_S))

    def _midi_loop(self):
        """Read MIDI events from the C bridge and dispatch.
        Note tracking uses raw MIDI note numbers so transpose/octave changes
        between note-on and note-off can't cause hung notes."""
        midi_buf = (ctypes.c_uint8 * 4)()

        while self.running:
            while True:
                n = self._bridge.bridge_read_midi(midi_buf)
                if n == 0:
                    break

                self._midi_events_seen += 1

                # System real-time messages are 1 byte (0xF8 = clock tick,
                # 0xFA = start, 0xFB = continue, 0xFC = stop). Handled
                # before the n>=2 channel-voice branch below.
                if n == 1 and midi_buf[0] == 0xF8 and self.midi_clock_enabled:
                    now = time.perf_counter()
                    last = self._midi_clock_last_ts
                    self._midi_clock_last_ts = now
                    if last > 0.0:
                        dt = now - last
                        # Reject huge gaps (clock paused / first tick after
                        # idle) so we don't average them in. Worth the cost
                        # of one extra branch per tick.
                        if 0.005 < dt < 0.5:  # 30..1500 BPM range
                            if self._midi_clock_avg_dt <= 0.0:
                                self._midi_clock_avg_dt = dt
                            else:
                                # One-pole at ~0.1 lets a 5-bpm shift settle
                                # in ~10 ticks (half a beat at 120bpm).
                                self._midi_clock_avg_dt += 0.1 * (dt - self._midi_clock_avg_dt)
                            self._midi_clock_count += 1
                            # Push BPM every ~24 ticks (1 beat). Avoids
                            # thrash on render thread; 24 PPQN is the spec.
                            if self._midi_clock_count >= 24:
                                self._midi_clock_count = 0
                                bpm = 60.0 / (self._midi_clock_avg_dt * 24.0)
                                bpm = max(40.0, min(240.0, bpm))
                                self.set_bpm(bpm)
                    continue

                if n >= 2:
                    # Channel-voice messages cover n==2 (program change,
                    # channel aftertouch) and n==3 (note, CC, pitch bend,
                    # poly aftertouch). midi_buf[2] is only valid for n==3
                    # — guarded inside each branch as appropriate.
                    status = midi_buf[0] & 0xF0
                    raw_note = midi_buf[1]
                    velocity = midi_buf[2] if n >= 3 else 0

                    if status == 0x90 and velocity > 0:
                        if velocity < self.min_velocity:
                            continue
                        vel_float = min(1.0, velocity / 127.0)

                        transposed = max(0, min(127, raw_note + self.transpose))
                        piano_note = max(0, min(127, transposed + self.piano_octave * 12))

                        # LAYER (split) — gate per-source by raw key position.
                        # raw_note is the key the user pressed (pre-transpose);
                        # split is a controller-position concept, not a pitch one.
                        if self.split_enabled:
                            osc1_w, osc2_w, shim_w = self.synth.compute_split_weights(raw_note)
                            instrument_w = SynthEngine.split_weight(
                                raw_note, self.instrument_split_low,
                                self.instrument_split_high, self.instrument_split_xfade,
                            )
                        else:
                            osc1_w = osc2_w = shim_w = instrument_w = 1.0

                        # If re-triggering with different transpose, release old note first
                        if raw_note in self._note_map:
                            old_pad, old_piano = self._note_map[raw_note]
                            if old_pad != transposed:
                                self.synth.note_off(old_pad)
                                if self.piano_callback:
                                    self.piano_callback("note_off", old_piano, 0)

                        self._physically_held.add(raw_note)
                        self._note_map[raw_note] = (transposed, piano_note)

                        # Skip pad note_on entirely if all pad sources are silenced
                        # in this zone — saves a voice slot for actually-audible notes.
                        if osc1_w > 0.0 or osc2_w > 0.0 or shim_w > 0.0:
                            self.synth.note_on(transposed, vel_float, osc1_w, osc2_w, shim_w)
                        self._midi_notes_triggered += 1
                        self._pad_notes_active.add(transposed)

                        # Retrigger BPM sidechain on note-on so the first pump
                        # lands on the first note instead of running free-phase.
                        if (self.bus_comp_retrigger and self.bus_comp.enabled
                                and self.bus_comp_source == "bpm"):
                            self._bpm_beat_phase = 0.0
                            self._bpm_pulse_remaining = int(0.05 * SAMPLE_RATE)

                        # Piano/organ velocity scaled by the instrument zone
                        # weight. weight=0 → skip the callback entirely so the
                        # voice doesn't even start.
                        if self.piano_callback and instrument_w > 0.0:
                            self.piano_callback("note_on", piano_note, vel_float * instrument_w)
                            self._piano_notes_active.add(piano_note)
                        if self.midi_callback:
                            self.midi_callback("note_on", transposed, vel_float)

                    elif status == 0x80 or (status == 0x90 and velocity == 0):
                        self._physically_held.discard(raw_note)

                        if raw_note not in self._note_map:
                            continue

                        pad_note, piano_note = self._note_map[raw_note]

                        if self._sustain_on:
                            self._sustained_notes.add(raw_note)
                        elif self._sostenuto_on and raw_note in self._sostenuto_held:
                            # Held by sostenuto — leave the voice alone, but
                            # remove from physically_held so a later sustain
                            # press doesn't re-capture it.
                            pass
                        else:
                            self._note_map.pop(raw_note)
                            self.synth.note_off(pad_note)
                            self._pad_notes_active.discard(pad_note)
                            if self.piano_callback:
                                self.piano_callback("note_off", piano_note, 0)
                                self._piano_notes_active.discard(piano_note)

                        if self.midi_callback:
                            self.midi_callback("note_off", pad_note, 0)

                    elif status == 0xB0 and raw_note == 64:
                        # Sustain pedal
                        if velocity >= 64:
                            self._sustain_on = True
                        else:
                            self._sustain_on = False
                            for raw in list(self._sustained_notes):
                                if raw not in self._physically_held and raw in self._note_map:
                                    pad_n, piano_n = self._note_map.pop(raw)
                                    self.synth.note_off(pad_n)
                                    self._pad_notes_active.discard(pad_n)
                                    if self.piano_callback:
                                        self.piano_callback("note_off", piano_n, 0)
                                        self._piano_notes_active.discard(piano_n)
                            self._sustained_notes.clear()

                    elif status == 0xB0 and raw_note == 66:
                        # Sostenuto pedal (CC66): captures notes that are
                        # CURRENTLY held when pressed, sustains only those —
                        # later notes pass through as normal. Pianists use
                        # this for a held bass under moving voicings.
                        if velocity >= 64:
                            self._sostenuto_on = True
                            self._sostenuto_held = set(self._physically_held)
                        else:
                            self._sostenuto_on = False
                            for raw in list(self._sostenuto_held):
                                if raw not in self._physically_held and raw in self._note_map:
                                    pad_n, piano_n = self._note_map.pop(raw)
                                    self.synth.note_off(pad_n)
                                    self._pad_notes_active.discard(pad_n)
                                    if self.piano_callback:
                                        self.piano_callback("note_off", piano_n, 0)
                                        self._piano_notes_active.discard(piano_n)
                            self._sostenuto_held.clear()

                    elif status == 0xB0 and raw_note in (120, 123):
                        # CC120 = All Sound Off (immediate silence + flush tails)
                        # CC123 = All Notes Off (release ADSRs, tails decay)
                        # Treat both as full panic — flushes reverb/freeze/drone too.
                        self._sustain_on = False
                        self._sustained_notes.clear()
                        self._sostenuto_on = False
                        self._sostenuto_held.clear()
                        self._physically_held.clear()
                        self._note_map.clear()
                        self._pad_notes_active.clear()
                        self._piano_notes_active.clear()
                        self.synth.all_notes_off()
                        if raw_note == 120:
                            # CC120 wants tails flushed; defer to the panic-pending
                            # mechanism so render-thread does the work cleanly.
                            self.synth.panic()
                        if self.piano_callback:
                            self.piano_callback("all_notes_off", 0, 0)
                        if self.midi_callback:
                            self.midi_callback("all_notes_off", 0, 0)

                    elif status == 0xB0:
                        cc_num = raw_note
                        cc_val = velocity
                        if self.cc_callback:
                            self.cc_callback(cc_num, cc_val)

                    elif status == 0xE0:
                        # Pitch bend: 14-bit value, MSB byte 2, LSB byte 1.
                        # Center = 8192. Bipolar normalized [-1, +1] → semitones.
                        if self.pitch_bend_enabled:
                            lsb = raw_note  # midi_buf[1]
                            msb = velocity  # midi_buf[2]
                            bend14 = ((msb & 0x7F) << 7) | (lsb & 0x7F)
                            normalized = (bend14 - 8192) / 8192.0
                            self.synth.set_pitch_bend(normalized)
                            if self.piano_callback:
                                self.piano_callback("pitch_bend", bend14, 0)

                    elif status == 0xD0:
                        # Channel aftertouch: 1 data byte (pressure 0..127).
                        # Forwarded as a CC-shaped event so callbacks can route
                        # it (e.g. to filter cutoff) via the same pathway as CC.
                        if self.cc_callback:
                            self.cc_callback(-1, raw_note)  # cc_num=-1 means aftertouch

                    elif status == 0xA0:
                        # Poly aftertouch (per-note pressure): byte1=note, byte2=pressure.
                        # Currently pass-through only; future work could route to
                        # per-voice filter or amp.
                        if self.midi_callback:
                            self.midi_callback("poly_aftertouch", raw_note, velocity / 127.0)

                    elif status == 0xC0:
                        # Program change: 1 data byte (program 0..127).
                        # Wire to setlist position via main.py callback.
                        if self.program_change_callback:
                            self.program_change_callback(raw_note)

            time.sleep(0.0005)  # 0.5ms polling

    def set_master_hp(self, freq_hz: float, slope: int = 12, enabled: bool = True):
        """Update master low cut filter."""
        self._master_hp_enabled = enabled
        self._master_hp_slope = slope
        self._master_hp6_l.set_params(freq_hz)
        self._master_hp6_r.set_params(freq_hz)
        self._master_hp12_l.set_params(freq_hz, 0.707)
        self._master_hp12_r.set_params(freq_hz, 0.707)
        for f in self._master_hp24_l:
            f.set_params(freq_hz, 0.707)
        for f in self._master_hp24_r:
            f.set_params(freq_hz, 0.707)

    def set_master_eq(self, bands: list):
        """Update master EQ bands. Each band: {freq_hz, gain_db, q}"""
        cached = []
        for i, band in enumerate(bands):
            if i < len(self._master_eq_l):
                freq = float(band.get("freq_hz", 1000))
                gain = float(band.get("gain_db", 0.0))
                q = float(band.get("q", 1.5))
                self._master_eq_l[i].set_params(freq, gain, q)
                self._master_eq_r[i].set_params(freq, gain, q)
                cached.append((freq, gain, q))
        self._master_eq_bands_cached = cached  # read by the Faust master-fx path
        self._master_eq_active = any(
            abs(float(b.get("gain_db", 0.0))) > 0.01 for b in bands
        )

    def panic(self):
        """Clear sustain pedal, note-maps, active-note sets, and master-chain
        transient state so no ghost state bleeds into the next note. Also
        flush the C bridge ring so the ~80ms of pre-rendered audio doesn't
        keep playing the pre-panic howl after the user mashed STOP."""
        self._sustain_on = False
        self._physically_held.clear()
        self._sustained_notes.clear()
        self._note_map.clear()
        self._pad_notes_active.clear()
        self._piano_notes_active.clear()
        # Release the brickwall limiter so the next hit doesn't inherit
        # a pumped-down gain from whatever peak we were catching at panic time.
        self._limiter.reset()
        # Flush the C bridge ring (~20ms mute). Without this, pre-rendered
        # audio in the ring still plays for ~80ms (16-slot) / ~16ms (low-lat)
        # after panic, so a feedback howl rings on for a beat after STOP.
        try:
            self._bridge.bridge_clear_ring()
        except Exception as e:
            logger.warning("bridge_clear_ring failed: %s", e)

    def get_and_reset_peak(self) -> float:
        """Return post-limiter peak level. Reflects actual output after tanh + master volume."""
        peak = self._peak_output
        self._peak_output = 0.0
        return peak

    def get_and_reset_bus_peaks(self) -> dict:
        """Return per-bus peak levels (pad, piano) since last call. Used to drive
        per-fader signal meters in the UI."""
        pad = self._pad_peak
        piano = self._piano_peak
        self._pad_peak = 0.0
        self._piano_peak = 0.0
        return {"pad": pad, "piano": piano}

    def stop(self):
        """Shut down."""
        self.synth.all_notes_off()
        self.running = False
        time.sleep(0.1)
        self._bridge.bridge_stop()
        logger.info("JACK engine stopped")
