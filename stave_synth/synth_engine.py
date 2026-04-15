"""Synth pad engine: oscillators, filter, ADSR, reverb, shimmer, voice management.

Performance-critical: all audio processing uses vectorized NumPy.
"""

import numpy as np
from dataclasses import dataclass, field

try:
    from scipy.signal import lfilter
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

SAMPLE_RATE = 48000
TWO_PI = 2.0 * np.pi

# dB volume curves: maps a 0-1 fader to amplitude via exponential (dB) scaling.
# Hard digital zero at fader=0.
MASTER_DB_RANGE = 60.0  # -60dB to 0dB for master/volume faders
BLEND_DB_RANGE = 24.0   # -24dB to 0dB for oscillator blend faders (gentler taper)

# Available oscillator waveforms
WAVEFORMS = ["sine", "square", "saw", "triangle", "saturated"]

def fader_to_amplitude(fader: float) -> float:
    """Convert a 0-1 fader position to amplitude using dB curve (-60dB range).
    0 -> silence, 1 -> amplitude 1.0 (0dB)."""
    if fader <= 0.0:
        return 0.0
    return 10.0 ** ((fader - 1.0) * MASTER_DB_RANGE / 20.0)

def blend_to_amplitude(fader: float) -> float:
    """Convert a 0-1 blend fader to amplitude using gentler dB curve (-24dB range).
    Better for oscillator mix levels where mid-range positions need to be audible."""
    if fader <= 0.0:
        return 0.0
    return 10.0 ** ((fader - 1.0) * BLEND_DB_RANGE / 20.0)

def generate_waveform(waveform: str, phases: np.ndarray) -> np.ndarray:
    """Generate a waveform from phase array. All outputs are roughly -1 to +1."""
    if waveform == "square":
        return np.sign(np.sin(phases))
    elif waveform == "saw":
        return 2.0 * ((phases / TWO_PI) % 1.0) - 1.0
    elif waveform == "triangle":
        return 2.0 * np.abs(2.0 * ((phases / TWO_PI) % 1.0) - 1.0) - 1.0
    elif waveform == "saturated":
        return np.tanh(4.0 * np.sin(phases))
    # Default: sine
    return np.sin(phases)


@dataclass
class ADSRConfig:
    attack_ms: float = 200.0
    decay_ms: float = 1500.0
    sustain_percent: float = 80.0
    release_ms: float = 500.0


class ADSREnvelope:
    """Per-voice ADSR envelope — block-level vectorized processing."""

    ATTACK = 0
    DECAY = 1
    SUSTAIN = 2
    RELEASE = 3
    OFF = 4

    def __init__(self, config: ADSRConfig):
        self.config = config
        self.stage = self.OFF
        self.level = 0.0

    def trigger(self):
        self.stage = self.ATTACK

    def release(self):
        if self.stage != self.OFF:
            self.stage = self.RELEASE

    def is_active(self):
        return self.stage != self.OFF

    def process(self, n_samples: int) -> np.ndarray:
        if self.stage == self.OFF:
            return np.zeros(n_samples, dtype=np.float64)

        if self.stage == self.SUSTAIN:
            self.level = self.config.sustain_percent / 100.0
            return np.full(n_samples, self.level, dtype=np.float64)

        if self.stage == self.ATTACK:
            rate = 1.0 / max(self.config.attack_ms * SAMPLE_RATE / 1000.0, 1.0)
            end_level = self.level + rate * n_samples
            if end_level >= 1.0:
                attack_samples = min(int((1.0 - self.level) / rate) + 1, n_samples)
                out = np.linspace(self.level, 1.0, attack_samples, dtype=np.float64)
                self.level = 1.0
                self.stage = self.DECAY
                if attack_samples < n_samples:
                    out = np.concatenate([out, self._decay_block(n_samples - attack_samples)])
                return out
            out = np.linspace(self.level, end_level, n_samples, dtype=np.float64)
            self.level = end_level
            return out

        if self.stage == self.DECAY:
            return self._decay_block(n_samples)

        if self.stage == self.RELEASE:
            rate = 1.0 / max(self.config.release_ms * SAMPLE_RATE / 1000.0, 1.0)
            factors = (1.0 - rate) ** np.arange(1, n_samples + 1, dtype=np.float64)
            out = self.level * factors
            self.level = out[-1] if n_samples > 0 else self.level
            if self.level < 0.001:
                self.level = 0.0
                self.stage = self.OFF
            return out

        return np.full(n_samples, self.level, dtype=np.float64)

    def _decay_block(self, n_samples: int) -> np.ndarray:
        sustain = self.config.sustain_percent / 100.0
        rate = 1.0 / max(self.config.decay_ms * SAMPLE_RATE / 1000.0, 1.0)
        diff = self.level - sustain
        factors = (1.0 - rate) ** np.arange(1, n_samples + 1, dtype=np.float64)
        out = sustain + diff * factors
        self.level = out[-1] if n_samples > 0 else self.level
        if abs(self.level - sustain) < 0.001:
            self.level = sustain
            self.stage = self.SUSTAIN
        return out


class OnePole6dBLowpass:
    """Gentle 6dB/oct (1-pole) lowpass — transparent for EQ use.
    Uses scipy.signal.lfilter when available for vectorized processing."""

    def __init__(self, cutoff_hz: float = 20000.0, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self._zi = np.zeros(1, dtype=np.float64)
        self._b = np.zeros(1, dtype=np.float64)
        self._a = np.zeros(2, dtype=np.float64)
        self.set_params(cutoff_hz)

    def set_params(self, cutoff_hz: float):
        cutoff_hz = max(20.0, min(cutoff_hz, self.sample_rate * 0.45))
        w = TWO_PI * cutoff_hz / self.sample_rate
        a = w / (1.0 + w)
        self._b[0] = a
        self._a[0] = 1.0
        self._a[1] = -(1.0 - a)

    def process(self, samples: np.ndarray) -> np.ndarray:
        if HAS_SCIPY:
            out, self._zi = lfilter(self._b, self._a, samples, zi=self._zi)
            return out
        # Fallback: per-sample loop
        a = self._b[0]
        y1 = self._zi[0]
        out = np.empty(len(samples), dtype=np.float64)
        for i in range(len(samples)):
            y1 += a * (samples[i] - y1)
            out[i] = y1
        self._zi[0] = y1
        return out

    def reset(self):
        self._zi[:] = 0.0


class OnePole6dBHighpass:
    """Gentle 6dB/oct (1-pole) highpass — transparent for EQ use.
    Uses scipy.signal.lfilter when available for vectorized processing."""

    def __init__(self, cutoff_hz: float = 20.0, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self._zi = np.zeros(1, dtype=np.float64)
        self._b = np.zeros(2, dtype=np.float64)
        self._a_coeff = np.zeros(2, dtype=np.float64)
        self.set_params(cutoff_hz)

    def set_params(self, cutoff_hz: float):
        cutoff_hz = max(20.0, min(cutoff_hz, self.sample_rate * 0.45))
        w = TWO_PI * cutoff_hz / self.sample_rate
        a = 1.0 / (1.0 + w)
        self._b[0] = a
        self._b[1] = -a
        self._a_coeff[0] = 1.0
        self._a_coeff[1] = -a

    def process(self, samples: np.ndarray) -> np.ndarray:
        if HAS_SCIPY:
            out, self._zi = lfilter(self._b, self._a_coeff, samples, zi=self._zi)
            return out
        # Fallback: per-sample loop
        a = self._b[0]
        x1 = self._zi[0] / a if a > 0 else 0.0  # recover x1 from zi state
        y1 = self._zi[0]
        out = np.empty(len(samples), dtype=np.float64)
        for i in range(len(samples)):
            y1 = a * (y1 + samples[i] - x1)
            x1 = samples[i]
            out[i] = y1
        self._zi[0] = y1
        return out

    def reset(self):
        self._zi[:] = 0.0


class BiquadLowpass:
    """Biquad lowpass filter (high-cut) — uses scipy.signal.lfilter when available."""

    def __init__(self, cutoff_hz: float = 8000.0, resonance: float = 0.707,
                 sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.zi = np.zeros(2, dtype=np.float64)
        self.b = np.zeros(3, dtype=np.float64)
        self.a = np.zeros(3, dtype=np.float64)
        self.set_params(cutoff_hz, resonance)

    def set_params(self, cutoff_hz: float, resonance: float):
        cutoff_hz = max(20.0, min(cutoff_hz, self.sample_rate * 0.45))
        resonance = max(0.1, min(resonance, 10.0))
        w0 = TWO_PI * cutoff_hz / self.sample_rate
        cos_w0 = np.cos(w0)
        sin_w0 = np.sin(w0)
        alpha = sin_w0 / (2.0 * resonance)

        a0 = 1.0 + alpha
        self.b[0] = ((1.0 - cos_w0) / 2.0) / a0
        self.b[1] = ((1.0 - cos_w0)) / a0
        self.b[2] = ((1.0 - cos_w0) / 2.0) / a0
        self.a[0] = 1.0
        self.a[1] = (-2.0 * cos_w0) / a0
        self.a[2] = (1.0 - alpha) / a0

    def process(self, samples: np.ndarray) -> np.ndarray:
        if HAS_SCIPY:
            out, self.zi = lfilter(self.b, self.a, samples, zi=self.zi)
            return out
        n = len(samples)
        out = np.empty(n, dtype=np.float64)
        z1, z2 = self.zi[0], self.zi[1]
        b0, b1, b2 = self.b
        a1, a2 = self.a[1], self.a[2]
        for i in range(n):
            x = samples[i]
            y = b0 * x + z1
            z1 = b1 * x - a1 * y + z2
            z2 = b2 * x - a2 * y
            out[i] = y
        self.zi[0], self.zi[1] = z1, z2
        return out

    def reset(self):
        self.zi[:] = 0.0


class BiquadHighpass:
    """Biquad highpass filter — mirrors BiquadLowpass structure."""

    def __init__(self, cutoff_hz: float = 2000.0, resonance: float = 0.707,
                 sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.zi = np.zeros(2, dtype=np.float64)
        self.b = np.zeros(3, dtype=np.float64)
        self.a = np.zeros(3, dtype=np.float64)
        self.set_params(cutoff_hz, resonance)

    def set_params(self, cutoff_hz: float, resonance: float):
        cutoff_hz = max(20.0, min(cutoff_hz, self.sample_rate * 0.45))
        resonance = max(0.1, min(resonance, 10.0))
        w0 = TWO_PI * cutoff_hz / self.sample_rate
        cos_w0 = np.cos(w0)
        sin_w0 = np.sin(w0)
        alpha = sin_w0 / (2.0 * resonance)

        a0 = 1.0 + alpha
        self.b[0] = ((1.0 + cos_w0) / 2.0) / a0
        self.b[1] = (-(1.0 + cos_w0)) / a0
        self.b[2] = ((1.0 + cos_w0) / 2.0) / a0
        self.a[0] = 1.0
        self.a[1] = (-2.0 * cos_w0) / a0
        self.a[2] = (1.0 - alpha) / a0

    def process(self, samples: np.ndarray) -> np.ndarray:
        if HAS_SCIPY:
            out, self.zi = lfilter(self.b, self.a, samples, zi=self.zi)
            return out
        n = len(samples)
        out = np.empty(n, dtype=np.float64)
        z1, z2 = self.zi[0], self.zi[1]
        b0, b1, b2 = self.b
        a1, a2 = self.a[1], self.a[2]
        for i in range(n):
            x = samples[i]
            y = b0 * x + z1
            z1 = b1 * x - a1 * y + z2
            z2 = b2 * x - a2 * y
            out[i] = y
        self.zi[0], self.zi[1] = z1, z2
        return out

    def reset(self):
        self.zi[:] = 0.0


class AllPassDiffuser:
    """Schroeder all-pass via block processing — smears transients.
    y[n] = -g*x[n] + x[n-D] + g*y[n-D]
    Auto-splits into sub-blocks when block_size > delay for correctness.
    """

    def __init__(self, delay_samples: int, gain: float = 0.5):
        self.delay = delay_samples
        self.gain = gain
        buf_size = delay_samples + 8192  # headroom for large blocks
        self.x_buf = np.zeros(buf_size, dtype=np.float64)
        self.y_buf = np.zeros(buf_size, dtype=np.float64)
        self.buf_size = buf_size
        self.pos = delay_samples  # start after initial delay

    def _read(self, buf, offset, n):
        start = offset % self.buf_size
        end = start + n
        if end <= self.buf_size:
            return buf[start:end].copy()
        first = self.buf_size - start
        return np.concatenate([buf[start:], buf[:n - first]])

    def _write(self, buf, offset, data):
        n = len(data)
        start = offset % self.buf_size
        end = start + n
        if end <= self.buf_size:
            buf[start:end] = data
        else:
            first = self.buf_size - start
            buf[start:] = data[:first]
            buf[:n - first] = data[first:]

    def _process_block(self, samples: np.ndarray) -> np.ndarray:
        """Process a single block where len(samples) <= self.delay."""
        n = len(samples)
        g = self.gain
        p = self.pos
        d = self.delay

        x_delayed = self._read(self.x_buf, p - d, n)
        y_delayed = self._read(self.y_buf, p - d, n)

        self._write(self.x_buf, p, samples)

        out = -g * samples + x_delayed + g * y_delayed

        self._write(self.y_buf, p, out)

        self.pos = (p + n) % self.buf_size
        return out

    def process(self, samples: np.ndarray) -> np.ndarray:
        n = len(samples)
        d = self.delay
        if n <= d:
            return self._process_block(samples)
        # Block is larger than delay — split into safe sub-blocks
        out = np.empty(n, dtype=np.float64)
        offset = 0
        while offset < n:
            chunk = min(d, n - offset)
            out[offset:offset + chunk] = self._process_block(samples[offset:offset + chunk])
            offset += chunk
        return out


def _hadamard8():
    """8x8 Hadamard matrix normalized for energy preservation."""
    h = np.array([
        [ 1,  1,  1,  1,  1,  1,  1,  1],
        [ 1, -1,  1, -1,  1, -1,  1, -1],
        [ 1,  1, -1, -1,  1,  1, -1, -1],
        [ 1, -1, -1,  1,  1, -1, -1,  1],
        [ 1,  1,  1,  1, -1, -1, -1, -1],
        [ 1, -1,  1, -1, -1,  1, -1,  1],
        [ 1,  1, -1, -1, -1, -1,  1,  1],
        [ 1, -1, -1,  1, -1,  1,  1, -1],
    ], dtype=np.float64) / np.sqrt(8.0)
    return h

_HADAMARD = _hadamard8()


class FeedbackDelayReverb:
    """Cathedral reverb: early reflections → diffusion → 8-line Hadamard FDN → stereo.

    What makes this musical:
    1. Early reflections give body and spatial cues before the tail builds.
    2. Irregular delay times (no harmonic relationships) prevent metallic ringing.
    3. Deep delay modulation (±24-36 samples via slow LFOs) smears comb modes.
    4. Two-stage damping: one-pole HF rolloff + biquad hi/lo cut on feedback.
    5. True stereo: even-indexed taps → L, odd-indexed taps → R (Hadamard
       mixing ensures maximum decorrelation between channels).
    """

    def __init__(self, decay_seconds: float = 6.0, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.dry_wet = 0.75
        self.wet_gain = 1.0  # trim only (0.5-1.5), not a boost — crossfade handles mix
        self.decay_seconds = decay_seconds
        self.low_cut_hz = 80.0
        self.high_cut_hz = 7000.0
        self.space = 0.0  # 0=tight, 1=massive (stereo width + mod depth + diffusion)
        self.predelay_ms = 25.0  # adjustable pre-delay

        # ── Pre-delay buffer (sized for up to 150ms) ──
        self._predelay_max = int(0.15 * sample_rate)
        self.predelay_samples = int(0.025 * sample_rate)
        self.predelay_buf = np.zeros(self._predelay_max + sample_rate, dtype=np.float64)
        self.predelay_pos = 0

        # ── Early reflections — stereo wall/ceiling bounces ──
        # Pairs of (delay_ms, gain). L and R get different patterns for width.
        er_l_config = [(11.3, 0.72), (23.7, 0.55), (37.1, 0.38), (53.9, 0.22)]
        er_r_config = [(13.9, 0.68), (29.3, 0.48), (43.7, 0.30), (61.3, 0.18)]
        self._er_l_delays = [int(t * sample_rate / 1000) for t, _ in er_l_config]
        self._er_l_gains = np.array([g for _, g in er_l_config], dtype=np.float64)
        self._er_r_delays = [int(t * sample_rate / 1000) for t, _ in er_r_config]
        self._er_r_gains = np.array([g for _, g in er_r_config], dtype=np.float64)
        er_max = max(max(self._er_l_delays), max(self._er_r_delays))
        self._er_buf_size = er_max + sample_rate
        self._er_buf = np.zeros(self._er_buf_size, dtype=np.float64)
        self._er_pos = 0

        # ── Diffusion: 8 all-pass stages with varied delays/gains ──
        ap_config = [
            (11.7, 0.50), (19.3, 0.55), (27.1, 0.45), (33.7, 0.50),
            (41.3, 0.45), (51.9, 0.55), (63.7, 0.50), (79.3, 0.45),
        ]
        self.diffusers = [
            AllPassDiffuser(int(t * sample_rate / 1000), gain=g)
            for t, g in ap_config
        ]

        # ── 8 FDN delay lines — irregular spacing, wide spread ──
        # Chosen for irregular ratios (no harmonic relationships).
        # Adjacent ratios: 1.24, 1.20, 1.17, 1.18, 1.16, 1.16, 1.13
        delay_times_ms = [63.7, 79.3, 95.3, 111.7, 131.9, 153.1, 177.7, 200.9]
        self.n_lines = len(delay_times_ms)
        self.delays = [int(t * sample_rate / 1000) for t in delay_times_ms]

        buf_size = max(self.delays) + sample_rate * 2 + 1024  # extra for modulation
        self.bufs = [np.zeros(buf_size, dtype=np.float64) for _ in range(self.n_lines)]
        self.buf_size = buf_size
        self.write_pos = 0

        self.feedback = 0.0
        self.set_decay(decay_seconds)

        # Damping: one-pole HF rolloff on feedback path
        self.damp_coeff = 0.50
        self._damp_zi = [np.zeros(1, dtype=np.float64) for _ in range(self.n_lines)]

        # ── Delay modulation — deeper, slower LFOs ──
        self._mod_phases = np.zeros(self.n_lines, dtype=np.float64)
        self._mod_rates = np.array([0.23, 0.37, 0.47, 0.61, 0.73, 0.89, 0.31, 0.53],
                                    dtype=np.float64) * TWO_PI / sample_rate
        self._mod_depths_base = np.array([28, 32, 24, 36, 26, 30, 34, 25], dtype=np.float64)
        self._mod_depths = self._mod_depths_base.copy()

        # Feedback frequency bounds
        self.fb_lowcut = BiquadHighpass(80.0, 0.707, sample_rate)
        self.fb_highcut = BiquadLowpass(7000.0, 0.707, sample_rate)
        self._fb_lc_zi = [np.zeros(2, dtype=np.float64) for _ in range(self.n_lines)]
        self._fb_hc_zi = [np.zeros(2, dtype=np.float64) for _ in range(self.n_lines)]

        # ── Output diffusion: allpass stages on wet output for extra smoothness ──
        out_ap_config = [
            (7.3, 0.45), (13.1, 0.40), (5.9, 0.45), (11.7, 0.40),
        ]
        self._out_diffusers_l = [
            AllPassDiffuser(int(t * sample_rate / 1000), gain=g)
            for t, g in out_ap_config
        ]
        self._out_diffusers_r = [
            AllPassDiffuser(int((t + 2.3) * sample_rate / 1000), gain=g)
            for t, g in out_ap_config
        ]

        # Freeze state
        self.frozen = False
        self._normal_feedback = 0.0
        self._normal_damp = self.damp_coeff

    def set_freeze(self, enabled: bool):
        """Freeze the reverb tail — infinite loop with no new input."""
        if enabled and not self.frozen:
            self._normal_feedback = self.feedback
            self._normal_damp = self.damp_coeff
            self.feedback = 0.999
            self.damp_coeff = 0.05  # near-zero damping so it doesn't die
            self.frozen = True
        elif not enabled and self.frozen:
            self.feedback = self._normal_feedback
            self.damp_coeff = self._normal_damp
            self.frozen = False

    def set_space(self, value: float):
        """Set space parameter (0-1). Affects stereo width, mod depth, output diffusion."""
        self.space = max(0.0, min(1.0, value))
        # Scale mod depths: 1x at space=0, up to 2x at space=1
        self._mod_depths = self._mod_depths_base * (1.0 + self.space)

    def set_predelay(self, ms: float):
        """Set pre-delay in milliseconds (0-150ms)."""
        self.predelay_ms = max(0.0, min(150.0, ms))
        self.predelay_samples = int(self.predelay_ms * self.sample_rate / 1000.0)
        self.predelay_samples = min(self.predelay_samples, self._predelay_max)

    def set_decay(self, seconds: float):
        self.decay_seconds = seconds
        if seconds > 0:
            avg_delay = sum(self.delays) / len(self.delays)
            loops_per_sec = self.sample_rate / avg_delay
            self.feedback = 10.0 ** (-3.0 / (seconds * loops_per_sec))
            self.feedback = min(self.feedback, 0.985)
        else:
            self.feedback = 0.0

    def set_low_cut(self, freq_hz: float):
        self.low_cut_hz = max(20.0, freq_hz)
        self.fb_lowcut.set_params(self.low_cut_hz, 0.707)
        for i in range(self.n_lines):
            self._fb_lc_zi[i][:] = 0.0

    def set_high_cut(self, freq_hz: float):
        self.high_cut_hz = min(freq_hz, 20000.0)
        self.fb_highcut.set_params(self.high_cut_hz, 0.707)
        for i in range(self.n_lines):
            self._fb_hc_zi[i][:] = 0.0

    def _write_block(self, buf, data, n):
        end = self.write_pos + n
        if end <= self.buf_size:
            buf[self.write_pos:end] = data
        else:
            first = self.buf_size - self.write_pos
            buf[self.write_pos:self.buf_size] = data[:first]
            buf[:end - self.buf_size] = data[first:]

    def _read_block_modulated(self, buf, base_delay, mod_offset, n):
        """Read with per-sample fractional delay via linear interpolation (vectorized)."""
        bs = self.buf_size
        sample_idx = np.arange(n, dtype=np.float64)
        total_delay = base_delay + mod_offset
        float_pos = (self.write_pos + sample_idx - total_delay) % bs
        pos0 = float_pos.astype(np.int64) % bs
        pos1 = (pos0 + 1) % bs
        frac = float_pos - np.floor(float_pos)
        return buf[pos0] * (1.0 - frac) + buf[pos1] * frac

    def _er_read(self, delay, n):
        """Read from early reflection buffer at given delay."""
        bs = self._er_buf_size
        start = (self._er_pos - delay) % bs
        if start + n <= bs:
            return self._er_buf[start:start + n].copy()
        first = bs - start
        return np.concatenate([self._er_buf[start:bs], self._er_buf[:n - first]])

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Process stereo (2, n) or mono (n,) input, return stereo (2, n) output."""
        if samples.ndim == 2:
            input_l = samples[0]
            input_r = samples[1]
            n = len(input_l)
            mono_input = (input_l + input_r) * 0.5
        else:
            mono_input = samples
            input_l = samples
            input_r = samples
            n = len(samples)
        if n == 0:
            return np.zeros((2, 0), dtype=np.float64)

        # ── Pre-delay (operates on mono sum for ER + diffusion) ──
        pd = self.predelay_samples
        pd_bs = len(self.predelay_buf)
        pd_pos = self.predelay_pos
        end = pd_pos + n
        if end <= pd_bs:
            self.predelay_buf[pd_pos:end] = mono_input
        else:
            first = pd_bs - pd_pos
            self.predelay_buf[pd_pos:pd_bs] = mono_input[:first]
            self.predelay_buf[:end - pd_bs] = mono_input[first:]
        rd_start = (pd_pos - pd) % pd_bs
        if rd_start + n <= pd_bs:
            predelayed = self.predelay_buf[rd_start:rd_start + n].copy()
        else:
            first = pd_bs - rd_start
            predelayed = np.concatenate([
                self.predelay_buf[rd_start:pd_bs],
                self.predelay_buf[:n - first]
            ])
        self.predelay_pos = end % pd_bs

        # ── Early reflections (stereo) ──
        # Write pre-delayed signal into ER buffer
        er_bs = self._er_buf_size
        er_pos = self._er_pos
        er_end = er_pos + n
        if er_end <= er_bs:
            self._er_buf[er_pos:er_end] = predelayed
        else:
            first = er_bs - er_pos
            self._er_buf[er_pos:er_bs] = predelayed[:first]
            self._er_buf[:er_end - er_bs] = predelayed[first:]

        er_left = np.zeros(n, dtype=np.float64)
        er_right = np.zeros(n, dtype=np.float64)
        for i, delay in enumerate(self._er_l_delays):
            er_left += self._er_read(delay, n) * self._er_l_gains[i]
        for i, delay in enumerate(self._er_r_delays):
            er_right += self._er_read(delay, n) * self._er_r_gains[i]
        self._er_pos = er_end % er_bs

        # ── Diffusion: smear input through all-pass chain ──
        diffused = predelayed.copy()
        for ap in self.diffusers:
            diffused = ap.process(diffused)

        fb = self.feedback
        damp = self.damp_coeff
        b_lp = np.array([1.0 - damp])
        a_lp = np.array([1.0, -damp])

        # ── Compute modulation offsets ──
        sample_offsets = np.arange(n, dtype=np.float64)
        mod_offsets = []
        for i in range(self.n_lines):
            phases = self._mod_phases[i] + self._mod_rates[i] * sample_offsets
            mod_offsets.append(np.sin(phases) * self._mod_depths[i])
            self._mod_phases[i] = (self._mod_phases[i] + self._mod_rates[i] * n) % TWO_PI

        # ── Read modulated FDN taps ──
        taps = [self._read_block_modulated(self.bufs[i], self.delays[i], mod_offsets[i], n)
                for i in range(self.n_lines)]

        # ── Stereo wet output: split taps into L/R groups ──
        # Even indices (0,2,4,6) → L, odd indices (1,3,5,7) → R
        # Hadamard mixing already decorrelated them — this gives natural stereo width
        half = self.n_lines // 2
        wet_l = np.zeros(n, dtype=np.float64)
        wet_r = np.zeros(n, dtype=np.float64)
        for i in range(self.n_lines):
            if i % 2 == 0:
                wet_l += taps[i]
            else:
                wet_r += taps[i]
        wet_l *= (1.0 / np.sqrt(half))
        wet_r *= (1.0 / np.sqrt(half))

        # ── Hadamard mixing for feedback ──
        taps_matrix = np.array(taps)  # (8, n)
        mixed = _HADAMARD @ taps_matrix  # (8, n)

        lc_b, lc_a = self.fb_lowcut.b, self.fb_lowcut.a
        hc_b, hc_a = self.fb_highcut.b, self.fb_highcut.a

        # Stereo FDN input: even lines get L, odd lines get R
        # This preserves stereo image in the reverb tail
        if not self.frozen:
            input_l_diff = diffused * 0.5 + input_l * 0.5  # blend diffused mono + direct L
            input_r_diff = diffused * 0.5 + input_r * 0.5  # blend diffused mono + direct R
        else:
            input_l_diff = np.zeros(n, dtype=np.float64)
            input_r_diff = np.zeros(n, dtype=np.float64)

        for i in range(self.n_lines):
            fb_signal = mixed[i] * fb
            if HAS_SCIPY:
                fb_signal, self._damp_zi[i] = lfilter(b_lp, a_lp, fb_signal, zi=self._damp_zi[i])
                fb_signal, self._fb_lc_zi[i] = lfilter(lc_b, lc_a, fb_signal, zi=self._fb_lc_zi[i])
                fb_signal, self._fb_hc_zi[i] = lfilter(hc_b, hc_a, fb_signal, zi=self._fb_hc_zi[i])
            # Even lines fed by L, odd by R
            inp = input_l_diff if (i % 2 == 0) else input_r_diff
            self._write_block(self.bufs[i], inp + fb_signal, n)

        self.write_pos = (self.write_pos + n) % self.buf_size

        # ── Stereo width enhancement (space-dependent) ──
        # Cross-feed: blend opposite channels for wider image
        width = self.space
        if width > 0.0:
            mid = (wet_l + wet_r) * 0.5
            side = (wet_l - wet_r) * 0.5
            # Boost side signal by space amount (up to 2x)
            side *= (1.0 + width)
            wet_l = mid + side
            wet_r = mid - side

        # ── Output diffusion (space-dependent) ──
        if width > 0.0:
            for ap in self._out_diffusers_l:
                wet_l = ap.process(wet_l)
            for ap in self._out_diffusers_r:
                wet_r = ap.process(wet_r)

        # ── Soft-limit wet taps so FDN energy can't clip ──
        wet_l = np.tanh(wet_l)
        wet_r = np.tanh(wet_r)

        # Return wet-only (early reflections + FDN tail)
        er_scale = 0.4 if not self.frozen else 0.0
        out_l = er_left * er_scale + wet_l
        out_r = er_right * er_scale + wet_r

        return np.array([out_l, out_r])


@dataclass
class Voice:
    note: int = 0
    velocity: float = 1.0
    adsr: ADSREnvelope = field(default_factory=lambda: ADSREnvelope(ADSRConfig()))
    phases: list = field(default_factory=list)       # base phase per unison voice
    osc1_phases: list = field(default_factory=list)  # osc1 phase (with octave baked in)
    osc2_phases: list = field(default_factory=list)  # osc2 phase (with octave baked in)
    shimmer_phases: list = field(default_factory=list)  # shimmer phase (octave up)
    age: int = 0

    def is_active(self):
        return self.adsr.is_active()


class SynthEngine:
    """Main synth pad engine managing voices, oscillators, and effects."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, max_voices: int = 16):
        self.sample_rate = sample_rate
        self.max_voices = max_voices

        self.osc1_blend = 0.6
        self.osc2_blend = 0.4
        self._osc1_blend_cur = 0.6
        self._osc2_blend_cur = 0.4
        self.osc1_waveform = "sine"
        self.osc2_waveform = "square"
        self.osc1_octave = 0  # -3 to +3 octave shift for OSC1
        self.osc2_octave = 0  # -3 to +3 octave shift for OSC2
        self.unison_voices = 1
        self.unison_detune = 0.20
        self.unison_spread = 0.85  # stereo spread: 0=mono center, 1=hard L-R

        # Per-oscillator pan and Haas delay
        self.osc1_pan = 0.0   # -1 (full L) to +1 (full R)
        self.osc2_pan = 0.0
        self.osc_hard_pan = False  # shortcut: OSC1 full L, OSC2 full R + Haas
        self._haas_delay_samples = int(0.015 * sample_rate)  # 15ms
        self._haas_buf_size = int(0.030 * sample_rate) + 512  # room for 30ms
        self._haas_buf_l = np.zeros(self._haas_buf_size, dtype=np.float64)
        self._haas_buf_r = np.zeros(self._haas_buf_size, dtype=np.float64)
        self._haas_pos = 0

        self.adsr_config = ADSRConfig()

        # Stereo filter pairs (L/R share params, independent state)
        self.filter_l = BiquadLowpass(8000.0, 0.707, sample_rate)
        self.filter_r = BiquadLowpass(8000.0, 0.707, sample_rate)
        self.filter2_l = BiquadLowpass(8000.0, 0.707, sample_rate)  # Second stage for 24dB
        self.filter2_r = BiquadLowpass(8000.0, 0.707, sample_rate)
        self.filter_cutoff = 8000.0
        self._filter_cutoff_cur = 8000.0
        self.filter_resonance = 0.707
        self.filter_slope = 12  # 12 or 24 dB/oct
        self.filter_range_min = 150.0
        self.filter_range_max = 20000.0
        self.osc1_filter_enabled = True
        self.osc2_filter_enabled = True

        # Independent per-osc stereo filters (used when shared filter is unchecked)
        self.osc1_indep_filter_l = BiquadLowpass(20000.0, 0.707, sample_rate)
        self.osc1_indep_filter_r = BiquadLowpass(20000.0, 0.707, sample_rate)
        self.osc1_indep_cutoff = 20000.0
        self._osc1_indep_cutoff_cur = 20000.0
        self.osc2_indep_filter_l = BiquadLowpass(20000.0, 0.707, sample_rate)
        self.osc2_indep_filter_r = BiquadLowpass(20000.0, 0.707, sample_rate)
        self.osc2_indep_cutoff = 20000.0
        self._osc2_indep_cutoff_cur = 20000.0

        # Main reverb — long ambient wash
        self.reverb = FeedbackDelayReverb(6.0, sample_rate)

        # Synthesized shimmer: octave-up sines fed into reverb input
        self.shimmer_enabled = False
        self.shimmer_mix = 0.5
        self._shimmer_hp = BiquadHighpass(2000.0, 0.707, sample_rate)  # low cut — keep only sparkle/air

        # Reverb freeze state
        self.freeze_enabled = False
        self._freeze_prev_feedback = 0.0
        self._freeze_prev_damp = 0.0

        self.volume = 0.8

        self.voices: list[Voice] = []
        self._age_counter = 0

        self._sample_indices = np.arange(1, 513, dtype=np.float64)

    def note_on(self, note: int, velocity: float = 1.0):
        for v in self.voices:
            if v.note == note and v.adsr.stage != ADSREnvelope.RELEASE:
                v.adsr.trigger()
                v.velocity = velocity
                v.age = self._age_counter
                self._age_counter += 1
                return

        if len(self.voices) >= self.max_voices:
            oldest = min(self.voices, key=lambda v: v.age)
            self.voices.remove(oldest)

        voice = Voice(
            note=note,
            velocity=velocity,
            adsr=ADSREnvelope(self.adsr_config),
            phases=[0.0] * self.unison_voices,
            osc1_phases=[0.0] * self.unison_voices,
            osc2_phases=[0.0] * self.unison_voices,
            shimmer_phases=[0.0] * self.unison_voices,
            age=self._age_counter,
        )
        voice.adsr.trigger()
        self._age_counter += 1
        self.voices.append(voice)

    def note_off(self, note: int):
        for v in self.voices:
            if v.note == note and v.adsr.stage != ADSREnvelope.RELEASE:
                v.adsr.release()

    def all_notes_off(self):
        for v in self.voices:
            v.adsr.release()

    def render(self, n_samples: int) -> np.ndarray:
        if n_samples == 0:
            return np.zeros((2, 0), dtype=np.float64)

        if len(self._sample_indices) < n_samples:
            self._sample_indices = np.arange(1, n_samples + 1, dtype=np.float64)
        indices = self._sample_indices[:n_samples]

        # Smooth oscillator blend changes (~5ms), then apply blend dB curve
        smooth = 1.0 - np.exp(-n_samples / (0.005 * self.sample_rate))
        self._osc1_blend_cur += smooth * (self.osc1_blend - self._osc1_blend_cur)
        self._osc2_blend_cur += smooth * (self.osc2_blend - self._osc2_blend_cur)
        osc1_b = blend_to_amplitude(self._osc1_blend_cur)
        osc2_b = blend_to_amplitude(self._osc2_blend_cur)

        both_filtered = self.osc1_filter_enabled and self.osc2_filter_enabled

        # Stereo buffers: [0]=L, [1]=R
        filter_buf = np.zeros((2, n_samples), dtype=np.float64)
        osc1_indep_buf = np.zeros((2, n_samples), dtype=np.float64) if not self.osc1_filter_enabled else None
        osc2_indep_buf = np.zeros((2, n_samples), dtype=np.float64) if not self.osc2_filter_enabled else None
        shimmer_sines = np.zeros(n_samples, dtype=np.float64)
        dead_voices = []

        osc1_wf = self.osc1_waveform
        osc2_wf = self.osc2_waveform
        spread = self.unison_spread
        n_uni = self.unison_voices

        # Per-oscillator base pan (hard_pan overrides individual settings)
        if self.osc_hard_pan:
            o1_pan = -1.0
            o2_pan = 1.0
        else:
            o1_pan = self.osc1_pan
            o2_pan = self.osc2_pan
        haas_active = abs(o1_pan - o2_pan) > 0.5

        # Accumulate osc2 separately for Haas delay
        osc2_accum_l = np.zeros(n_samples, dtype=np.float64)
        osc2_accum_r = np.zeros(n_samples, dtype=np.float64)

        for voice in self.voices:
            if not voice.is_active():
                dead_voices.append(voice)
                continue

            env = voice.adsr.process(n_samples)
            base_freq = 440.0 * (2.0 ** ((voice.note - 69) / 12.0))
            osc1_oct_mult = 2.0 ** self.osc1_octave
            osc2_oct_mult = 2.0 ** self.osc2_octave
            osc1_l = np.zeros(n_samples, dtype=np.float64)
            osc1_r = np.zeros(n_samples, dtype=np.float64)
            osc2_l = np.zeros(n_samples, dtype=np.float64)
            osc2_r = np.zeros(n_samples, dtype=np.float64)
            voice_shimmer = np.zeros(n_samples, dtype=np.float64)

            for u in range(n_uni):
                if n_uni > 1:
                    detune_st = self.unison_detune * (
                        2.0 * u / (n_uni - 1) - 1.0
                    )
                    uni_pan = (2.0 * u / (n_uni - 1) - 1.0) * spread
                else:
                    detune_st = 0.0
                    uni_pan = 0.0

                # OSC1 pan: base pan + unison spread, clamped
                pan1 = max(-1.0, min(1.0, o1_pan + uni_pan))
                pan1_shaped = np.sign(pan1) * abs(pan1) ** 0.7
                angle1 = (pan1_shaped + 1.0) * 0.25 * np.pi
                o1_gl = np.cos(angle1) * 1.4142135623730951
                o1_gr = np.sin(angle1) * 1.4142135623730951

                # OSC2 pan: base pan + unison spread, clamped
                pan2 = max(-1.0, min(1.0, o2_pan + uni_pan))
                pan2_shaped = np.sign(pan2) * abs(pan2) ** 0.7
                angle2 = (pan2_shaped + 1.0) * 0.25 * np.pi
                o2_gl = np.cos(angle2) * 1.4142135623730951
                o2_gr = np.sin(angle2) * 1.4142135623730951

                freq = base_freq * (2.0 ** (detune_st / 12.0))
                base_inc = TWO_PI * freq / self.sample_rate

                osc1_inc = base_inc * osc1_oct_mult
                osc2_inc = base_inc * osc2_oct_mult

                osc1_ph = voice.osc1_phases[u] + osc1_inc * indices
                osc2_ph = voice.osc2_phases[u] + osc2_inc * indices

                osc1_wave = generate_waveform(osc1_wf, osc1_ph) * osc1_b
                osc2_wave = generate_waveform(osc2_wf, osc2_ph) * osc2_b

                osc1_l += osc1_wave * o1_gl
                osc1_r += osc1_wave * o1_gr
                osc2_l += osc2_wave * o2_gl
                osc2_r += osc2_wave * o2_gr

                if self.shimmer_enabled:
                    shim_inc = base_inc * 2.0
                    shim_ph = voice.shimmer_phases[u] + shim_inc * indices
                    voice_shimmer += np.sin(shim_ph)
                    voice.shimmer_phases[u] = shim_ph[-1] % TWO_PI

                voice.osc1_phases[u] = osc1_ph[-1] % TWO_PI
                voice.osc2_phases[u] = osc2_ph[-1] % TWO_PI

            scale = (1.0 / max(n_uni, 1)) * (1.0 + 0.15 * (n_uni - 1)) * env * voice.velocity
            osc1_l *= scale
            osc1_r *= scale
            osc2_l *= scale
            osc2_r *= scale

            if self.shimmer_enabled:
                shimmer_sines += voice_shimmer * scale * 0.15

            # OSC1 goes directly to filter buffers
            if self.osc1_filter_enabled:
                filter_buf[0] += osc1_l
                filter_buf[1] += osc1_r
            else:
                osc1_indep_buf[0] += osc1_l
                osc1_indep_buf[1] += osc1_r

            # OSC2 accumulates separately for Haas delay
            osc2_accum_l += osc2_l
            osc2_accum_r += osc2_r

        # Apply Haas delay to OSC2 if pans are separated
        if haas_active:
            delay = self._haas_delay_samples
            bs = self._haas_buf_size
            pos = self._haas_pos
            # Write current block into delay buffer
            end = pos + n_samples
            if end <= bs:
                self._haas_buf_l[pos:end] = osc2_accum_l
                self._haas_buf_r[pos:end] = osc2_accum_r
            else:
                first = bs - pos
                self._haas_buf_l[pos:bs] = osc2_accum_l[:first]
                self._haas_buf_l[:end - bs] = osc2_accum_l[first:]
                self._haas_buf_r[pos:bs] = osc2_accum_r[:first]
                self._haas_buf_r[:end - bs] = osc2_accum_r[first:]
            # Read delayed
            rd_start = (pos - delay) % bs
            if rd_start + n_samples <= bs:
                osc2_accum_l = self._haas_buf_l[rd_start:rd_start + n_samples].copy()
                osc2_accum_r = self._haas_buf_r[rd_start:rd_start + n_samples].copy()
            else:
                first = bs - rd_start
                osc2_accum_l = np.concatenate([
                    self._haas_buf_l[rd_start:bs],
                    self._haas_buf_l[:n_samples - first]
                ])
                osc2_accum_r = np.concatenate([
                    self._haas_buf_r[rd_start:bs],
                    self._haas_buf_r[:n_samples - first]
                ])
            self._haas_pos = end % bs

        # Route OSC2 (possibly delayed) to filter buffers
        if self.osc2_filter_enabled:
            filter_buf[0] += osc2_accum_l
            filter_buf[1] += osc2_accum_r
        else:
            osc2_indep_buf[0] += osc2_accum_l
            osc2_indep_buf[1] += osc2_accum_r

        for v in dead_voices:
            self.voices.remove(v)

        # Smooth filter cutoff in log space (~80ms time constant)
        alpha_s = 1.0 - np.exp(-n_samples / (0.08 * self.sample_rate))

        log_cur = np.log(max(self._filter_cutoff_cur, 20.0))
        log_tgt = np.log(max(self.filter_cutoff, 20.0))
        log_cur += alpha_s * (log_tgt - log_cur)
        self._filter_cutoff_cur = np.exp(log_cur)
        self.filter_l.set_params(self._filter_cutoff_cur, self.filter_resonance)
        self.filter_r.set_params(self._filter_cutoff_cur, self.filter_resonance)
        if self.filter_slope == 24:
            self.filter2_l.set_params(self._filter_cutoff_cur, self.filter_resonance)
            self.filter2_r.set_params(self._filter_cutoff_cur, self.filter_resonance)

        if not self.osc1_filter_enabled:
            lc = np.log(max(self._osc1_indep_cutoff_cur, 20.0))
            lt = np.log(max(self.osc1_indep_cutoff, 20.0))
            lc += alpha_s * (lt - lc)
            self._osc1_indep_cutoff_cur = np.exp(lc)
            self.osc1_indep_filter_l.set_params(self._osc1_indep_cutoff_cur, 0.707)
            self.osc1_indep_filter_r.set_params(self._osc1_indep_cutoff_cur, 0.707)
        if not self.osc2_filter_enabled:
            lc = np.log(max(self._osc2_indep_cutoff_cur, 20.0))
            lt = np.log(max(self.osc2_indep_cutoff, 20.0))
            lc += alpha_s * (lt - lc)
            self._osc2_indep_cutoff_cur = np.exp(lc)
            self.osc2_indep_filter_l.set_params(self._osc2_indep_cutoff_cur, 0.707)
            self.osc2_indep_filter_r.set_params(self._osc2_indep_cutoff_cur, 0.707)

        # Apply stereo filters and combine
        # Filter gain compensation: -6dB at full open, 0dB at minimum cutoff
        # Prevents volume jump when sweeping filter up
        f_min = max(self.filter_range_min, 20.0)
        f_max = max(self.filter_range_max, f_min + 1.0)
        f_pos = np.log(max(self._filter_cutoff_cur, f_min) / f_min) / np.log(f_max / f_min)
        f_pos = max(0.0, min(1.0, f_pos))
        filter_comp = 10.0 ** (-12.0 * f_pos / 20.0)  # 0dB at min, -12dB at max

        output_l = np.zeros(n_samples, dtype=np.float64)
        output_r = np.zeros(n_samples, dtype=np.float64)
        if self.osc1_filter_enabled or self.osc2_filter_enabled:
            filtered_l = self.filter_l.process(filter_buf[0]) * filter_comp
            filtered_r = self.filter_r.process(filter_buf[1]) * filter_comp
            if self.filter_slope == 24:
                filtered_l = self.filter2_l.process(filtered_l)
                filtered_r = self.filter2_r.process(filtered_r)
            output_l += filtered_l
            output_r += filtered_r
        if not self.osc1_filter_enabled:
            output_l += self.osc1_indep_filter_l.process(osc1_indep_buf[0])
            output_r += self.osc1_indep_filter_r.process(osc1_indep_buf[1])
        if not self.osc2_filter_enabled:
            output_l += self.osc2_indep_filter_l.process(osc2_indep_buf[0])
            output_r += self.osc2_indep_filter_r.process(osc2_indep_buf[1])

        # Reverb gets stereo input — preserves stereo image in tail
        reverb_in_l = output_l.copy()
        reverb_in_r = output_r.copy()

        if self.shimmer_enabled:
            shimmer_filtered = self._shimmer_hp.process(shimmer_sines)
            shimmer_sig = shimmer_filtered * self.shimmer_mix
            reverb_in_l += shimmer_sig
            reverb_in_r += shimmer_sig

        reverb_in_l = np.tanh(reverb_in_l)
        reverb_in_r = np.tanh(reverb_in_r)

        # Main reverb — stereo in, stereo out
        reverb_out = self.reverb.process(np.array([reverb_in_l, reverb_in_r]))

        # Equal-power crossfade: stereo dry oscillators + stereo wet reverb
        dry_wet = self.reverb.dry_wet
        angle = dry_wet * (np.pi / 2.0)
        dry_gain = np.cos(angle)
        wet_gain_val = np.sin(angle) * self.reverb.wet_gain

        stereo_out = np.zeros((2, n_samples), dtype=np.float64)
        stereo_out[0] = output_l * dry_gain + reverb_out[0] * wet_gain_val
        stereo_out[1] = output_r * dry_gain + reverb_out[1] * wet_gain_val

        return stereo_out  # (2, n)

    def update_params(self, params: dict):
        if "osc1_blend" in params:
            self.osc1_blend = float(params["osc1_blend"])
        if "osc2_blend" in params:
            self.osc2_blend = float(params["osc2_blend"])
        if "osc1_waveform" in params:
            wf = params["osc1_waveform"]
            if wf in WAVEFORMS:
                self.osc1_waveform = wf
        if "osc2_waveform" in params:
            wf = params["osc2_waveform"]
            if wf in WAVEFORMS:
                self.osc2_waveform = wf
        if "osc1_octave" in params:
            self.osc1_octave = max(-3, min(3, int(params["osc1_octave"])))
        if "osc2_octave" in params:
            self.osc2_octave = max(-3, min(3, int(params["osc2_octave"])))
        if "unison_voices" in params:
            new_count = max(1, min(5, int(params["unison_voices"])))
            if new_count != self.unison_voices:
                self.unison_voices = new_count
                # Resize phase arrays on all active voices
                for v in self.voices:
                    while len(v.osc1_phases) < new_count:
                        v.osc1_phases.append(v.osc1_phases[0] if v.osc1_phases else 0.0)
                        v.osc2_phases.append(v.osc2_phases[0] if v.osc2_phases else 0.0)
                        v.shimmer_phases.append(0.0)
                    v.osc1_phases = v.osc1_phases[:new_count]
                    v.osc2_phases = v.osc2_phases[:new_count]
                    v.shimmer_phases = v.shimmer_phases[:new_count]
        if "unison_detune" in params:
            self.unison_detune = float(params["unison_detune"])
        if "unison_spread" in params:
            self.unison_spread = max(0.0, min(1.0, float(params["unison_spread"])))
        if "osc1_pan" in params:
            self.osc1_pan = max(-1.0, min(1.0, float(params["osc1_pan"])))
        if "osc2_pan" in params:
            self.osc2_pan = max(-1.0, min(1.0, float(params["osc2_pan"])))
        if "osc_hard_pan" in params:
            self.osc_hard_pan = bool(params["osc_hard_pan"])
        if "volume" in params:
            self.volume = float(params["volume"])

        adsr = params.get("adsr", {})
        if "attack_ms" in adsr:
            self.adsr_config.attack_ms = float(adsr["attack_ms"])
        if "decay_ms" in adsr:
            self.adsr_config.decay_ms = float(adsr["decay_ms"])
        if "sustain_percent" in adsr:
            self.adsr_config.sustain_percent = float(adsr["sustain_percent"])
        if "release_ms" in adsr:
            self.adsr_config.release_ms = float(adsr["release_ms"])

        if "filter_cutoff_hz" in params:
            self.filter_cutoff = float(params["filter_cutoff_hz"])
        if "filter_highpass_hz" in params:
            self.filter_cutoff = float(params["filter_highpass_hz"])
        if "filter_resonance" in params:
            self.filter_resonance = float(params["filter_resonance"])
        if "filter_range_min" in params:
            self.filter_range_min = float(params["filter_range_min"])
        if "filter_range_max" in params:
            self.filter_range_max = float(params["filter_range_max"])
        if "filter_slope" in params:
            slope = int(params["filter_slope"])
            if slope in (12, 24):
                self.filter_slope = slope
                if slope == 12:
                    self.filter2_l.reset()
                    self.filter2_r.reset()
        if "osc1_filter_enabled" in params:
            self.osc1_filter_enabled = bool(params["osc1_filter_enabled"])
        if "osc2_filter_enabled" in params:
            self.osc2_filter_enabled = bool(params["osc2_filter_enabled"])
        if "osc1_indep_cutoff" in params:
            self.osc1_indep_cutoff = max(20.0, min(20000.0, float(params["osc1_indep_cutoff"])))
        if "osc2_indep_cutoff" in params:
            self.osc2_indep_cutoff = max(20.0, min(20000.0, float(params["osc2_indep_cutoff"])))

        if "reverb_dry_wet" in params:
            self.reverb.dry_wet = float(params["reverb_dry_wet"])
        if "reverb_wet_gain" in params:
            self.reverb.wet_gain = max(0.5, min(3.0, float(params["reverb_wet_gain"])))
        if "reverb_decay_seconds" in params:
            self.reverb.set_decay(float(params["reverb_decay_seconds"]))
        if "reverb_low_cut" in params:
            self.reverb.set_low_cut(float(params["reverb_low_cut"]))
        if "reverb_high_cut" in params:
            self.reverb.set_high_cut(float(params["reverb_high_cut"]))
        if "reverb_space" in params:
            self.reverb.set_space(float(params["reverb_space"]))
        if "reverb_predelay_ms" in params:
            self.reverb.set_predelay(float(params["reverb_predelay_ms"]))
        if "shimmer_enabled" in params:
            self.shimmer_enabled = bool(params["shimmer_enabled"])
        if "shimmer_mix" in params:
            self.shimmer_mix = max(0.0, min(1.0, float(params["shimmer_mix"])))
        if "freeze_enabled" in params:
            self.freeze_enabled = bool(params["freeze_enabled"])
            self.reverb.set_freeze(self.freeze_enabled)

    def get_params(self) -> dict:
        return {
            "osc1_blend": self.osc1_blend,
            "osc2_blend": self.osc2_blend,
            "osc1_waveform": self.osc1_waveform,
            "osc2_waveform": self.osc2_waveform,
            "unison_voices": self.unison_voices,
            "unison_detune": self.unison_detune,
            "unison_spread": self.unison_spread,
            "osc1_pan": self.osc1_pan,
            "osc2_pan": self.osc2_pan,
            "osc_hard_pan": self.osc_hard_pan,
            "adsr": {
                "attack_ms": self.adsr_config.attack_ms,
                "decay_ms": self.adsr_config.decay_ms,
                "sustain_percent": self.adsr_config.sustain_percent,
                "release_ms": self.adsr_config.release_ms,
            },
            "filter_cutoff_hz": self.filter_cutoff,
            "filter_resonance": self.filter_resonance,
            "filter_slope": self.filter_slope,
            "osc1_filter_enabled": self.osc1_filter_enabled,
            "osc2_filter_enabled": self.osc2_filter_enabled,
            "reverb_dry_wet": self.reverb.dry_wet,
            "reverb_wet_gain": self.reverb.wet_gain,
            "reverb_decay_seconds": self.reverb.decay_seconds,
            "reverb_low_cut": self.reverb.low_cut_hz,
            "reverb_high_cut": self.reverb.high_cut_hz,
            "reverb_space": self.reverb.space,
            "reverb_predelay_ms": self.reverb.predelay_ms,
            "shimmer_enabled": self.shimmer_enabled,
            "shimmer_mix": self.shimmer_mix,
            "freeze_enabled": self.freeze_enabled,
            "volume": self.volume,
        }
