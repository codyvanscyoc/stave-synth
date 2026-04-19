"""B3 Hammond organ engine: realistic tonewheel synthesis + Leslie speaker.

Realism features baked in (no UI settings needed):
1. Split Leslie — horn (>800Hz) and drum (<800Hz) with independent speeds + Doppler
2. Tonewheel imperfection — slight 2nd/3rd harmonics on each drawbar sine
3. Tonewheel crosstalk — subtle leakage from adjacent tonewheels (breathy quality)
4. Soft overdrive — gentle tanh saturation for tube-amp warmth

Matches FluidSynthPlayer's interface for seamless swapping on fader 1.
"""

import threading
import numpy as np

try:
    from scipy.signal import lfilter
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

from .synth_engine import (
    OnePole6dBLowpass, OnePole6dBHighpass,
    BiquadLowpass, BiquadHighpass, BiquadPeakingEQ,
)

SAMPLE_RATE = 48000
TWO_PI = 2.0 * np.pi

# Hammond B3 drawbar harmonic ratios (footage → frequency multiplier)
HARMONIC_RATIOS = np.array([0.5, 1.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0],
                           dtype=np.float64)

# Tonewheel imperfection: each sine gets slight 2nd and 3rd harmonic content.
# Real tonewheels aren't perfect sines — the metal wheel shape and EM pickup
# introduce small amounts of harmonic distortion. This gives the "gritty alive" quality.
# Values are relative to the fundamental of each tonewheel.
TW_2ND_HARMONIC = 0.02   # 2% second harmonic — subtle grit
TW_3RD_HARMONIC = 0.008  # 0.8% third harmonic — barely there

# Tonewheel crosstalk: adjacent tonewheels leak into each other's pickups.
# This adds a subtle breathy/noisy quality that's a signature B3 trait.
CROSSTALK_LEVEL = 0.008  # -42dB — subtle but audible

# Named drawbar presets (0-8 scale, like a real B3)
ORGAN_PRESETS = {
    "mellow":  [8, 0, 6, 4, 0, 0, 0, 0, 0],  # Coldplay "Fix You" — warm, simple
    "full":    [8, 6, 8, 8, 6, 6, 4, 4, 4],  # Full registration
    "gospel":  [8, 8, 8, 6, 4, 4, 2, 2, 2],  # Gospel/blues
    "jazz":    [8, 0, 8, 0, 0, 0, 0, 0, 0],  # Mellow jazz — sub + fundamental
}

# Leslie speaker speeds (Hz)
LESLIE_SLOW_HZ = 0.8     # Chorale — gentle rotation
LESLIE_FAST_HZ = 6.5     # Tremolo — fast spin
# Horn and drum have different ramp times (horn is lighter, spins up faster)
HORN_RAMP_SEC = 0.8      # horn reaches target speed in ~0.8s
DRUM_RAMP_SEC = 3.0      # drum is heavy, takes ~3s
# Leslie crossover frequency (horn above, drum below)
LESLIE_CROSSOVER_HZ = 800.0
# Doppler depth in samples (horn moves fast enough for pitch wobble)
HORN_DOPPLER_SAMPLES = 12.0  # ±12 samples at 48kHz ≈ ±0.25ms — subtle pitch wobble


class OrganVoice:
    """Lightweight per-note voice for the organ."""

    __slots__ = ('note', 'frequency', 'velocity', 'phases',
                 'click_remaining', 'attack_remaining', 'attack_total',
                 'release_remaining', 'release_total', 'releasing')

    def __init__(self, note: int, velocity: float, sample_rate: int = SAMPLE_RATE):
        self.note = note
        self.frequency = 440.0 * (2.0 ** ((note - 69) / 12.0))
        self.velocity = velocity
        self.phases = np.zeros(9, dtype=np.float64)
        self.click_remaining = int(0.002 * sample_rate)  # 2ms click burst
        self.attack_remaining = 0
        self.attack_total = 0
        self.release_remaining = 0
        self.release_total = int(0.010 * sample_rate)
        self.releasing = False

    def start_release(self, release_samples: int):
        if not self.releasing:
            self.releasing = True
            self.release_total = max(1, release_samples)
            self.release_remaining = self.release_total


class OrganEngine:
    """Synthesized B3 Hammond organ with realistic Leslie speaker effect."""

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.enabled = False
        self.volume = 0.5

        # Drawbar registration
        self.preset = "mellow"
        self.drawbars = list(ORGAN_PRESETS["mellow"])
        self._drawbar_amps = self._compute_amps()

        # Overdrive (tube amp warmth) — 0=clean, 1=moderate warmth
        self.drive = 0.05  # very subtle by default

        # Key click
        self.click_enabled = True
        self.click_level = 0.3

        # Soft attack / release (ms). Defaults preserve original character:
        # attack 0 = instant (click audible), release 10 = tight B3 decay.
        self.attack_ms = 0.0
        self.release_ms = 10.0

        # Leslie speaker (user-facing controls)
        self.leslie_speed = "slow"
        self.leslie_depth = 0.3

        # ── Split Leslie internals ──
        # Horn rotor (treble, >800Hz) — lighter, faster ramp
        self._horn_phase = 0.0
        self._horn_current_hz = LESLIE_SLOW_HZ
        # Drum rotor (bass, <800Hz) — heavier, slower ramp
        self._drum_phase = 0.0
        self._drum_current_hz = LESLIE_SLOW_HZ * 0.85  # drum slightly slower than horn

        # Leslie crossover filters (stereo: L/R for horn, L/R for drum)
        self._xover_hp_l = BiquadHighpass(LESLIE_CROSSOVER_HZ, 0.707, sample_rate)
        self._xover_hp_r = BiquadHighpass(LESLIE_CROSSOVER_HZ, 0.707, sample_rate)
        self._xover_lp_l = BiquadLowpass(LESLIE_CROSSOVER_HZ, 0.707, sample_rate)
        self._xover_lp_r = BiquadLowpass(LESLIE_CROSSOVER_HZ, 0.707, sample_rate)

        # Horn Doppler delay line (variable delay for pitch wobble)
        # Must be larger than any single render block (typically 1024 samples)
        self._doppler_buf_size = max(int(0.05 * sample_rate), 4096)  # ~50ms or 4096
        self._doppler_buf_l = np.zeros(self._doppler_buf_size, dtype=np.float64)
        self._doppler_buf_r = np.zeros(self._doppler_buf_size, dtype=np.float64)
        self._doppler_pos = 0

        # Tone shaping
        self.highcut_hz = 8000.0
        self.lowcut_hz = 40.0
        self.highcut_filter_l = OnePole6dBLowpass(8000.0, sample_rate)
        self.highcut_filter_r = OnePole6dBLowpass(8000.0, sample_rate)
        self.lowcut_filter_l = OnePole6dBHighpass(40.0, sample_rate)
        self.lowcut_filter_r = OnePole6dBHighpass(40.0, sample_rate)

        # Tone tilt (fader TONE in UI): 0=warm, 0.5=flat, 1=bright. Volume-
        # neutral. Mirrors faust/organ.dsp tilt_amount = (tone_tilt-0.5)*2:
        # one peak EQ at 250 Hz (low), one at 3 kHz (high), gains ±4 dB.
        self.tone_tilt = 0.5
        self._tilt_eq_low_l = BiquadPeakingEQ(250.0, 0.0, 1.0, sample_rate)
        self._tilt_eq_low_r = BiquadPeakingEQ(250.0, 0.0, 1.0, sample_rate)
        self._tilt_eq_high_l = BiquadPeakingEQ(3000.0, 0.0, 1.0, sample_rate)
        self._tilt_eq_high_r = BiquadPeakingEQ(3000.0, 0.0, 1.0, sample_rate)

        # Keyboard stereo width — mid/side scaling on the post-Leslie stereo
        # bus. 0=mono (side collapsed), 1=full preserved stereo. Matches the
        # Faust organ's `width` 0..1 semantic so the UI fader behaves
        # identically across backends. (Faust scales per-voice keyboard pan;
        # Python applies M/S scaling to the post-Leslie stereo bus — different
        # mechanism, same audible direction.)
        self.width = 1.0

        # Voices
        self.voices: dict[int, OrganVoice] = {}
        self._lock = threading.Lock()

        # Pre-allocated click noise (band-limited)
        self._click_sample = self._generate_click_sample(sample_rate)

    def _generate_click_sample(self, sample_rate: int) -> np.ndarray:
        """Generate the B3 key-click sample: filtered noise with fast decay."""
        click_len = int(0.003 * sample_rate)  # 3ms
        raw_noise = np.random.default_rng(42).standard_normal(click_len)
        if HAS_SCIPY:
            # Bandpass: 1kHz highpass + 4kHz lowpass
            w_hp = TWO_PI * 1000.0 / sample_rate
            a_hp = 1.0 / (1.0 + w_hp)
            raw_noise = lfilter(np.array([a_hp, -a_hp]), np.array([1.0, -a_hp]), raw_noise)
            w_lp = TWO_PI * 4000.0 / sample_rate
            a_lp = w_lp / (1.0 + w_lp)
            raw_noise = lfilter(np.array([a_lp]), np.array([1.0, -(1.0 - a_lp)]), raw_noise)
        # Fast exponential decay
        decay = np.exp(-np.arange(click_len, dtype=np.float64) / (0.0008 * sample_rate))
        return (raw_noise * decay).astype(np.float64)

    def _compute_amps(self) -> np.ndarray:
        """Normalize drawbar levels (0-8) to amplitudes with headroom."""
        raw = np.array(self.drawbars, dtype=np.float64) / 8.0
        total = raw.sum()
        if total > 0:
            raw *= (1.0 / max(total, 1.0))
        return raw

    def note_on(self, note: int, velocity: float):
        if not self.enabled:
            return
        attack_samples = int(self.attack_ms * 0.001 * self.sample_rate)
        with self._lock:
            if note in self.voices and not self.voices[note].releasing:
                v = self.voices[note]
                v.velocity = velocity
                v.click_remaining = int(0.002 * self.sample_rate)
                v.attack_remaining = attack_samples
                v.attack_total = attack_samples
                return
            v = OrganVoice(note, velocity, self.sample_rate)
            v.attack_remaining = attack_samples
            v.attack_total = attack_samples
            self.voices[note] = v

    def _release_samples(self) -> int:
        return max(1, int(self.release_ms * 0.001 * self.sample_rate))

    def note_off(self, note: int):
        with self._lock:
            if note in self.voices:
                self.voices[note].start_release(self._release_samples())

    def all_notes_off(self):
        rs = self._release_samples()
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

    def set_volume(self, volume: float):
        self.volume = max(0.0, min(1.0, volume))

    def set_highcut(self, freq_hz: float):
        self.highcut_hz = max(200.0, min(12000.0, freq_hz))
        self.highcut_filter_l.set_params(self.highcut_hz)
        self.highcut_filter_r.set_params(self.highcut_hz)

    def set_lowcut(self, freq_hz: float):
        self.lowcut_hz = max(20.0, min(500.0, freq_hz))
        self.lowcut_filter_l.set_params(self.lowcut_hz)
        self.lowcut_filter_r.set_params(self.lowcut_hz)

    def set_tone_tilt(self, t: float):
        """0=warm (lows up, highs down), 0.5=flat, 1=bright. Volume-neutral.
        Maps to two peak EQs at 250 Hz and 3 kHz with complementary gains."""
        self.tone_tilt = max(0.0, min(1.0, float(t)))
        tilt_amount = (self.tone_tilt - 0.5) * 2.0  # -1..+1
        low_db = -tilt_amount * 4.0
        high_db = tilt_amount * 4.0
        self._tilt_eq_low_l.set_params(250.0, low_db, 1.0)
        self._tilt_eq_low_r.set_params(250.0, low_db, 1.0)
        self._tilt_eq_high_l.set_params(3000.0, high_db, 1.0)
        self._tilt_eq_high_r.set_params(3000.0, high_db, 1.0)

    def set_width(self, w: float):
        """Stereo width scaler on the master bus. 0=mono, 1=normal."""
        self.width = max(0.0, min(1.0, float(w)))

    def set_preset(self, name: str):
        if name in ORGAN_PRESETS:
            self.preset = name
            self.drawbars = list(ORGAN_PRESETS[name])
            self._drawbar_amps = self._compute_amps()

    def render_block(self, n_samples: int) -> np.ndarray:
        """Render organ audio. Returns stereo (2, n) float64."""
        if not self.enabled:
            return np.zeros((2, n_samples), dtype=np.float64)

        sr = self.sample_rate
        mono = np.zeros(n_samples, dtype=np.float64)
        dead_notes = []
        amps = self._drawbar_amps
        click_sample = self._click_sample
        click_len = len(click_sample)

        with self._lock:
            voices = list(self.voices.items())

        indices = np.arange(1, n_samples + 1, dtype=np.float64)

        for note, voice in voices:
            # ── Tonewheel synthesis with imperfection ──
            freqs = voice.frequency * HARMONIC_RATIOS  # (9,)
            phase_incs = TWO_PI * freqs / sr  # (9,)

            # Phase arrays: (9, n_samples)
            phases = voice.phases[:, np.newaxis] + phase_incs[:, np.newaxis] * indices

            # Each tonewheel: fundamental + slight 2nd + slight 3rd harmonic
            # This is what makes it sound like a real tonewheel, not a digital sine
            tw_signal = (np.sin(phases)
                         + TW_2ND_HARMONIC * np.sin(2.0 * phases)
                         + TW_3RD_HARMONIC * np.sin(3.0 * phases))

            # Weight by drawbar amplitudes and sum
            signal = np.sum(tw_signal * amps[:, np.newaxis], axis=0)

            # ── Tonewheel crosstalk ──
            # Adjacent tonewheels leak into each other. We approximate this as
            # a tiny bit of the neighboring harmonics bleeding through.
            # Shift amps array left and right to simulate adjacent wheel leakage.
            if CROSSTALK_LEVEL > 0:
                amps_shifted_up = np.roll(amps, -1)
                amps_shifted_up[-1] = 0.0
                amps_shifted_dn = np.roll(amps, 1)
                amps_shifted_dn[0] = 0.0
                crosstalk = np.sum(tw_signal * (amps_shifted_up + amps_shifted_dn)[:, np.newaxis],
                                   axis=0) * CROSSTALK_LEVEL
                signal += crosstalk

            # Update phases
            voice.phases = (voice.phases + phase_incs * n_samples) % TWO_PI

            # Velocity
            signal *= voice.velocity

            # Key click
            if self.click_enabled and voice.click_remaining > 0:
                click_start = click_len - voice.click_remaining
                if click_start < 0:
                    click_start = 0
                click_end = min(click_start + min(voice.click_remaining, n_samples), click_len)
                actual_n = click_end - click_start
                if actual_n > 0:
                    signal[:actual_n] += click_sample[click_start:click_end] * self.click_level
                voice.click_remaining = max(0, voice.click_remaining - n_samples)

            # Soft attack ramp (0 → 1 over attack_total samples). Ramps both the
            # tonewheel content AND the click, so longer attack = softer key start.
            if voice.attack_remaining > 0 and voice.attack_total > 0:
                total = voice.attack_total
                done = total - voice.attack_remaining
                end_idx = min(voice.attack_remaining, n_samples)
                ramp = np.linspace(done / total,
                                   (done + end_idx) / total,
                                   end_idx, dtype=np.float64)
                np.clip(ramp, 0.0, 1.0, out=ramp)
                signal[:end_idx] *= ramp
                voice.attack_remaining = max(0, voice.attack_remaining - n_samples)

            # Release fade (linear, length = voice.release_total)
            if voice.releasing:
                remaining = voice.release_remaining
                release_samples = max(1, voice.release_total)
                if remaining <= 0:
                    dead_notes.append(note)
                    continue
                if remaining >= n_samples:
                    fade = np.linspace(remaining / release_samples,
                                       (remaining - n_samples) / release_samples,
                                       n_samples, dtype=np.float64)
                    np.clip(fade, 0.0, 1.0, out=fade)
                    signal *= fade
                    voice.release_remaining -= n_samples
                else:
                    fade = np.linspace(remaining / release_samples, 0.0,
                                       remaining, dtype=np.float64)
                    np.clip(fade, 0.0, 1.0, out=fade)
                    signal[:remaining] *= fade
                    signal[remaining:] = 0.0
                    voice.release_remaining = 0
                    dead_notes.append(note)

            mono += signal

        # Remove dead voices
        if dead_notes:
            with self._lock:
                for n in dead_notes:
                    self.voices.pop(n, None)

        # ── Soft overdrive (tube amp warmth) ──
        # drive 0=clean (bypass), 1=heavy saturation
        if self.drive > 0.01:
            # Map drive 0-1 to gain multiplier 1.0-1.5 (very gentle range)
            drive_gain = 1.0 + self.drive * 0.5
            mono = np.tanh(mono * drive_gain) / np.tanh(drive_gain)

        # ── Split Leslie speaker ──
        depth = self.leslie_depth
        target_hz = LESLIE_FAST_HZ if self.leslie_speed == "fast" else LESLIE_SLOW_HZ

        # Horn ramp (fast — ~0.8s time constant)
        horn_smooth = 1.0 - np.exp(-n_samples / (HORN_RAMP_SEC * sr))
        self._horn_current_hz += horn_smooth * (target_hz - self._horn_current_hz)

        # Drum ramp (slow — ~3s time constant, and drum is ~85% of horn speed)
        drum_target = target_hz * 0.85
        drum_smooth = 1.0 - np.exp(-n_samples / (DRUM_RAMP_SEC * sr))
        self._drum_current_hz += drum_smooth * (drum_target - self._drum_current_hz)

        # Create initial stereo from mono (before Leslie splits it)
        left = mono.copy()
        right = mono.copy()

        # ── Crossover: split into horn (highpass) and drum (lowpass) ──
        horn_l = self._xover_hp_l.process(left)
        horn_r = self._xover_hp_r.process(right)
        drum_l = self._xover_lp_l.process(left)
        drum_r = self._xover_lp_r.process(right)

        # ── Horn rotor: amplitude modulation + Doppler pitch wobble ──
        horn_inc = TWO_PI * self._horn_current_hz / sr
        horn_phases = self._horn_phase + horn_inc * np.arange(n_samples, dtype=np.float64)
        self._horn_phase = (self._horn_phase + horn_inc * n_samples) % TWO_PI

        horn_sin = np.sin(horn_phases)
        horn_cos = np.cos(horn_phases)  # 90° offset for other channel

        # Amplitude modulation (L and R get opposite sides of the rotation)
        horn_l_out = horn_l * (1.0 + depth * horn_sin)
        horn_r_out = horn_r * (1.0 + depth * horn_cos)

        # Doppler effect on horn: variable delay modulated by rotor position
        # Write into delay buffer, read back with modulated delay
        doppler_depth = HORN_DOPPLER_SAMPLES * depth
        if doppler_depth > 0.5:
            bs = self._doppler_buf_size
            pos = self._doppler_pos
            # Write current horn output into circular buffer
            end = pos + n_samples
            if end <= bs:
                self._doppler_buf_l[pos:end] = horn_l_out
                self._doppler_buf_r[pos:end] = horn_r_out
            else:
                first = bs - pos
                self._doppler_buf_l[pos:bs] = horn_l_out[:first]
                self._doppler_buf_l[:end - bs] = horn_l_out[first:]
                self._doppler_buf_r[pos:bs] = horn_r_out[:first]
                self._doppler_buf_r[:end - bs] = horn_r_out[first:]

            # Read with modulated delay (linear interpolation)
            base_delay = doppler_depth + 2.0  # center offset
            mod_offset = horn_sin * doppler_depth  # oscillates ±doppler_depth
            sample_idx = np.arange(n_samples, dtype=np.float64)
            float_pos = (pos + sample_idx - base_delay - mod_offset) % bs
            pos0 = float_pos.astype(np.int64) % bs
            pos1 = (pos0 + 1) % bs
            frac = float_pos - np.floor(float_pos)
            horn_l_out = self._doppler_buf_l[pos0] * (1.0 - frac) + self._doppler_buf_l[pos1] * frac
            horn_r_out = self._doppler_buf_r[pos0] * (1.0 - frac) + self._doppler_buf_r[pos1] * frac

            self._doppler_pos = end % bs

        # ── Drum rotor: amplitude modulation only (too heavy for Doppler) ──
        drum_inc = TWO_PI * self._drum_current_hz / sr
        drum_phases = self._drum_phase + drum_inc * np.arange(n_samples, dtype=np.float64)
        self._drum_phase = (self._drum_phase + drum_inc * n_samples) % TWO_PI

        drum_sin = np.sin(drum_phases)
        drum_cos = np.cos(drum_phases)

        # Drum modulation is gentler (heavier rotor, less dramatic effect)
        drum_depth = depth * 0.5
        drum_l_out = drum_l * (1.0 + drum_depth * drum_sin)
        drum_r_out = drum_r * (1.0 + drum_depth * drum_cos)

        # ── Recombine horn + drum ──
        left = horn_l_out + drum_l_out
        right = horn_r_out + drum_r_out

        # ── Tone tilt EQ (matches faust/organ.dsp tone_tilt_eq) ──
        # Applied pre-volume so it shapes the source spectrum, not the master
        # gain. Even at 0/1 it stays roughly volume-neutral (push/pull is
        # symmetric ±4 dB at 250 Hz / 3 kHz).
        if abs(self.tone_tilt - 0.5) > 1e-3:
            left = self._tilt_eq_low_l.process(left)
            right = self._tilt_eq_low_r.process(right)
            left = self._tilt_eq_high_l.process(left)
            right = self._tilt_eq_high_r.process(right)

        # Volume (dB curve: -40dB to 0dB)
        if self.volume <= 0.0:
            return np.zeros((2, n_samples), dtype=np.float64)
        gain = 10.0 ** ((self.volume - 1.0) * 40.0 / 20.0)
        left *= gain
        right *= gain

        # Tone shaping
        if self.lowcut_hz > 25.0:
            left = self.lowcut_filter_l.process(left)
            right = self.lowcut_filter_r.process(right)
        if self.highcut_hz < 11000.0:
            left = self.highcut_filter_l.process(left)
            right = self.highcut_filter_r.process(right)

        # ── Mid/side stereo width ──
        # Standard mid/side scaling: width=1 leaves stereo unchanged, width=0
        # collapses to mono. Faust organ scales per-voice keyboard pan; this
        # post-Leslie M/S approach is the closest equivalent in the mono-
        # tonewheel-then-stereo-Leslie Python pipeline. 0..1 range matches UI.
        if abs(self.width - 1.0) > 1e-3:
            mid = (left + right) * 0.5
            side = (left - right) * 0.5
            left = mid + side * self.width
            right = mid - side * self.width

        return np.array([left, right])

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
                self._drawbar_amps = self._compute_amps()
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
