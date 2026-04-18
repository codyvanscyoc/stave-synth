"""FluidSynth player: manages FluidSynth for piano/e-piano soundfont playback."""

import logging
import math
import threading

try:
    import fluidsynth
except ImportError:
    fluidsynth = None

import numpy as np

from pathlib import Path

from .config import SOUNDFONT_DIR, SAMPLE_RATE

logger = logging.getLogger(__name__)

# General MIDI program numbers
SOUNDS = {
    "acoustic_grand_piano": 0,
    "bright_acoustic_piano": 1,
    "electric_grand_piano": 2,
    "honky_tonk_piano": 3,
    "electric_piano_1": 4,   # Rhodes
    "electric_piano_2": 5,   # DX7-style
}


class FluidSynthPlayer:
    """Manages a FluidSynth instance for piano/e-piano playback."""

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        if fluidsynth is None:
            raise RuntimeError(
                "pyfluidsynth not installed. Run: pip install pyfluidsynth"
            )

        self.sample_rate = sample_rate
        self.fs = None
        self.sfid = None
        self.enabled = True
        self.volume = 0.5
        self._volume_cur = 0.5  # smoothed volume for zipper-free changes
        self.current_sound = "acoustic_grand_piano"
        self.current_soundfont = "Arachno"
        self.reverb_dry_wet = 0.4
        self._lock = threading.Lock()

        # Our own DSP chain — 24dB/oct (cascaded biquads) for audible piano EQ
        from .synth_engine import BiquadLowpass, BiquadHighpass
        self.highcut_filter_l = [BiquadLowpass(20000.0, 0.707, sample_rate),
                                 BiquadLowpass(20000.0, 0.707, sample_rate)]
        self.highcut_filter_r = [BiquadLowpass(20000.0, 0.707, sample_rate),
                                 BiquadLowpass(20000.0, 0.707, sample_rate)]
        self.highcut_hz = 20000.0
        self.lowcut_filter_l = [BiquadHighpass(20.0, 0.707, sample_rate),
                                BiquadHighpass(20.0, 0.707, sample_rate)]
        self.lowcut_filter_r = [BiquadHighpass(20.0, 0.707, sample_rate),
                                BiquadHighpass(20.0, 0.707, sample_rate)]
        self.lowcut_hz = 20.0

        # Debug counters
        self._note_on_count = 0
        self._render_count = 0
        self._active_notes = 0  # tracks held notes for render skip optimization
        self._silent_blocks = 0  # count consecutive silent blocks after last note-off
        self._last_raw_peak = 0

        # Compressor state — optical-tube-flavoured defaults. Ratio 3:1 (real
        # optical compressors measure ~3:1 at nominal input despite 4:1 being
        # the often-cited spec). Wide soft knee gives the smooth onset that
        # makes the class of unit feel musical. Makeup always multiplies
        # output (post-gain stage, not gated on reduction).
        self.comp_threshold_db = -20.0
        self.comp_ratio = 3.0
        self.comp_attack_ms = 10.0
        self.comp_release_ms = 80.0
        self.comp_makeup_db = 0.0
        self.comp_knee_db = 18.0
        self.comp_drive_db = 0.0  # input gain INTO the comp (LA-2A-style drive)
        self.comp_wet = 1.0  # parallel-compression dry/wet blend (1 = fully wet)
        self.comp_enabled = False
        self._comp_envelope = 0.0  # current envelope level (linear)

    def start(self, soundfont_name: str = "Arachno"):
        """Initialize FluidSynth and load soundfont.
        Audio is rendered via render_block() — no JACK driver needed."""
        self.fs = fluidsynth.Synth(samplerate=float(self.sample_rate))

        # Configure — gain at 1.0 since our pipeline handles volume
        self.fs.setting("synth.polyphony", 64)
        self.fs.setting("synth.gain", 1.0)
        # FluidSynth's internal reverb stays ON as a dedicated acoustic-room
        # colour for piano. It's intentionally separate from the global pad
        # reverb bus — every real piano sits in a physical space with its
        # own body+room character, so even when the global reverb is on
        # DRONE or BLOOM we want the piano to still sound like a piano in
        # a room, not a piano into a drone tank. Chorus stays off.
        self.fs.setting("synth.reverb.active", 1)
        self.fs.setting("synth.chorus.active", 0)

        # Find soundfont file
        self.current_soundfont = soundfont_name
        sf_path = self._find_soundfont(soundfont_name)
        if sf_path:
            self.sfid = self.fs.sfload(str(sf_path))
            if self.sfid >= 0:
                self.current_soundfont = Path(sf_path).stem
                self.set_sound(self.current_sound)
                logger.info("Loaded soundfont: %s (id=%d)", sf_path, self.sfid)
            else:
                logger.error("Failed to load soundfont: %s — piano disabled", sf_path)
                self.sfid = None
                self.enabled = False
        else:
            logger.error("No soundfont found (tried %s + fallbacks) — piano disabled", soundfont_name)
            self.enabled = False

        if self.sfid is not None:
            logger.info("FluidSynth started (rendered in Python pipeline)")
            # Piano-room reverb defaults: medium room, fair damp, wide
            # stereo. Damp raised to 0.5 (from 0.25) so the reverb tail
            # doesn't accumulate high-frequency hiss when the user opens
            # the tone fader wide — that's where sustained brightness in
            # the tail was showing up as static crackle.
            self.fs.set_reverb_roomsize(0.45)
            self.fs.set_reverb_damp(0.50)
            self.fs.set_reverb_width(0.8)
            self.fs.set_reverb_level(float(self.reverb_dry_wet))
        else:
            logger.warning("FluidSynth started but no soundfont loaded — piano will be silent")

    def _find_soundfont(self, name: str):
        """Search for a soundfont file by name."""
        for ext in (".sf2", ".sf3", ".SF2", ".SF3"):
            path = SOUNDFONT_DIR / f"{name}{ext}"
            if path.exists():
                return path

        # Try common system locations
        import os
        system_dirs = [
            "/usr/share/sounds/sf2",
            "/usr/share/soundfonts",
            "/usr/local/share/soundfonts",
        ]
        for d in system_dirs:
            for ext in (".sf2", ".sf3"):
                path = os.path.join(d, f"{name}{ext}")
                if os.path.exists(path):
                    return path

        # Fallback chain
        fallbacks = ["FluidR3_GM", "TimGM6mb", "default-GM"]
        for fb in fallbacks:
            if fb != name:
                logger.info("Trying fallback soundfont: %s", fb)
                result = self._find_soundfont(fb)
                if result:
                    return result

        return None

    def set_sound(self, sound_name: str):
        """Set the piano sound (General MIDI program)."""
        if self.fs is None or self.sfid is None:
            return

        program = SOUNDS.get(sound_name, 0)
        with self._lock:
            self.fs.program_select(0, self.sfid, 0, program)
            self.current_sound = sound_name
            logger.info("Piano sound set to: %s (program %d)", sound_name, program)

    def note_on(self, note: int, velocity: float):
        """Play a note."""
        if not self.enabled or self.fs is None:
            logger.debug("note_on skipped (enabled=%s, fs=%s)", self.enabled, self.fs is not None)
            return
        vel_midi = max(1, min(127, int(velocity * 127)))
        self._note_on_count += 1
        self._active_notes += 1
        self._silent_blocks = 0
        logger.debug("PIANO note_on: note=%d vel=%d (count=%d)", note, vel_midi, self._note_on_count)
        with self._lock:
            self.fs.noteon(0, note, vel_midi)

    def note_off(self, note: int):
        """Release a note."""
        if self.fs is None:
            return
        self._active_notes = max(0, self._active_notes - 1)
        with self._lock:
            self.fs.noteoff(0, note)

    def all_notes_off(self):
        """Silence all notes."""
        if self.fs is None:
            return
        self._active_notes = 0
        with self._lock:
            for note in range(128):
                self.fs.noteoff(0, note)

    def midi_callback(self, event_type: str, note: int, velocity: float):
        """Callback to be registered with JackEngine for MIDI forwarding."""
        if event_type == "note_on":
            self.note_on(note, velocity)
        elif event_type == "note_off":
            self.note_off(note)
        elif event_type == "all_notes_off":
            self.all_notes_off()

    def set_volume(self, volume: float):
        """Set piano volume (0.0-1.0). Applied in render_block()."""
        self.volume = max(0.0, min(1.0, volume))

    def set_highcut(self, freq_hz: float):
        """Set piano high-cut filter frequency (applied in our DSP pipeline)."""
        self.highcut_hz = max(200.0, min(20000.0, freq_hz))
        for f in self.highcut_filter_l:
            f.set_params(self.highcut_hz, 0.707)
        for f in self.highcut_filter_r:
            f.set_params(self.highcut_hz, 0.707)
        logger.debug("Piano tone: highcut=%dHz", int(self.highcut_hz))

    def set_lowcut(self, freq_hz: float):
        """Set piano low-cut filter frequency (removes rumble/mud)."""
        self.lowcut_hz = max(20.0, min(2000.0, freq_hz))
        for f in self.lowcut_filter_l:
            f.set_params(self.lowcut_hz, 0.707)
        for f in self.lowcut_filter_r:
            f.set_params(self.lowcut_hz, 0.707)
        logger.debug("Piano tone: lowcut=%dHz", int(self.lowcut_hz))

    def render_block(self, n_samples: int) -> np.ndarray:
        """Render FluidSynth audio and apply our DSP chain.
        Returns stereo (2, n) float64 array, ready to mix with synth pad."""
        if self.fs is None or not self.enabled:
            return np.zeros((2, n_samples), dtype=np.float64)

        # Skip rendering when piano is silent (no active notes + release tail finished)
        # ~2s of blocks at 48kHz/256 = ~375 blocks for release tails to decay
        if self._active_notes == 0:
            self._silent_blocks += 1
            if self._silent_blocks > 400:
                return np.zeros((2, n_samples), dtype=np.float64)

        with self._lock:
            if self.fs is None:
                return np.zeros((2, n_samples), dtype=np.float64)
            # get_samples returns interleaved stereo int16, length = 2 * n_samples
            raw = self.fs.get_samples(n_samples)

        self._render_count += 1

        # Convert interleaved int16 stereo to separate L/R float64
        inv_scale = 1.0 / 32768.0
        left = raw[0::2].astype(np.float64) * inv_scale
        right = raw[1::2].astype(np.float64) * inv_scale

        # Smooth volume changes (~10ms time constant at 48kHz/128 block)
        smooth_alpha = 1.0 - np.exp(-n_samples / (0.01 * self.sample_rate))
        self._volume_cur += smooth_alpha * (self.volume - self._volume_cur)

        # Apply volume (dB curve for musical fader response)
        if self._volume_cur <= 0.001:
            left = np.zeros_like(left)
            right = np.zeros_like(right)
        else:
            gain = 10.0 ** ((self._volume_cur - 1.0) * 40.0 / 20.0)  # -40dB to 0dB
            left = left * gain
            right = right * gain

        # Apply low-cut filter — 24dB/oct (cascaded biquads). Always running
        # (no bypass at boundary) so filter state stays fresh — bypass-then-
        # rejoin caused stale-state clicks when user swept the fader back
        # below the threshold.
        for f in self.lowcut_filter_l:
            left = f.process(left)
        for f in self.lowcut_filter_r:
            right = f.process(right)

        # Apply high-cut filter — always running (same reasoning as low-cut).
        # At filter_highcut_hz = 20000 the 24dB response is imperceptible
        # inside the audible band, but keeping state continuous avoids the
        # click when sweeping the tone fader.
        for f in self.highcut_filter_l:
            left = f.process(left)
        for f in self.highcut_filter_r:
            right = f.process(right)

        # Compressor: mono sidechain, stereo gain, parallel wet/dry blend.
        # The fader1-ALT=COMP knob on the front screen maps directly to
        # `comp_wet` (0 = bypass, 1 = fully wet) so the user can dial in
        # "amount of compression" without touching threshold/ratio.
        if self.comp_enabled and self.comp_wet > 0.001:
            mono = (left + right) * 0.5
            compressed = self._compress(mono)
            safe_mono = np.where(np.abs(mono) > 1e-10, mono, 1.0)
            wet_gain = compressed / safe_mono
            # Parallel: out = dry * (1-wet) + compressed * wet, expressed as
            # an effective scalar-per-sample `blend` applied to each channel.
            w = self.comp_wet
            blend = (1.0 - w) + wet_gain * w
            left = left * blend
            right = right * blend

        return np.array([left, right])

    def _compress(self, samples: np.ndarray) -> np.ndarray:
        """LA-2A-flavoured feed-forward compressor. Soft-knee + per-sample
        gain interpolation. The interpolation across the block is critical
        (without it, per-block gain jumps clicked at sample 255→256) and
        the soft knee is what gives the musical optical character —
        compression engages smoothly ~knee/2 dB below threshold instead of
        hard-switching.

        DRIVE is pre-comp input gain (matches LA-2A Gain knob workflow):
        push signal into the fixed-ish threshold without touching makeup.
        The final output applies `drive_gain × reduction × makeup`, so
        DRIVE affects how hard the comp engages but NOT the dry output
        when compressor is unity-at-rest — it's compensated by the signal
        path, not stacked on top."""
        ratio = max(1.0, self.comp_ratio)
        drive_gain = 10.0 ** (self.comp_drive_db / 20.0)
        makeup = 10.0 ** (self.comp_makeup_db / 20.0)
        knee = max(0.1, self.comp_knee_db)
        knee_half = knee * 0.5

        # Envelope sees the DRIVEN signal — that's what makes DRIVE useful:
        # boost signal into the threshold without boosting output dry.
        driven = samples * drive_gain
        rms = float(np.sqrt(np.mean(driven ** 2)))
        attack_ms = max(1.0, self.comp_attack_ms)
        release_ms = max(1.0, self.comp_release_ms)
        attack_coeff = 1.0 - math.exp(-len(samples) / (attack_ms * 0.001 * self.sample_rate))
        release_coeff = 1.0 - math.exp(-len(samples) / (release_ms * 0.001 * self.sample_rate))
        if rms > self._comp_envelope:
            self._comp_envelope += attack_coeff * (rms - self._comp_envelope)
        else:
            self._comp_envelope += release_coeff * (rms - self._comp_envelope)

        env = max(self._comp_envelope, 1e-10)
        env_db = 20.0 * math.log10(env)
        delta_db = env_db - self.comp_threshold_db  # positive = over threshold

        # Quadratic soft knee — standard cookbook form. Smoothly bridges the
        # "no compression" and "full ratio" regimes over `knee` dB total.
        slope = 1.0 - 1.0 / ratio
        if delta_db > knee_half:
            gain_reduction_db = delta_db * slope
        elif delta_db > -knee_half:
            x = delta_db + knee_half  # 0..knee
            gain_reduction_db = slope * (x * x) / (2.0 * knee)
        else:
            gain_reduction_db = 0.0

        # Final per-sample gain: drive boosts into comp, reduction pulls
        # peaks down, makeup adjusts output. At drive=0dB + reduction=0dB +
        # makeup=0dB, gain == 1.0 and output is identical to input.
        reduction = 10.0 ** (-gain_reduction_db / 20.0)
        gain = drive_gain * reduction * makeup

        # Ramp from previous block's gain to this block's — kills the
        # sample-255→256 discontinuity that caused audible clicks.
        prev_gain = getattr(self, "_prev_comp_gain", gain)
        n = len(samples)
        gain_ramp = np.linspace(prev_gain, gain, n, dtype=samples.dtype)
        self._prev_comp_gain = gain
        return samples * gain_ramp

    def update_params(self, params: dict):
        """Update piano parameters from dict."""
        if "volume" in params:
            self.set_volume(float(params["volume"]))
        if "enabled" in params:
            self.enabled = bool(params["enabled"])
        if "sound" in params:
            self.set_sound(params["sound"])
        if "filter_highcut_hz" in params:
            self.set_highcut(float(params["filter_highcut_hz"]))
        if "filter_lowcut_hz" in params:
            self.set_lowcut(float(params["filter_lowcut_hz"]))
        if "reverb_dry_wet" in params:
            # Maps directly to FluidSynth's internal reverb level (0..1).
            # Keeping the state key as `reverb_dry_wet` for backward-compat
            # with saved presets even though semantically it's "wet level".
            self.reverb_dry_wet = max(0.0, min(1.0, float(params["reverb_dry_wet"])))
            if self.fs:
                self.fs.set_reverb_level(self.reverb_dry_wet)
        if "comp_enabled" in params:
            self.comp_enabled = bool(params["comp_enabled"])
        if "comp_threshold_db" in params:
            self.comp_threshold_db = float(params["comp_threshold_db"])
        if "comp_ratio" in params:
            self.comp_ratio = max(1.0, float(params["comp_ratio"]))
        if "comp_makeup_db" in params:
            self.comp_makeup_db = float(params["comp_makeup_db"])
        if "comp_knee_db" in params:
            self.comp_knee_db = max(0.0, min(24.0, float(params["comp_knee_db"])))
        if "comp_wet" in params:
            self.comp_wet = max(0.0, min(1.0, float(params["comp_wet"])))
        if "comp_attack_ms" in params:
            self.comp_attack_ms = max(0.5, min(200.0, float(params["comp_attack_ms"])))
        if "comp_release_ms" in params:
            self.comp_release_ms = max(5.0, min(2000.0, float(params["comp_release_ms"])))
        if "comp_drive_db" in params:
            self.comp_drive_db = max(-12.0, min(12.0, float(params["comp_drive_db"])))

    def get_params(self) -> dict:
        return {
            "enabled": self.enabled,
            "soundfont": self.current_soundfont,
            "sound": self.current_sound,
            "filter_highcut_hz": self.highcut_hz,
            "filter_lowcut_hz": self.lowcut_hz,
            "volume": self.volume,
            "reverb_dry_wet": self.reverb_dry_wet,
            "comp_enabled": self.comp_enabled,
            "comp_threshold_db": self.comp_threshold_db,
            "comp_ratio": self.comp_ratio,
            "comp_makeup_db": self.comp_makeup_db,
            "comp_knee_db": self.comp_knee_db,
            "comp_drive_db": self.comp_drive_db,
            "comp_wet": self.comp_wet,
        }

    def stop(self):
        """Shut down FluidSynth."""
        self.all_notes_off()
        with self._lock:
            if self.fs:
                try:
                    self.fs.delete()
                except Exception as e:
                    logger.warning("Error stopping FluidSynth: %s", e)
                self.fs = None
        logger.info("FluidSynth stopped")
