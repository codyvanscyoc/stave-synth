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

from .synth_engine import SynthEngine, fader_to_amplitude, blend_to_amplitude
from .config import SAMPLE_RATE, BUFFER_SIZE, BTL_MODE

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
        self.pre_gain = 1.5  # pre-gain into limiter — mild boost before tanh
        self.transpose = 0
        self.piano_octave = 0  # -3 to +3 octaves for piano

        # MIDI filtering
        self.min_velocity = 10

        # Sustain pedal state
        self._sustain_on = False
        self._sustained_pad_notes = set()    # pad notes held by pedal
        self._sustained_piano_notes = set()  # piano notes held by pedal

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

        while self.running:
            try:
                # Exit if JACK server disappeared — systemd will restart us
                if self._bridge.bridge_is_shutdown():
                    logger.critical("JACK server shut down — exiting for systemd restart")
                    os._exit(1)

                # Check ring buffer fill level
                fill = self._bridge.bridge_get_ring_fill()

                if fill < 4:
                    # Ring is getting low — render and push a block
                    stereo = self.synth.render(bs)  # (2, n) stereo

                    # Mix in piano audio (stereo)
                    if self.piano_player:
                        piano = self.piano_player.render_block(bs)
                        piano_peak = float(np.abs(piano).max())
                        if piano_peak > self._piano_peak:
                            self._piano_peak = piano_peak
                        self._piano_renders += 1
                        stereo[0] += piano[0]
                        stereo[1] += piano[1]

                    # Pre-gain: boost into limiter for more headroom
                    stereo *= self.pre_gain

                    # Track pre-limiter peak for metering (shows how hard limiter is hit)
                    pre_peak = float(np.abs(stereo).max())
                    if pre_peak > self._peak_output:
                        self._peak_output = pre_peak

                    # Soft limiter (tanh) on both channels
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

            except Exception as e:
                self._last_error = str(e)
                import traceback
                self._last_traceback = traceback.format_exc()
                logger.error("Render error: %s", e)

    def _midi_loop(self):
        """Read MIDI events from the C bridge and dispatch."""
        midi_buf = (ctypes.c_uint8 * 4)()

        while self.running:
            while True:
                n = self._bridge.bridge_read_midi(midi_buf)
                if n == 0:
                    break

                self._midi_events_seen += 1

                if n >= 3:
                    status = midi_buf[0] & 0xF0
                    note = midi_buf[1]
                    velocity = midi_buf[2]

                    transposed = note + self.transpose
                    transposed = max(0, min(127, transposed))

                    if status == 0x90 and velocity > 0:
                        if velocity < self.min_velocity:
                            continue
                        vel_float = min(1.0, velocity / 127.0)

                        # Piano octave shift (pad oscs handle their own octave internally)
                        piano_note = max(0, min(127, transposed + self.piano_octave * 12))

                        self.synth.note_on(transposed, vel_float)
                        self._midi_notes_triggered += 1

                        if self.piano_callback:
                            self.piano_callback("note_on", piano_note, vel_float)
                        if self.midi_callback:
                            self.midi_callback("note_on", transposed, vel_float)

                    elif status == 0x80 or (status == 0x90 and velocity == 0):
                        piano_note = max(0, min(127, transposed + self.piano_octave * 12))

                        if self._sustain_on:
                            # Pedal held — defer note-off
                            self._sustained_pad_notes.add(transposed)
                            self._sustained_piano_notes.add(piano_note)
                        else:
                            self.synth.note_off(transposed)

                            if self.piano_callback:
                                self.piano_callback("note_off", piano_note, 0)
                        if self.midi_callback:
                            self.midi_callback("note_off", transposed, 0)

                    elif status == 0xB0 and midi_buf[1] == 64:
                        # Sustain pedal
                        if midi_buf[2] >= 64:
                            self._sustain_on = True
                        else:
                            self._sustain_on = False
                            # Release all sustained notes
                            for held in self._sustained_pad_notes:
                                self.synth.note_off(held)
                            for held in self._sustained_piano_notes:
                                if self.piano_callback:
                                    self.piano_callback("note_off", held, 0)
                            self._sustained_pad_notes.clear()
                            self._sustained_piano_notes.clear()

                    elif status == 0xB0 and midi_buf[1] == 123:
                        self._sustain_on = False
                        self._sustained_pad_notes.clear()
                        self._sustained_piano_notes.clear()
                        self.synth.all_notes_off()
                        if self.piano_callback:
                            self.piano_callback("all_notes_off", 0, 0)
                        if self.midi_callback:
                            self.midi_callback("all_notes_off", 0, 0)

                    elif status == 0xB0:
                        # CC message — forward to cc_callback
                        cc_num = midi_buf[1]
                        cc_val = midi_buf[2]
                        if self.cc_callback:
                            self.cc_callback(cc_num, cc_val)

            time.sleep(0.002)  # 2ms polling

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
