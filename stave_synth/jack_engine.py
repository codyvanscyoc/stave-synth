"""JACK audio engine via C bridge — handles audio output and MIDI input.

python-jack-client's CFFI buffer writing is broken on aarch64/Pi 5.
This module uses a small C shared library (jack_bridge.so) loaded via ctypes
that handles the JACK process callback natively.
"""

import ctypes
import logging
import os
import threading
import time

import numpy as np

from .synth_engine import (SynthEngine, BiquadLowpass, BiquadHighpass,
                           BiquadPeakingEQ, BiquadLowShelf, OnePole6dBHighpass,
                           fader_to_amplitude, blend_to_amplitude)
from .config import SAMPLE_RATE, BTL_MODE

logger = logging.getLogger(__name__)

# Locate the C bridge shared library next to this file
_BRIDGE_PATH = os.path.join(os.path.dirname(__file__), "jack_bridge.so")


class JackEngine:
    """Manages JACK audio via C bridge with MIDI input."""

    def __init__(self, synth: SynthEngine, midi_callback=None, piano_callback=None,
                 piano_player=None, cc_callback=None):
        self.synth = synth
        self.midi_callback = midi_callback
        self.piano_callback = piano_callback
        self.cc_callback = cc_callback  # Called with (cc_number, value_0_to_127)
        self.piano_player = piano_player  # FluidSynthPlayer for rendered mixing
        self.running = False

        # Master volume and transpose (set from outside)
        self._master_volume = 0.85
        self.pre_gain = 2.0  # pre-gain into limiter — drive into tanh for louder sustained material
        self.transpose = 0
        self.piano_octave = 0  # -3 to +3 octaves for piano

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

        # Saturation (SAT button): optional asymmetric drive before the tanh
        # soft limiter. Off by default — tanh alone is the clean path.
        self.saturation_enabled = False
        self._sat_scratch = np.empty((2, 512), dtype=np.float64)

        # Debug counters
        self._callback_count = 0
        self._peak_output = 0.0
        self._piano_peak = 0.0
        self._piano_renders = 0
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
        if hasattr(self, '_bridge'):
            self._bridge.bridge_set_master_volume(ctypes.c_float(fader_to_amplitude(val)))

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
        b.bridge_is_shutdown.restype = ctypes.c_int

    def start(self):
        """Start the C bridge JACK client."""
        ret = self._bridge.bridge_start()
        if ret != 0:
            raise RuntimeError(f"bridge_start() failed with code {ret}")

        sr = self._bridge.bridge_get_sample_rate()
        bs = self._bridge.bridge_get_buffer_size()
        logger.info("JACK C bridge active: sr=%d, blocksize=%d", sr, bs)

        self._bridge.bridge_set_master_volume(ctypes.c_float(fader_to_amplitude(self.master_volume)))
        self._bridge.bridge_set_btl_mode(1 if BTL_MODE else 0)
        # Verify BTL mode was set correctly
        self._bridge.bridge_get_btl_mode.restype = ctypes.c_int
        btl_actual = self._bridge.bridge_get_btl_mode()
        logger.info("BTL mode: config=%s, bridge=%d (0=stereo, 1=mono-invert)", BTL_MODE, btl_actual)
        self.running = True

        # Start render thread — pushes audio blocks to the C bridge
        self._render_thread = threading.Thread(target=self._render_loop, daemon=True)
        self._render_thread.start()

        # Start MIDI read thread
        self._midi_thread = threading.Thread(target=self._midi_loop, daemon=True)
        self._midi_thread.start()

    def _render_loop(self):
        """Continuously render synth audio and keep the ring buffer full."""
        bs = self._bridge.bridge_get_buffer_size()
        sr = self._bridge.bridge_get_sample_rate()
        block_time = bs / sr
        consecutive_errors = 0
        MAX_CONSECUTIVE_ERRORS = 20

        while self.running:
            try:
                # Exit if JACK server disappeared — systemd will restart us
                if self._bridge.bridge_is_shutdown():
                    logger.critical("JACK server shut down — exiting for systemd restart")
                    os._exit(1)

                # Check ring buffer fill level
                fill = self._bridge.bridge_get_ring_fill()

                if fill < 4:
                    # Snapshot note sets — the MIDI thread mutates these
                    # concurrently, and iterating live sets would raise a
                    # RuntimeError mid-render that gets swallowed silently.
                    if self.synth.sympathetic_enabled and not self.synth._sympathetic_suppress:
                        self.synth.set_sympathetic_notes(self._piano_notes_active.copy())
                    if self.synth.drone_enabled and self._pad_notes_active:
                        self.synth.set_drone_chord(list(self._pad_notes_active))

                    # Ring is getting low — render and push a block
                    stereo = self.synth.render(bs)  # (2, n) stereo

                    # Mix in piano/organ audio (stereo)
                    if self.piano_player:
                        piano = self.piano_player.render_block(bs)
                        self._piano_renders += 1

                        # Optionally route through main filter
                        if self.organ_filter_enabled:
                            cutoff = self.synth._filter_cutoff_cur
                            res = self.synth.filter_resonance
                            self._organ_filter_l.set_params(cutoff, res)
                            self._organ_filter_r.set_params(cutoff, res)
                            piano[0] = self._organ_filter_l.process(piano[0])
                            piano[1] = self._organ_filter_r.process(piano[1])
                            if self.synth.filter_slope == 24:
                                self._organ_filter2_l.set_params(cutoff, res)
                                self._organ_filter2_r.set_params(cutoff, res)
                                piano[0] = self._organ_filter2_l.process(piano[0])
                                piano[1] = self._organ_filter2_r.process(piano[1])

                        stereo[0] += piano[0]
                        stereo[1] += piano[1]

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

                    # Pre-gain: boost into limiter for more headroom
                    stereo *= self.pre_gain

                    # Track pre-limiter peak for metering (shows how hard limiter is hit)
                    pre_peak = float(np.abs(stereo).max())
                    if pre_peak > self._peak_output:
                        self._peak_output = pre_peak

                    # Optional asymmetric drive (SAT button) — injects 2nd
                    # harmonic for tape/transformer-style warmth. Skipped when
                    # off so the limiter stays purely clean tanh.
                    if self.saturation_enabled:
                        scratch = self._sat_scratch[:, :bs]
                        np.abs(stereo, out=scratch)
                        scratch *= 0.09
                        stereo *= 1.01
                        stereo += scratch

                    # Soft limiter (tanh) — clean when SAT is off
                    np.tanh(stereo, out=stereo)

                    left_f32 = stereo[0].astype(np.float32)
                    right_f32 = stereo[1].astype(np.float32)

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

                if n >= 3:
                    status = midi_buf[0] & 0xF0
                    raw_note = midi_buf[1]
                    velocity = midi_buf[2]

                    if status == 0x90 and velocity > 0:
                        if velocity < self.min_velocity:
                            continue
                        vel_float = min(1.0, velocity / 127.0)

                        transposed = max(0, min(127, raw_note + self.transpose))
                        piano_note = max(0, min(127, transposed + self.piano_octave * 12))

                        # If re-triggering with different transpose, release old note first
                        if raw_note in self._note_map:
                            old_pad, old_piano = self._note_map[raw_note]
                            if old_pad != transposed:
                                self.synth.note_off(old_pad)
                                if self.piano_callback:
                                    self.piano_callback("note_off", old_piano, 0)

                        self._physically_held.add(raw_note)
                        self._note_map[raw_note] = (transposed, piano_note)

                        self.synth.note_on(transposed, vel_float)
                        self._midi_notes_triggered += 1
                        self._pad_notes_active.add(transposed)

                        if self.piano_callback:
                            self.piano_callback("note_on", piano_note, vel_float)
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

                    elif status == 0xB0 and raw_note == 123:
                        self._sustain_on = False
                        self._sustained_notes.clear()
                        self._physically_held.clear()
                        self._note_map.clear()
                        self._pad_notes_active.clear()
                        self._piano_notes_active.clear()
                        self.synth.all_notes_off()
                        if self.piano_callback:
                            self.piano_callback("all_notes_off", 0, 0)
                        if self.midi_callback:
                            self.midi_callback("all_notes_off", 0, 0)

                    elif status == 0xB0:
                        cc_num = raw_note
                        cc_val = velocity
                        if self.cc_callback:
                            self.cc_callback(cc_num, cc_val)

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
        for i, band in enumerate(bands):
            if i < len(self._master_eq_l):
                freq = float(band.get("freq_hz", 1000))
                gain = float(band.get("gain_db", 0.0))
                q = float(band.get("q", 1.5))
                self._master_eq_l[i].set_params(freq, gain, q)
                self._master_eq_r[i].set_params(freq, gain, q)
        self._master_eq_active = any(
            abs(float(b.get("gain_db", 0.0))) > 0.01 for b in bands
        )

    def panic(self):
        """Clear sustain pedal, note-maps, and active-note sets so no ghost state remains."""
        self._sustain_on = False
        self._physically_held.clear()
        self._sustained_notes.clear()
        self._note_map.clear()
        self._pad_notes_active.clear()
        self._piano_notes_active.clear()

    def get_and_reset_peak(self) -> float:
        """Return post-limiter peak level. Reflects actual output after tanh + master volume."""
        peak = self._peak_output
        self._peak_output = 0.0
        return peak

    def stop(self):
        """Shut down."""
        self.synth.all_notes_off()
        self.running = False
        time.sleep(0.1)
        self._bridge.bridge_stop()
        logger.info("JACK engine stopped")
