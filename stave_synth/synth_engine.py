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

    def __init__(self, config: ADSRConfig, sample_rate: int = SAMPLE_RATE):
        self.config = config
        self.sample_rate = sample_rate
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
            rate = 1.0 / max(self.config.attack_ms * self.sample_rate / 1000.0, 1.0)
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
            rate = 1.0 / max(self.config.release_ms * self.sample_rate / 1000.0, 1.0)
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
        rate = 1.0 / max(self.config.decay_ms * self.sample_rate / 1000.0, 1.0)
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


class BiquadPeakingEQ:
    """Parametric peaking EQ (bell curve) — boost or cut at a center frequency."""

    def __init__(self, freq_hz: float = 1000.0, gain_db: float = 0.0,
                 q: float = 1.0, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.zi = np.zeros(2, dtype=np.float64)
        self.b = np.zeros(3, dtype=np.float64)
        self.a = np.zeros(3, dtype=np.float64)
        self.set_params(freq_hz, gain_db, q)

    def set_params(self, freq_hz: float, gain_db: float, q: float):
        freq_hz = max(20.0, min(freq_hz, self.sample_rate * 0.45))
        q = max(0.1, min(q, 20.0))
        A = 10.0 ** (gain_db / 40.0)
        w0 = TWO_PI * freq_hz / self.sample_rate
        cos_w0 = np.cos(w0)
        sin_w0 = np.sin(w0)
        alpha = sin_w0 / (2.0 * q)

        a0 = 1.0 + alpha / A
        self.b[0] = (1.0 + alpha * A) / a0
        self.b[1] = (-2.0 * cos_w0) / a0
        self.b[2] = (1.0 - alpha * A) / a0
        self.a[0] = 1.0
        self.a[1] = (-2.0 * cos_w0) / a0
        self.a[2] = (1.0 - alpha / A) / a0

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


class BiquadLowShelf:
    """Low-shelf biquad (RBJ cookbook). Boosts or cuts below a shelf frequency.
    Used for the SSL-style stereo shuffler: low-shelf on the side signal."""

    def __init__(self, freq_hz: float = 800.0, gain_db: float = 0.0,
                 q: float = 0.707, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.zi = np.zeros(2, dtype=np.float64)
        self.b = np.zeros(3, dtype=np.float64)
        self.a = np.zeros(3, dtype=np.float64)
        self.set_params(freq_hz, gain_db, q)

    def set_params(self, freq_hz: float, gain_db: float, q: float = 0.707):
        freq_hz = max(20.0, min(freq_hz, self.sample_rate * 0.45))
        q = max(0.1, min(q, 20.0))
        A = 10.0 ** (gain_db / 40.0)
        w0 = TWO_PI * freq_hz / self.sample_rate
        cos_w0 = np.cos(w0)
        sin_w0 = np.sin(w0)
        alpha = sin_w0 / (2.0 * q)
        two_sqrtA_alpha = 2.0 * np.sqrt(A) * alpha

        a0 = (A + 1.0) + (A - 1.0) * cos_w0 + two_sqrtA_alpha
        self.b[0] = (A * ((A + 1.0) - (A - 1.0) * cos_w0 + two_sqrtA_alpha)) / a0
        self.b[1] = (2.0 * A * ((A - 1.0) - (A + 1.0) * cos_w0)) / a0
        self.b[2] = (A * ((A + 1.0) - (A - 1.0) * cos_w0 - two_sqrtA_alpha)) / a0
        self.a[0] = 1.0
        self.a[1] = (-2.0 * ((A - 1.0) + (A + 1.0) * cos_w0)) / a0
        self.a[2] = ((A + 1.0) + (A - 1.0) * cos_w0 - two_sqrtA_alpha) / a0

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
        self._damp_zi = np.zeros((self.n_lines, 1), dtype=np.float64)

        # ── Delay modulation — deeper, slower LFOs ──
        self._mod_phases = np.zeros(self.n_lines, dtype=np.float64)
        self._mod_rates = np.array([0.23, 0.37, 0.47, 0.61, 0.73, 0.89, 0.31, 0.53],
                                    dtype=np.float64) * TWO_PI / sample_rate
        self._mod_depths_base = np.array([28, 32, 24, 36, 26, 30, 34, 25], dtype=np.float64)
        self._mod_depths = self._mod_depths_base.copy()

        # Feedback frequency bounds
        self.fb_lowcut = BiquadHighpass(80.0, 0.707, sample_rate)
        self.fb_highcut = BiquadLowpass(7000.0, 0.707, sample_rate)
        self._fb_lc_zi = np.zeros((self.n_lines, 2), dtype=np.float64)
        self._fb_hc_zi = np.zeros((self.n_lines, 2), dtype=np.float64)

        # Freeze state — smoothly ramped to avoid artifacts
        self.frozen = False
        self._normal_feedback = 0.0
        self._normal_damp = self.damp_coeff
        self._feedback_target = self.feedback
        self._damp_target = self.damp_coeff
        self._freeze_input_gain = 1.0       # 1.0 = normal, 0.0 = frozen (muted input)
        self._freeze_input_target = 1.0
        self._freeze_capture_remaining = 0  # samples left in capture window
        # Freeze/feedback/damp/input-gain smoothing: ~30 ms time constant,
        # applied once per audio block. Recomputed each block in process() so
        # it stays correct if JACK/PipeWire buffer size changes from the
        # original assumption.

    def panic(self):
        """Silence the reverb instantly: force off freeze and zero every buffer."""
        self.frozen = False
        self._feedback_target = self._normal_feedback if self._normal_feedback else self.feedback
        self._damp_target = self._normal_damp
        self._freeze_input_gain = 1.0
        self._freeze_input_target = 1.0
        self._freeze_capture_remaining = 0
        self.predelay_buf[:] = 0.0
        self._er_buf[:] = 0.0
        for b in self.bufs:
            b[:] = 0.0
        for d in self.diffusers:
            d.x_buf[:] = 0.0
            d.y_buf[:] = 0.0
        self._damp_zi[:] = 0.0
        self._fb_lc_zi[:] = 0.0
        self._fb_hc_zi[:] = 0.0
        self.fb_lowcut.reset()
        self.fb_highcut.reset()

    def set_freeze(self, enabled: bool):
        """Freeze the reverb tail — captures input for ~2s then seals the loop."""
        if enabled and not self.frozen:
            self._normal_feedback = self.feedback
            self._normal_damp = self.damp_coeff
            self._feedback_target = 0.999
            self._damp_target = 0.05  # near-zero damping so it doesn't die
            # Keep input open for 2 seconds so you can play into the freeze
            self._freeze_capture_remaining = int(2.0 * self.sample_rate)
            self._freeze_input_target = 1.0  # input stays open during capture
            self.frozen = True
        elif not enabled and self.frozen:
            self._feedback_target = self._normal_feedback
            self._damp_target = self._normal_damp
            self._freeze_input_target = 1.0
            self._freeze_capture_remaining = 0
            self.frozen = False

    def set_space(self, value: float):
        """Stores the Space value (0-1). The actual SSL-style shuffler runs in
        JackEngine on the full master bus — this attribute is read from there."""
        self.space = max(0.0, min(1.0, value))

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
        if hasattr(self, 'frozen') and not self.frozen:
            self._feedback_target = self.feedback

    def set_low_cut(self, freq_hz: float):
        new_hz = max(20.0, freq_hz)
        if new_hz == self.low_cut_hz:
            return
        self.low_cut_hz = new_hz
        self.fb_lowcut.set_params(self.low_cut_hz, 0.707)

    def set_high_cut(self, freq_hz: float):
        new_hz = min(freq_hz, 20000.0)
        if new_hz == self.high_cut_hz:
            return
        self.high_cut_hz = new_hz
        self.fb_highcut.set_params(self.high_cut_hz, 0.707)

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

        # Vectorized early reflections — single batched read instead of 8 Python loops
        er_left = np.zeros(n, dtype=np.float64)
        er_right = np.zeros(n, dtype=np.float64)
        for i in range(len(self._er_l_delays)):
            start = (er_pos - self._er_l_delays[i]) % er_bs
            if start + n <= er_bs:
                er_left += self._er_buf[start:start + n] * self._er_l_gains[i]
            else:
                first = er_bs - start
                er_left[:first] += self._er_buf[start:er_bs] * self._er_l_gains[i]
                er_left[first:] += self._er_buf[:n - first] * self._er_l_gains[i]
        for i in range(len(self._er_r_delays)):
            start = (er_pos - self._er_r_delays[i]) % er_bs
            if start + n <= er_bs:
                er_right += self._er_buf[start:start + n] * self._er_r_gains[i]
            else:
                first = er_bs - start
                er_right[:first] += self._er_buf[start:er_bs] * self._er_r_gains[i]
                er_right[first:] += self._er_buf[:n - first] * self._er_r_gains[i]
        self._er_pos = er_end % er_bs

        # ── Diffusion: smear input through all-pass chain ──
        diffused = predelayed.copy()
        for ap in self.diffusers:
            diffused = ap.process(diffused)

        # Freeze capture window: input stays open for ~2s, then ramps to zero
        if self.frozen and self._freeze_capture_remaining > 0:
            self._freeze_capture_remaining -= n
            if self._freeze_capture_remaining <= 0:
                self._freeze_input_target = 0.0  # seal the loop

        # Smoothly ramp feedback/damp/input gain toward targets (freeze crossfade).
        # Alpha scales with actual block size so it stays ~30 ms regardless of quantum.
        a_f = 1.0 - np.exp(-n / (0.030 * self.sample_rate))
        self.feedback += a_f * (self._feedback_target - self.feedback)
        self.damp_coeff += a_f * (self._damp_target - self.damp_coeff)
        self._freeze_input_gain += a_f * (self._freeze_input_target - self._freeze_input_gain)
        # Safety: scrub NaN/Inf and clamp to stable ranges. Extreme parameter
        # changes (negative values, huge cutoffs) during live use must never
        # corrupt the recursive reverb state.
        if not np.isfinite(self.feedback):
            self.feedback = self._normal_feedback
        if not np.isfinite(self.damp_coeff):
            self.damp_coeff = self._normal_damp
        if not np.isfinite(self._freeze_input_gain):
            self._freeze_input_gain = 1.0
        self.feedback = min(max(self.feedback, 0.0), 0.9995)
        self.damp_coeff = min(max(self.damp_coeff, 0.0), 0.99)
        self._freeze_input_gain = min(max(self._freeze_input_gain, 0.0), 1.0)

        fb = self.feedback
        damp = self.damp_coeff
        b_lp = np.array([1.0 - damp])
        a_lp = np.array([1.0, -damp])

        # ── Compute modulation offsets (vectorized: 1 sin call instead of 8) ──
        sample_offsets = np.arange(n, dtype=np.float64)
        phases_2d = self._mod_phases[:, None] + self._mod_rates[:, None] * sample_offsets[None, :]
        mod_offsets_2d = np.sin(phases_2d) * self._mod_depths[:, None]
        self._mod_phases = (self._mod_phases + self._mod_rates * n) % TWO_PI

        # ── Read modulated FDN taps ──
        taps = [self._read_block_modulated(self.bufs[i], self.delays[i], mod_offsets_2d[i], n)
                for i in range(self.n_lines)]

        # ── Hadamard mixing + stereo wet output ──
        taps_matrix = np.array(taps)  # (8, n)
        mixed = _HADAMARD @ taps_matrix  # (8, n)

        # Even indices (0,2,4,6) → L, odd indices (1,3,5,7) → R
        half = self.n_lines // 2
        inv_sqrt_half = 1.0 / np.sqrt(half)
        wet_l = taps_matrix[0::2].sum(axis=0) * inv_sqrt_half
        wet_r = taps_matrix[1::2].sum(axis=0) * inv_sqrt_half

        # ── Batched feedback filtering (3 lfilter calls instead of 24) ──
        fb_signals = mixed * fb  # (8, n)
        if HAS_SCIPY:
            fb_signals, self._damp_zi = lfilter(b_lp, a_lp, fb_signals, zi=self._damp_zi, axis=-1)
            lc_b, lc_a = self.fb_lowcut.b, self.fb_lowcut.a
            hc_b, hc_a = self.fb_highcut.b, self.fb_highcut.a
            fb_signals, self._fb_lc_zi = lfilter(lc_b, lc_a, fb_signals, zi=self._fb_lc_zi, axis=-1)
            fb_signals, self._fb_hc_zi = lfilter(hc_b, hc_a, fb_signals, zi=self._fb_hc_zi, axis=-1)

        # Stereo FDN input: even lines get L, odd lines get R
        # Freeze smoothly ramps input gain to zero (and back) to avoid artifacts
        fg = self._freeze_input_gain
        input_l_diff = (diffused * 0.5 + input_l * 0.5) * fg
        input_r_diff = (diffused * 0.5 + input_r * 0.5) * fg

        for i in range(self.n_lines):
            inp = input_l_diff if (i % 2 == 0) else input_r_diff
            self._write_block(self.bufs[i], inp + fb_signals[i], n)

        self.write_pos = (self.write_pos + n) % self.buf_size

        # ── Stereo width enhancement (space-dependent) ──
        # Cross-feed: blend opposite channels for wider image
        # ── Soft-limit wet taps so FDN energy can't clip ──
        wet_l = np.tanh(wet_l)
        wet_r = np.tanh(wet_r)

        # Return wet-only (early reflections + FDN tail)
        er_scale = 0.4 if not self.frozen else 0.0
        out_l = er_left * er_scale + wet_l
        out_r = er_right * er_scale + wet_r

        return np.array([out_l, out_r])


class BusCompressor:
    """SSL G-style stereo bus compressor.

    Block-level detection with per-sample linear ramp for ballistic smoothness —
    good enough for a bus comp where attack/release times are always slow relative
    to block size (~10ms). Fast percussive comps would need per-sample loops; bus
    comp doesn't.

    Detection signal can come from the bus itself (feedback-style SELF), the
    piano mix, the LFO, or a BPM pulse train. HPF on the sidechain at 100 Hz
    (SSL-style) so bass doesn't trigger ducking.

    Parallel MIX knob blends dry + compressed for New York-style parallel comp.
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.enabled = False
        self.threshold_db = -10.0
        self.ratio = 4.0
        self.attack_ms = 3.0
        self.release_ms = 300.0
        self.release_auto = True
        self.makeup_db = 0.0
        self.mix = 1.0              # 0=dry, 1=full comp
        self.knee_db = 2.0
        self.sidechain_hpf_hz = 100.0

        self._hpf_l = BiquadHighpass(self.sidechain_hpf_hz, 0.707, sample_rate)
        self._hpf_r = BiquadHighpass(self.sidechain_hpf_hz, 0.707, sample_rate)

        # RMS envelope integrator state
        self._rms_alpha = 1.0 - np.exp(-1.0 / (0.005 * sample_rate))  # 5ms window
        self._rms_state = np.zeros(1, dtype=np.float64)

        # GR envelope (in dB, positive = reduction)
        self._env_gr_db = 0.0
        self._gr_db = 0.0  # exposed for metering

    def reset(self):
        self._hpf_l.reset()
        self._hpf_r.reset()
        self._rms_state[:] = 0.0
        self._env_gr_db = 0.0
        self._gr_db = 0.0

    @property
    def current_gr_db(self) -> float:
        """Current gain reduction in dB (positive = reduced)."""
        return float(self._gr_db)

    def _target_gr_db(self, env_db: float) -> float:
        over = env_db - self.threshold_db
        knee = self.knee_db
        ratio = max(1.0, self.ratio)
        if over < -knee * 0.5:
            return 0.0
        if over > knee * 0.5:
            return over * (1.0 - 1.0 / ratio)
        # Soft knee quadratic
        x = (over + knee * 0.5) / knee  # 0..1
        return x * x * knee * (1.0 - 1.0 / ratio)

    def process(self, out_l: np.ndarray, out_r: np.ndarray,
                sc_l: np.ndarray = None, sc_r: np.ndarray = None):
        """Process stereo in-place. sc_l/sc_r: external sidechain; if None, uses
        bus signal (feedback-ish approximation using current input as detection
        source). No-ops if disabled or mix is effectively zero."""
        if not self.enabled:
            self._gr_db = 0.0
            return
        n = out_l.shape[0]

        # ── Sidechain source ──
        if sc_l is not None and sc_r is not None:
            sc = (sc_l + sc_r) * 0.5
        elif sc_l is not None:
            sc = sc_l.copy()
        elif sc_r is not None:
            sc = sc_r.copy()
        else:
            # SELF (feedback approximation — uses input pre-compression)
            sc = (out_l + out_r) * 0.5

        # ── Highpass the detection signal so bass doesn't trigger ──
        sc = self._hpf_l.process(sc)

        # ── RMS envelope (one-pole on squared signal) ──
        sq = sc * sc
        if HAS_SCIPY:
            a = self._rms_alpha
            rms, self._rms_state = lfilter([a], [1.0, -(1.0 - a)], sq, zi=self._rms_state)
        else:
            rms = sq  # fallback: no smoothing
        rms = np.maximum(rms, 1e-12)
        env_db = 10.0 * np.log10(rms)

        # Use block average as the detection level (mean RMS across block)
        # This is the SSL-style slow-ish detection for bus work.
        target_env_db = float(np.mean(env_db))
        target_gr = self._target_gr_db(target_env_db)

        # ── Ballistic smoothing with per-block time constant + linear ramp ──
        block_sec = n / self.sample_rate
        delta = target_gr - self._env_gr_db
        if delta > 0:  # attack
            tau = max(self.attack_ms * 0.001, 0.0001)
        else:
            if self.release_auto:
                # Dual-stage: faster for big peaks, slower for sustained compression.
                # Approximated by scaling with current GR — low GR releases quickly.
                tau = 0.1 + 0.5 * min(1.0, self._env_gr_db / 8.0)
            else:
                tau = max(self.release_ms * 0.001, 0.0001)
        coef = 1.0 - np.exp(-block_sec / tau)
        new_gr = self._env_gr_db + coef * delta
        gr_ramp = np.linspace(self._env_gr_db, new_gr, n, dtype=np.float64)
        self._env_gr_db = new_gr
        self._gr_db = new_gr

        # ── Apply gain reduction + makeup + parallel mix ──
        gain_db = -gr_ramp + self.makeup_db
        gain = np.power(10.0, gain_db / 20.0)

        if self.mix >= 0.999:
            out_l *= gain
            out_r *= gain
        elif self.mix < 0.001:
            # Dry pass — detector still runs (GR meter moves) but audio untouched
            return
        else:
            m = self.mix
            inv = 1.0 - m
            comp_l = out_l * gain
            comp_r = out_r * gain
            out_l[:] = out_l * inv + comp_l * m
            out_r[:] = out_r * inv + comp_r * m


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
        self.haas_delay_ms = 20.0  # user-selectable (15 / 20 / 40 ms)
        self._haas_delay_samples = int(self.haas_delay_ms * 0.001 * sample_rate)
        self._haas_buf_size = int(0.050 * sample_rate) + 512  # room for up to 50ms
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
        # Highpass (low cut) — filter fader ALT mode
        self.filter_hp_l = BiquadHighpass(20.0, 0.707, sample_rate)
        self.filter_hp_r = BiquadHighpass(20.0, 0.707, sample_rate)
        self.filter_highpass_hz = 20.0
        self._filter_highpass_cur = 20.0
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
        self._dry_wet_cur = 0.75  # smoothed dry_wet tracking
        self.reverb_filter_enabled = False  # route reverb wet through main filter
        self.reverb_filter_l = BiquadLowpass(8000.0, 0.707, sample_rate)
        self.reverb_filter_r = BiquadLowpass(8000.0, 0.707, sample_rate)
        self.reverb_filter2_l = BiquadLowpass(8000.0, 0.707, sample_rate)
        self.reverb_filter2_r = BiquadLowpass(8000.0, 0.707, sample_rate)

        # Synthesized shimmer: octave-up sines fed into reverb input
        self.shimmer_enabled = False
        self.shimmer_mix = 0.5
        self.shimmer_high = False  # False = +12 (2x), True = +24 (4x)
        self.shimmer_send = 1.0    # CLOUD knob: wet level of pre-reverb multi-tap bouncing delay
        self._shimmer_mix_cur = 0.5
        self._shimmer_hp = BiquadHighpass(1200.0, 0.707, sample_rate)  # low cut — keep sparkle + upper fundamentals

        # Shimmer pre-reverb "cloud": multi-tap stereo delay for sporadic bouncing.
        # Irregular tap times (L vs R offset) create stereo motion without hard echoes.
        # No feedback — single-generation bounces keep it musical and prevent buildup.
        _shim_delay_s = 0.6
        _shim_len = int(_shim_delay_s * sample_rate)
        self._shimmer_delay_l = np.zeros(_shim_len, dtype=np.float64)
        self._shimmer_delay_r = np.zeros(_shim_len, dtype=np.float64)
        self._shimmer_delay_len = _shim_len
        self._shimmer_delay_idx = 0
        # (offset_samples, gain) — L and R offsets interleave for stereo bounce
        self._shimmer_taps_l = [
            (int(0.130 * sample_rate), 0.65),
            (int(0.247 * sample_rate), 0.50),
            (int(0.363 * sample_rate), 0.36),
            (int(0.481 * sample_rate), 0.22),
        ]
        self._shimmer_taps_r = [
            (int(0.173 * sample_rate), 0.65),
            (int(0.289 * sample_rate), 0.50),
            (int(0.405 * sample_rate), 0.36),
            (int(0.523 * sample_rate), 0.22),
        ]

        # Reverb freeze state
        self.freeze_enabled = False
        self._freeze_prev_feedback = 0.0
        self._freeze_prev_damp = 0.0

        # Sympathetic resonance: piano notes reinforce the pad subtly
        self.sympathetic_enabled = False
        self.sympathetic_level = 0.035
        self._sympathetic_level_cur = 0.035
        self._sympathetic_suppress = False  # set during preset crossfade to silence resonance
        self._sympathetic_state = {}  # note -> {phase_l, phase_r, gain, target}

        # Chord drone: sustained root+fifth an octave below
        self.drone_enabled = False
        self._drone_root_freq = 0.0
        self._drone_fifth_freq = 0.0
        self._drone_root_freq_cur = 0.0
        self._drone_fifth_freq_cur = 0.0
        self._drone_root_phase = 0.0
        self._drone_fifth_phase = 0.0
        self._drone_gain = 0.0
        self._drone_gain_target = 0.0
        self._drone_latched = False  # locks drone pitch after first note

        self.volume = 0.8

        # ═══ Motion bus mix — scales all MOTION effects together ═══
        # Set by the FX fader's 3rd ALT state ("MOTION"). Multiplies into LFO
        # depth and ping-pong wet so one fader can bring all motion in/out live.
        self.motion_mix = 1.0

        # ═══ Ping-pong delay (Motion tab) ═══
        # Stereo delay with cross-feedback: L feeds R's buffer, R feeds L's.
        # Time either in ms (FREE) or derived from BPM × subdivision.
        self.delay_enabled = False
        self.delay_time_mode = "1/4"   # "FREE" or subdivision string
        self.delay_time_ms = 375.0     # used when mode = FREE
        self.delay_offset_ms = 0.0     # L/R offset in ms
        self.delay_feedback = 0.35     # 0..0.85
        self.delay_wet = 0.0           # 0..1 (dry always passes through)
        self.bpm = 120.0
        _delay_max_ms = 1000.0          # generous headroom; will clamp read offset
        _delay_buf_len = int((_delay_max_ms / 1000.0) * sample_rate)
        self._delay_buf_l = np.zeros(_delay_buf_len, dtype=np.float64)
        self._delay_buf_r = np.zeros(_delay_buf_len, dtype=np.float64)
        self._delay_buf_len = _delay_buf_len
        self._delay_write_pos = 0

        # ═══ LFO (Motion tab) ═══
        # Single control-rate LFO. Cheap because it updates once per block, not per sample.
        # Targets: filter / amp / pan. (Pitch target deferred — needs voice-level wiring.)
        self.lfo_enabled = False
        self.lfo_rate_hz = 1.0         # 0.05-20 Hz
        self.lfo_depth = 0.0           # 0..1
        self.lfo_shape = "sine"        # sine, triangle, square, sh
        self.lfo_target = "filter"     # filter, amp, pan
        self.lfo_spread = 0.0          # 0..1, 180° R/L offset at max
        self._lfo_phase = 0.0
        self._lfo_sh_value = 0.0       # current sample&hold held value
        self._lfo_sh_value_r = 0.0
        self._lfo_prev_phase = 0.0     # to detect wraps for S&H
        self._lfo_mod_a_last = 0.0     # previous block's end mod value (for per-sample ramp)
        self._lfo_mod_b_last = 0.0

        self.voices: list[Voice] = []
        self._age_counter = 0

        self._sample_indices = np.arange(1, 513, dtype=np.float64)

        # Pre-allocated render buffers — avoids per-block allocation/GC jitter
        self._buf_size = 512  # resized if needed
        self._filter_buf = np.zeros((2, self._buf_size), dtype=np.float64)
        self._osc1_indep_buf = np.zeros((2, self._buf_size), dtype=np.float64)
        self._osc2_indep_buf = np.zeros((2, self._buf_size), dtype=np.float64)
        self._shimmer_buf = np.zeros(self._buf_size, dtype=np.float64)
        self._output_l = np.zeros(self._buf_size, dtype=np.float64)
        self._output_r = np.zeros(self._buf_size, dtype=np.float64)
        self._stereo_out = np.zeros((2, self._buf_size), dtype=np.float64)
        self._reverb_in_l = np.zeros(self._buf_size, dtype=np.float64)
        self._reverb_in_r = np.zeros(self._buf_size, dtype=np.float64)
        self._osc2_accum_l = np.zeros(self._buf_size, dtype=np.float64)
        self._osc2_accum_r = np.zeros(self._buf_size, dtype=np.float64)
        self._shimmer_cloud_l = np.zeros(self._buf_size, dtype=np.float64)
        self._shimmer_cloud_r = np.zeros(self._buf_size, dtype=np.float64)

        # Per-voice pre-allocated buffers (avoid np.zeros per voice per block)
        self._voice_osc1_l = np.zeros((max_voices, self._buf_size), dtype=np.float64)
        self._voice_osc1_r = np.zeros((max_voices, self._buf_size), dtype=np.float64)
        self._voice_osc2_l = np.zeros((max_voices, self._buf_size), dtype=np.float64)
        self._voice_osc2_r = np.zeros((max_voices, self._buf_size), dtype=np.float64)
        self._voice_shimmer = np.zeros((max_voices, self._buf_size), dtype=np.float64)

        # Cached filter cutoff to avoid redundant set_params trig calls
        self._filter_cutoff_last_set = -1.0
        self._filter_res_last_set = -1.0

    def _ensure_buffers(self, n_samples: int):
        """Resize pre-allocated buffers if block size changed."""
        if n_samples > self._buf_size:
            self._buf_size = n_samples
            self._filter_buf = np.zeros((2, n_samples), dtype=np.float64)
            self._osc1_indep_buf = np.zeros((2, n_samples), dtype=np.float64)
            self._osc2_indep_buf = np.zeros((2, n_samples), dtype=np.float64)
            self._shimmer_buf = np.zeros(n_samples, dtype=np.float64)
            self._output_l = np.zeros(n_samples, dtype=np.float64)
            self._output_r = np.zeros(n_samples, dtype=np.float64)
            self._stereo_out = np.zeros((2, n_samples), dtype=np.float64)
            self._reverb_in_l = np.zeros(n_samples, dtype=np.float64)
            self._reverb_in_r = np.zeros(n_samples, dtype=np.float64)
            self._osc2_accum_l = np.zeros(n_samples, dtype=np.float64)
            self._osc2_accum_r = np.zeros(n_samples, dtype=np.float64)
            self._shimmer_cloud_l = np.zeros(n_samples, dtype=np.float64)
            self._shimmer_cloud_r = np.zeros(n_samples, dtype=np.float64)
            self._voice_osc1_l = np.zeros((self.max_voices, n_samples), dtype=np.float64)
            self._voice_osc1_r = np.zeros((self.max_voices, n_samples), dtype=np.float64)
            self._voice_osc2_l = np.zeros((self.max_voices, n_samples), dtype=np.float64)
            self._voice_osc2_r = np.zeros((self.max_voices, n_samples), dtype=np.float64)
            self._voice_shimmer = np.zeros((self.max_voices, n_samples), dtype=np.float64)

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

        # Randomize unison starting phases so detuned voices decorrelate from
        # the first sample — classic supersaw trick. Aligned phases (all 0)
        # cause audible beating/LFO-phasing when detune is small.
        n_u = self.unison_voices
        rand_phases_osc1 = list(np.random.uniform(0.0, TWO_PI, n_u))
        rand_phases_osc2 = list(np.random.uniform(0.0, TWO_PI, n_u))
        rand_phases_shim = list(np.random.uniform(0.0, TWO_PI, n_u))
        voice = Voice(
            note=note,
            velocity=velocity,
            adsr=ADSREnvelope(self.adsr_config, self.sample_rate),
            phases=[0.0] * n_u,
            osc1_phases=rand_phases_osc1,
            osc2_phases=rand_phases_osc2,
            shimmer_phases=rand_phases_shim,
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

    def panic(self):
        """Hard-silence everything: kill voices, drone, sympathetic, freeze, flush buffers."""
        for v in self.voices:
            v.adsr.stage = ADSREnvelope.OFF
            v.adsr.level = 0.0
        self._drone_root_freq = 0.0
        self._drone_fifth_freq = 0.0
        self._drone_root_freq_cur = 0.0
        self._drone_fifth_freq_cur = 0.0
        self._drone_gain = 0.0
        self._drone_gain_target = 0.0
        self._drone_latched = False
        self._sympathetic_state.clear()
        self.freeze_enabled = False
        self.reverb.panic()
        self._haas_buf_l[:] = 0.0
        self._haas_buf_r[:] = 0.0
        self.filter_l.reset()
        self.filter_r.reset()
        self.filter2_l.reset()
        self.filter2_r.reset()
        self.filter_hp_l.reset()
        self.filter_hp_r.reset()
        self.osc1_indep_filter_l.reset()
        self.osc1_indep_filter_r.reset()
        self.osc2_indep_filter_l.reset()
        self.osc2_indep_filter_r.reset()
        self.reverb_filter_l.reset()
        self.reverb_filter_r.reset()
        self.reverb_filter2_l.reset()
        self.reverb_filter2_r.reset()
        self._shimmer_hp.reset()
        self._shimmer_delay_l[:] = 0.0
        self._shimmer_delay_r[:] = 0.0
        self._delay_buf_l[:] = 0.0
        self._delay_buf_r[:] = 0.0

    # Subdivision → beat multiplier (fraction of a quarter note)
    _DELAY_DIVISIONS = {
        "1/2":   2.0,
        "1/4.":  1.5,
        "1/4":   1.0,
        "1/4T":  2.0 / 3.0,
        "1/8.":  0.75,
        "1/8":   0.5,
        "1/8T":  1.0 / 3.0,
        "1/16":  0.25,
    }

    def _delay_time_samples(self) -> int:
        """Current delay time in samples, from either free ms or BPM × subdivision."""
        if self.delay_time_mode == "FREE":
            ms = max(1.0, min(1000.0, self.delay_time_ms))
        else:
            mult = self._DELAY_DIVISIONS.get(self.delay_time_mode, 1.0)
            beat_sec = 60.0 / max(40.0, self.bpm)  # quarter note length in seconds
            ms = beat_sec * mult * 1000.0
            ms = max(1.0, min(1000.0, ms))
        return int(ms * 0.001 * self.sample_rate)

    def _process_ping_pong(self, out_l: np.ndarray, out_r: np.ndarray):
        """Stereo cross-feedback delay applied in place on out_l/out_r.
        Dry signal passes through unchanged; wet taps mix in at self.delay_wet × motion_mix.
        Read happens before write so we never self-read within a block."""
        effective_wet = self.delay_wet * self.motion_mix
        if not self.delay_enabled or effective_wet < 0.001:
            return
        n = out_l.shape[0]
        buf_l = self._delay_buf_l
        buf_r = self._delay_buf_r
        blen = self._delay_buf_len
        pos = self._delay_write_pos
        delay_samps = max(1, self._delay_time_samples())
        off_samps = int(self.delay_offset_ms * 0.001 * self.sample_rate)
        read_l_len = max(1, delay_samps)
        read_r_len = max(1, delay_samps + off_samps)
        read_l_len = min(read_l_len, blen - 1)
        read_r_len = min(read_r_len, blen - 1)

        # Read the two tap blocks (with wrap)
        def read_block(buf, delay_len):
            start = (pos - delay_len) % blen
            end = start + n
            if end <= blen:
                return buf[start:end].copy()
            first = blen - start
            out = np.empty(n, dtype=np.float64)
            out[:first] = buf[start:]
            out[first:] = buf[:end - blen]
            return out

        tap_l = read_block(buf_r, read_l_len)  # L tap reads R buffer (ping-pong)
        tap_r = read_block(buf_l, read_r_len)  # R tap reads L buffer

        # Write current input + feedback from opposite tap into each buffer
        fb = max(0.0, min(0.85, self.delay_feedback))
        write_l = out_l + tap_l * fb
        write_r = out_r + tap_r * fb

        end = pos + n
        if end <= blen:
            buf_l[pos:end] = write_l
            buf_r[pos:end] = write_r
        else:
            first = blen - pos
            buf_l[pos:] = write_l[:first]
            buf_l[:end - blen] = write_l[first:]
            buf_r[pos:] = write_r[:first]
            buf_r[:end - blen] = write_r[first:]
        self._delay_write_pos = end % blen

        # Mix wet taps into output (scaled by the motion bus)
        out_l += tap_l * effective_wet
        out_r += tap_r * effective_wet

    def _advance_lfo(self, n_samples: int):
        """Advance LFO phase by one block; return (mod_a, mod_b) bipolar, pre-scaled by
        effective depth (user depth × motion_mix bus). mod_b is phase-offset by spread.
        Control rate — cheap because it runs once per block, not per sample."""
        effective_depth = self.lfo_depth * self.motion_mix
        if not self.lfo_enabled or effective_depth < 0.001:
            return 0.0, 0.0
        block_sec = n_samples / self.sample_rate
        prev_phase = self._lfo_phase
        self._lfo_phase = (self._lfo_phase + self.lfo_rate_hz * block_sec) % 1.0
        # Sample & Hold regenerates on every phase wrap
        if self.lfo_shape == "sh" and self._lfo_phase < prev_phase:
            self._lfo_sh_value = float(np.random.uniform(-1.0, 1.0))
            self._lfo_sh_value_r = float(np.random.uniform(-1.0, 1.0))
        phase_a = self._lfo_phase
        phase_b = (self._lfo_phase + 0.5 * self.lfo_spread) % 1.0

        def _eval(phase, is_b):
            if self.lfo_shape == "triangle":
                return 4.0 * abs(phase - 0.5) - 1.0
            if self.lfo_shape == "square":
                return 1.0 if phase < 0.5 else -1.0
            if self.lfo_shape == "sh":
                return self._lfo_sh_value_r if is_b else self._lfo_sh_value
            # default: sine
            return float(np.sin(phase * TWO_PI))

        return _eval(phase_a, False) * effective_depth, _eval(phase_b, True) * effective_depth

    def _process_shimmer_cloud(self, shimmer_sig: np.ndarray, out_l: np.ndarray, out_r: np.ndarray):
        """Multi-tap pre-reverb cloud: writes shimmer_sig into a ring buffer and
        reads several stereo-offset taps, producing a sporadic bouncing stereo
        wet signal that fills the space before the main reverb. No feedback —
        single-generation taps keep it musical without buildup."""
        n = shimmer_sig.shape[0]
        buf_l = self._shimmer_delay_l
        buf_r = self._shimmer_delay_r
        buf_len = self._shimmer_delay_len
        idx = self._shimmer_delay_idx

        # Write the current block to both delay buffers (mono source, both channels).
        end = idx + n
        if end <= buf_len:
            buf_l[idx:end] = shimmer_sig
            buf_r[idx:end] = shimmer_sig
        else:
            first = buf_len - idx
            buf_l[idx:] = shimmer_sig[:first]
            buf_l[:end - buf_len] = shimmer_sig[first:]
            buf_r[idx:] = shimmer_sig[:first]
            buf_r[:end - buf_len] = shimmer_sig[first:]

        out_l[:] = 0.0
        out_r[:] = 0.0

        def read_taps(buf, taps, out):
            for offset, gain in taps:
                # Read n samples starting at (idx - offset) mod buf_len, going forward.
                start = (idx - offset) % buf_len
                rend = start + n
                if rend <= buf_len:
                    out += buf[start:rend] * gain
                else:
                    split = buf_len - start
                    out[:split] += buf[start:] * gain
                    out[split:] += buf[:rend - buf_len] * gain

        read_taps(buf_l, self._shimmer_taps_l, out_l)
        read_taps(buf_r, self._shimmer_taps_r, out_r)

        self._shimmer_delay_idx = (idx + n) % buf_len

    def sympathetic_fade_out(self):
        """Set all active sympathetic voices to fade toward zero. Lets held notes
        re-arm naturally from jack_engine's per-buffer sync after a preset switch."""
        for st in self._sympathetic_state.values():
            st["target"] = 0.0

    def sympathetic_set_suppress(self, suppress: bool):
        """Block sympathetic rendering and re-arm. Used during preset crossfade so
        held keys don't keep pumping tone into the reverb at changing levels."""
        self._sympathetic_suppress = bool(suppress)
        if suppress:
            for st in self._sympathetic_state.values():
                st["target"] = 0.0

    def set_sympathetic_notes(self, notes: set):
        """Update which piano notes resonate sympathetically (with fade envelopes)."""
        for n in notes:
            if n not in self._sympathetic_state:
                self._sympathetic_state[n] = {
                    "phase_l": 0.0, "phase_r": 0.0,
                    "gain": 0.0, "target": 1.0,
                }
            else:
                self._sympathetic_state[n]["target"] = 1.0
        for n in list(self._sympathetic_state):
            if n not in notes:
                self._sympathetic_state[n]["target"] = 0.0

    def set_drone_chord(self, notes: list):
        """Latch drone to the lowest held note at first call after enable.
        Ignored once latched — drone stays put as a pad underneath subsequent playing."""
        if not notes or self._drone_latched:
            return
        root = min(notes)
        drone_root = max(24, root - 12)
        drone_fifth = max(24, root - 12 + 7)
        self._drone_root_freq = 440.0 * (2.0 ** ((drone_root - 69) / 12.0))
        self._drone_fifth_freq = 440.0 * (2.0 ** ((drone_fifth - 69) / 12.0))
        self._drone_gain_target = 0.5
        self._drone_latched = True

    def drone_off(self):
        """Fade out the drone and clear latch so next enable re-picks the root.
        Zero the smoothed freqs so the next enable snaps to the new root (no glide)."""
        self._drone_gain_target = 0.0
        self._drone_latched = False
        self._drone_root_freq_cur = 0.0
        self._drone_fifth_freq_cur = 0.0

    def render(self, n_samples: int, separate_fx: bool = False):
        if n_samples == 0:
            return np.zeros((2, 0), dtype=np.float64)

        self._ensure_buffers(n_samples)

        if len(self._sample_indices) < n_samples:
            self._sample_indices = np.arange(1, n_samples + 1, dtype=np.float64)
        indices = self._sample_indices[:n_samples]

        # Smooth oscillator blend changes (~5ms), then apply blend dB curve
        smooth = 1.0 - np.exp(-n_samples / (0.005 * self.sample_rate))
        self._osc1_blend_cur += smooth * (self.osc1_blend - self._osc1_blend_cur)
        self._osc2_blend_cur += smooth * (self.osc2_blend - self._osc2_blend_cur)
        # Snap to exact zero when target is 0 — prevents asymptotic CPU waste
        if self.osc1_blend <= 0.0 and self._osc1_blend_cur < 0.005:
            self._osc1_blend_cur = 0.0
        if self.osc2_blend <= 0.0 and self._osc2_blend_cur < 0.005:
            self._osc2_blend_cur = 0.0
        osc1_b = blend_to_amplitude(self._osc1_blend_cur)
        osc2_b = blend_to_amplitude(self._osc2_blend_cur)
        render_osc1 = osc1_b > 0.0
        render_osc2 = osc2_b > 0.0
        render_shimmer = self.shimmer_enabled and self.shimmer_mix > 0.001
        has_drone = self.drone_enabled and self._drone_gain > 0.001
        skip_voices = not render_osc1 and not render_osc2 and not render_shimmer

        both_filtered = self.osc1_filter_enabled and self.osc2_filter_enabled

        # Re-use pre-allocated buffers (zero and slice to current block size)
        filter_buf = self._filter_buf[:, :n_samples]
        filter_buf[:] = 0
        if not self.osc1_filter_enabled:
            osc1_indep_buf = self._osc1_indep_buf[:, :n_samples]
            osc1_indep_buf[:] = 0
        else:
            osc1_indep_buf = None
        if not self.osc2_filter_enabled:
            osc2_indep_buf = self._osc2_indep_buf[:, :n_samples]
            osc2_indep_buf[:] = 0
        else:
            osc2_indep_buf = None
        shimmer_sines = self._shimmer_buf[:n_samples]
        shimmer_sines[:] = 0
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

        # Accumulate osc2 separately for Haas delay (pre-allocated)
        osc2_accum_l = self._osc2_accum_l[:n_samples]
        osc2_accum_l[:] = 0
        osc2_accum_r = self._osc2_accum_r[:n_samples]
        osc2_accum_r[:] = 0

        # ── Per-unison values, precomputed once per block (same for every voice) ──
        # Detune multipliers and stereo spread offsets
        if n_uni > 1:
            u_arr = np.arange(n_uni, dtype=np.float64)
            detune_factor = 2.0 * u_arr / (n_uni - 1) - 1.0  # -1 .. +1
            detune_mult = 2.0 ** (self.unison_detune * detune_factor / 12.0)
            uni_pan_arr = detune_factor * spread
        else:
            detune_mult = np.ones(1, dtype=np.float64)
            uni_pan_arr = np.zeros(1, dtype=np.float64)

        # Per-unison equal-power pan gains for OSC1
        _pan1 = np.clip(o1_pan + uni_pan_arr, -1.0, 1.0)
        _pan1_shaped = np.sign(_pan1) * np.abs(_pan1) ** 0.7
        _angle1 = (_pan1_shaped + 1.0) * 0.25 * np.pi
        o1_gl_arr = np.cos(_angle1) * 1.4142135623730951
        o1_gr_arr = np.sin(_angle1) * 1.4142135623730951
        # Per-unison equal-power pan gains for OSC2
        _pan2 = np.clip(o2_pan + uni_pan_arr, -1.0, 1.0)
        _pan2_shaped = np.sign(_pan2) * np.abs(_pan2) ** 0.7
        _angle2 = (_pan2_shaped + 1.0) * 0.25 * np.pi
        o2_gl_arr = np.cos(_angle2) * 1.4142135623730951
        o2_gr_arr = np.sin(_angle2) * 1.4142135623730951

        osc1_oct_mult = 2.0 ** self.osc1_octave
        osc2_oct_mult = 2.0 ** self.osc2_octave

        voice_idx = 0
        for voice in self.voices:
            if not voice.is_active():
                dead_voices.append(voice)
                continue

            # ADSR: sustain fast path returns scalar (avoids np.full allocation)
            if voice.adsr.stage == ADSREnvelope.SUSTAIN:
                env = voice.adsr.config.sustain_percent / 100.0
            else:
                env = voice.adsr.process(n_samples)

            if skip_voices:
                voice_idx += 1
                continue

            base_freq = 440.0 * (2.0 ** ((voice.note - 69) / 12.0))
            base_inc = TWO_PI * base_freq / self.sample_rate
            osc1_l = self._voice_osc1_l[voice_idx, :n_samples]
            osc1_l[:] = 0
            osc1_r = self._voice_osc1_r[voice_idx, :n_samples]
            osc1_r[:] = 0
            osc2_l = self._voice_osc2_l[voice_idx, :n_samples]
            osc2_l[:] = 0
            osc2_r = self._voice_osc2_r[voice_idx, :n_samples]
            osc2_r[:] = 0
            voice_shimmer = self._voice_shimmer[voice_idx, :n_samples]
            voice_shimmer[:] = 0

            # Per-unison phase increments for this voice (n_uni,)
            osc1_inc_per_u = base_inc * detune_mult * osc1_oct_mult
            osc2_inc_per_u = base_inc * detune_mult * osc2_oct_mult
            # Shimmer: octave-up (+12 = 2x) or two octaves up (+24 = 4x) when shimmer_high
            shim_mult = 4.0 if self.shimmer_high else 2.0
            shim_inc_per_u = base_inc * shim_mult * detune_mult

            if render_osc1:
                uni_starts = np.asarray(voice.osc1_phases[:n_uni], dtype=np.float64)
                ph_2d = uni_starts[:, None] + osc1_inc_per_u[:, None] * indices[None, :]
                wave_2d = generate_waveform(osc1_wf, ph_2d) * osc1_b
                osc1_l += (wave_2d * o1_gl_arr[:, None]).sum(axis=0)
                osc1_r += (wave_2d * o1_gr_arr[:, None]).sum(axis=0)
                # Phase wrap-and-store
                for u in range(n_uni):
                    voice.osc1_phases[u] = float(ph_2d[u, -1]) % TWO_PI

            if render_osc2:
                uni_starts = np.asarray(voice.osc2_phases[:n_uni], dtype=np.float64)
                ph_2d = uni_starts[:, None] + osc2_inc_per_u[:, None] * indices[None, :]
                wave_2d = generate_waveform(osc2_wf, ph_2d) * osc2_b
                osc2_l += (wave_2d * o2_gl_arr[:, None]).sum(axis=0)
                osc2_r += (wave_2d * o2_gr_arr[:, None]).sum(axis=0)
                for u in range(n_uni):
                    voice.osc2_phases[u] = float(ph_2d[u, -1]) % TWO_PI

            if render_shimmer:
                uni_starts = np.asarray(voice.shimmer_phases[:n_uni], dtype=np.float64)
                shim_2d = uni_starts[:, None] + shim_inc_per_u[:, None] * indices[None, :]
                voice_shimmer += np.sin(shim_2d).sum(axis=0)
                # Advance phases by full block length for consistent tracking
                new_phases = (uni_starts + shim_inc_per_u * n_samples) % TWO_PI
                for u in range(n_uni):
                    voice.shimmer_phases[u] = float(new_phases[u])

            scale = (1.0 / max(n_uni, 1)) * (1.0 + 0.15 * (n_uni - 1)) * env * voice.velocity
            osc1_l *= scale
            osc1_r *= scale
            osc2_l *= scale
            osc2_r *= scale

            if render_shimmer:
                shimmer_sines += voice_shimmer * scale * 0.30

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

            voice_idx += 1

        # Apply Haas delay to OSC2 if pans are separated and osc2 is audible
        if haas_active and render_osc2:
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
        if render_osc2:
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

        # ─── LFO compute (once per block) + filter modulation ───
        lfo_a, lfo_b = self._advance_lfo(n_samples)
        effective_cutoff = self._filter_cutoff_cur
        if self.lfo_enabled and self.lfo_target == "filter" and self.lfo_depth > 0.001:
            # ±2 octaves at depth=1 (lfo_a is already scaled by depth)
            effective_cutoff = self._filter_cutoff_cur * (2.0 ** (lfo_a * 2.0))
            effective_cutoff = max(20.0, min(20000.0, effective_cutoff))

        # Only recalculate filter coefficients if cutoff or resonance actually changed
        cutoff_changed = (abs(effective_cutoff - self._filter_cutoff_last_set) > 0.1
                          or self.filter_resonance != self._filter_res_last_set)
        if cutoff_changed:
            self._filter_cutoff_last_set = effective_cutoff
            self._filter_res_last_set = self.filter_resonance
            self.filter_l.set_params(effective_cutoff, self.filter_resonance)
            self.filter_r.set_params(effective_cutoff, self.filter_resonance)
            if self.filter_slope == 24:
                self.filter2_l.set_params(effective_cutoff, self.filter_resonance)
                self.filter2_r.set_params(effective_cutoff, self.filter_resonance)
            if self.reverb_filter_enabled:
                self.reverb_filter_l.set_params(effective_cutoff, self.filter_resonance)
                self.reverb_filter_r.set_params(effective_cutoff, self.filter_resonance)
                if self.filter_slope == 24:
                    self.reverb_filter2_l.set_params(effective_cutoff, self.filter_resonance)
                    self.reverb_filter2_r.set_params(effective_cutoff, self.filter_resonance)

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

        # Chord drone: root+fifth an octave below, through filter+reverb
        if self.drone_enabled and self._drone_root_freq > 0:
            drone_alpha = 1.0 - np.exp(-n_samples / (0.5 * self.sample_rate))
            self._drone_gain += drone_alpha * (self._drone_gain_target - self._drone_gain)
            # Smooth frequency glide in log space (~300ms)
            freq_alpha = 1.0 - np.exp(-n_samples / (0.3 * self.sample_rate))
            if self._drone_root_freq_cur < 20.0:
                self._drone_root_freq_cur = self._drone_root_freq
                self._drone_fifth_freq_cur = self._drone_fifth_freq
            else:
                lr = np.log(self._drone_root_freq_cur)
                lr += freq_alpha * (np.log(self._drone_root_freq) - lr)
                self._drone_root_freq_cur = np.exp(lr)
                lf = np.log(self._drone_fifth_freq_cur)
                lf += freq_alpha * (np.log(self._drone_fifth_freq) - lf)
                self._drone_fifth_freq_cur = np.exp(lf)
            if self._drone_gain > 0.001:
                inc1 = TWO_PI * self._drone_root_freq_cur / self.sample_rate
                ph1 = self._drone_root_phase + inc1 * indices
                root_tone = generate_waveform(self.osc1_waveform, ph1) * self._drone_gain * 0.30
                self._drone_root_phase = ph1[-1] % TWO_PI
                inc2 = TWO_PI * self._drone_fifth_freq_cur / self.sample_rate
                ph2 = self._drone_fifth_phase + inc2 * indices
                fifth_tone = generate_waveform(self.osc1_waveform, ph2) * self._drone_gain * 0.22
                self._drone_fifth_phase = ph2[-1] % TWO_PI
                drone_mix = root_tone + fifth_tone
                filter_buf[0] += drone_mix
                filter_buf[1] += drone_mix
        elif not self.drone_enabled and self._drone_gain > 0.001:
            # Fading out
            drone_alpha = 1.0 - np.exp(-n_samples / (0.5 * self.sample_rate))
            self._drone_gain += drone_alpha * (0.0 - self._drone_gain)

        # Apply stereo filters and combine
        # Filter gain compensation: reduces volume as filter opens to prevent brightness = loudness
        # Saw/square have massive harmonic energy above the cutoff
        f_min = max(self.filter_range_min, 20.0)
        f_max = max(self.filter_range_max, f_min + 1.0)
        f_pos = np.log(max(self._filter_cutoff_cur, f_min) / f_min) / np.log(f_max / f_min)
        f_pos = max(0.0, min(1.0, f_pos))
        # Shallow power curve: spreads compensation evenly across the range
        # f_pos^1.3 with -21dB max: even attenuation from low-mids through highs
        filter_comp = 10.0 ** (-21.0 * f_pos ** 1.3 / 20.0)

        output_l = self._output_l[:n_samples]
        output_l[:] = 0
        output_r = self._output_r[:n_samples]
        output_r[:] = 0
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

        # Highpass (low cut) — smooth in log space like the lowpass
        if self.filter_highpass_hz > 25.0 or self._filter_highpass_cur > 25.0:
            lc_hp = np.log(max(self._filter_highpass_cur, 20.0))
            lt_hp = np.log(max(self.filter_highpass_hz, 20.0))
            lc_hp += alpha_s * (lt_hp - lc_hp)
            self._filter_highpass_cur = np.exp(lc_hp)
            if self._filter_highpass_cur > 25.0:
                self.filter_hp_l.set_params(self._filter_highpass_cur, 0.707)
                self.filter_hp_r.set_params(self._filter_highpass_cur, 0.707)
                output_l = self.filter_hp_l.process(output_l)
                output_r = self.filter_hp_r.process(output_r)

        # ─── LFO amp/pan modulation on pad bus ───
        # Ramp mod values linearly from last block's end-value to this block's new
        # value. That avoids the 10ms-block-sized discontinuity that would
        # otherwise click on every render — particularly audible in the reverb tail.
        if (self.lfo_enabled and self.lfo_depth * self.motion_mix > 0.001
                and self.lfo_target in ("amp", "pan")):
            ramp_a = np.linspace(self._lfo_mod_a_last, lfo_a, n_samples, dtype=np.float64)
            if self.lfo_target == "amp":
                ramp_b = np.linspace(self._lfo_mod_b_last, lfo_b, n_samples, dtype=np.float64)
                # Tremolo: ±50% swing at depth=1
                output_l *= (1.0 + ramp_a * 0.5)
                output_r *= (1.0 + ramp_b * 0.5)
            else:  # pan — opposite scale on L vs R (uses mod_a on both sides)
                output_l *= (1.0 + ramp_a * 0.5)
                output_r *= (1.0 - ramp_a * 0.5)
        # Remember this block's end values for the next block's ramp start
        self._lfo_mod_a_last = lfo_a
        self._lfo_mod_b_last = lfo_b

        # Snapshot dry pad (post-LFO, pre-FX) so callers asking for FX-bypass
        # routing can subtract and extract a clean dry bus for the bus comp.
        _pre_fx_dry_l = output_l.copy() if separate_fx else None
        _pre_fx_dry_r = output_r.copy() if separate_fx else None

        # ─── Ping-pong delay (pre-reverb so taps get cathedral treatment) ───
        self._process_ping_pong(output_l, output_r)

        # Reverb gets stereo input — preserves stereo image in tail
        reverb_in_l = self._reverb_in_l[:n_samples]
        np.copyto(reverb_in_l, output_l)
        reverb_in_r = self._reverb_in_r[:n_samples]
        np.copyto(reverb_in_r, output_r)

        if render_shimmer and voice_idx > 0:
            shimmer_filtered = self._shimmer_hp.process(shimmer_sines)
            # Smooth shimmer mix (~20ms time constant)
            self._shimmer_mix_cur += alpha_s * (self.shimmer_mix - self._shimmer_mix_cur)
            shimmer_sig = shimmer_filtered * self._shimmer_mix_cur
            # Dry shimmer → reverb
            reverb_in_l += shimmer_sig
            reverb_in_r += shimmer_sig
            # Pre-reverb CLOUD: multi-tap bouncing delay scaled by shimmer_send (0..2)
            if self.shimmer_send > 0.001:
                cloud_l = self._shimmer_cloud_l[:n_samples]
                cloud_r = self._shimmer_cloud_r[:n_samples]
                self._process_shimmer_cloud(shimmer_sig, cloud_l, cloud_r)
                reverb_in_l += cloud_l * self.shimmer_send
                reverb_in_r += cloud_r * self.shimmer_send

        # Sympathetic resonance: piano notes generate subtle tones into reverb
        # Stereo detuned (L/R ~5 cents apart), frequency rolloff above C5,
        # smooth fade envelopes to avoid pops on note-off.
        # Batched across held notes: one sin call per channel instead of N,
        # and shared exponential-decay ramp factors computed once per block.
        if self.sympathetic_enabled and not self._sympathetic_suppress and self._sympathetic_state:
            self._sympathetic_level_cur += alpha_s * (self.sympathetic_level - self._sympathetic_level_cur)
            sym_level = self._sympathetic_level_cur
            decay_rate = np.exp(-1.0 / (0.15 * self.sample_rate))
            # Shared across all entries: exponential ramp factors over the block
            ramp_factors = decay_rate ** indices

            notes_list = list(self._sympathetic_state.keys())
            N = len(notes_list)
            freqs = np.empty(N, dtype=np.float64)
            tgts = np.empty(N, dtype=np.float64)
            g0s = np.empty(N, dtype=np.float64)
            ph_l_init = np.empty(N, dtype=np.float64)
            ph_r_init = np.empty(N, dtype=np.float64)
            for i, note in enumerate(notes_list):
                st = self._sympathetic_state[note]
                freqs[i] = 440.0 * 2.0 ** ((note - 69) / 12.0)
                tgts[i] = st["target"]
                g0s[i] = st["gain"]
                ph_l_init[i] = st["phase_l"]
                ph_r_init[i] = st["phase_r"]

            rolloffs = np.minimum(1.0, 523.0 / np.maximum(freqs, 523.0))
            gain_ramps = tgts[:, None] + (g0s - tgts)[:, None] * ramp_factors[None, :]
            final_gains = gain_ramps[:, -1]

            inc_l = TWO_PI * freqs / self.sample_rate
            inc_r = TWO_PI * (freqs * 1.003) / self.sample_rate
            ph_l_2d = ph_l_init[:, None] + inc_l[:, None] * indices[None, :]
            ph_r_2d = ph_r_init[:, None] + inc_r[:, None] * indices[None, :]
            g_scaled = gain_ramps * (sym_level * rolloffs)[:, None]

            reverb_in_l += (np.sin(ph_l_2d) * g_scaled).sum(axis=0)
            reverb_in_r += (np.sin(ph_r_2d) * g_scaled).sum(axis=0)

            dead_sym = []
            for i, note in enumerate(notes_list):
                st = self._sympathetic_state[note]
                st["gain"] = float(final_gains[i])
                if final_gains[i] < 0.001 and tgts[i] <= 0:
                    dead_sym.append(note)
                else:
                    st["phase_l"] = float(ph_l_2d[i, -1]) % TWO_PI
                    st["phase_r"] = float(ph_r_2d[i, -1]) % TWO_PI
            for note in dead_sym:
                del self._sympathetic_state[note]

        np.tanh(reverb_in_l, out=reverb_in_l)
        np.tanh(reverb_in_r, out=reverb_in_r)

        # Main reverb — stereo in, stereo out (reuse _stereo_out as temp)
        self._stereo_out[0, :n_samples] = reverb_in_l
        self._stereo_out[1, :n_samples] = reverb_in_r
        reverb_out = self.reverb.process(self._stereo_out[:, :n_samples])

        # Optionally filter the reverb wet output through main filter
        if self.reverb_filter_enabled:
            reverb_out[0] = self.reverb_filter_l.process(reverb_out[0]) * filter_comp
            reverb_out[1] = self.reverb_filter_r.process(reverb_out[1]) * filter_comp
            if self.filter_slope == 24:
                reverb_out[0] = self.reverb_filter2_l.process(reverb_out[0])
                reverb_out[1] = self.reverb_filter2_r.process(reverb_out[1])

        # Equal-power crossfade: stereo dry oscillators + stereo wet reverb
        # Smooth dry/wet mix (~20ms time constant, reuses alpha_s from filter smoothing)
        self._dry_wet_cur += alpha_s * (self.reverb.dry_wet - self._dry_wet_cur)
        dry_wet = self._dry_wet_cur
        angle = dry_wet * (np.pi / 2.0)
        dry_gain = np.cos(angle)
        wet_gain_val = np.sin(angle) * self.reverb.wet_gain

        stereo_out = self._stereo_out[:, :n_samples]
        stereo_out[0] = output_l * dry_gain + reverb_out[0] * wet_gain_val
        stereo_out[1] = output_r * dry_gain + reverb_out[1] * wet_gain_val

        if separate_fx:
            # dry bus = pre-FX pad × dry_gain.
            # fx bus = final − dry = delay taps × dry_gain + reverb wet × wet_gain_val.
            # Summing dry + fx post-comp reconstructs the same total mix as the
            # single-return path, but the comp only sees the dry bus.
            dry_bus = np.empty((2, n_samples), dtype=np.float64)
            dry_bus[0] = _pre_fx_dry_l * dry_gain
            dry_bus[1] = _pre_fx_dry_r * dry_gain
            fx_bus = np.empty((2, n_samples), dtype=np.float64)
            fx_bus[0] = stereo_out[0] - dry_bus[0]
            fx_bus[1] = stereo_out[1] - dry_bus[1]
            return dry_bus, fx_bus

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
                # Resize phase arrays on all active voices. New slots get
                # random starting phases (same decorrelation trick as note_on)
                # so bumping unison mid-note doesn't cause phase-aligned beating.
                for v in self.voices:
                    while len(v.osc1_phases) < new_count:
                        v.osc1_phases.append(float(np.random.uniform(0.0, TWO_PI)))
                        v.osc2_phases.append(float(np.random.uniform(0.0, TWO_PI)))
                        v.shimmer_phases.append(float(np.random.uniform(0.0, TWO_PI)))
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
        if "haas_delay_ms" in params:
            ms = max(5.0, min(40.0, float(params["haas_delay_ms"])))
            self.haas_delay_ms = ms
            self._haas_delay_samples = int(ms * 0.001 * self.sample_rate)
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
                # Invalidate coefficient cache so filter2 picks up the current
                # cutoff on the next render when switching 12 → 24.
                self._filter_cutoff_last_set = -1.0
        if "filter_highpass_hz" in params:
            self.filter_highpass_hz = max(20.0, min(2000.0, float(params["filter_highpass_hz"])))
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
        if "reverb_filter_enabled" in params:
            was = self.reverb_filter_enabled
            self.reverb_filter_enabled = bool(params["reverb_filter_enabled"])
            # Invalidate cache so reverb filters pick up current cutoff on enable
            if not was and self.reverb_filter_enabled:
                self._filter_cutoff_last_set = -1.0
        if "shimmer_enabled" in params:
            self.shimmer_enabled = bool(params["shimmer_enabled"])
        if "shimmer_mix" in params:
            self.shimmer_mix = max(0.0, min(1.0, float(params["shimmer_mix"])))
        if "shimmer_high" in params:
            self.shimmer_high = bool(params["shimmer_high"])
        if "shimmer_send" in params:
            self.shimmer_send = max(0.0, min(2.0, float(params["shimmer_send"])))
        if "lfo_enabled" in params:
            self.lfo_enabled = bool(params["lfo_enabled"])
        if "lfo_rate_hz" in params:
            self.lfo_rate_hz = max(0.05, min(20.0, float(params["lfo_rate_hz"])))
        if "lfo_depth" in params:
            self.lfo_depth = max(0.0, min(1.0, float(params["lfo_depth"])))
        if "lfo_shape" in params:
            s = str(params["lfo_shape"])
            if s in ("sine", "triangle", "square", "sh"):
                self.lfo_shape = s
        if "lfo_target" in params:
            t = str(params["lfo_target"])
            if t in ("filter", "amp", "pan"):
                self.lfo_target = t
                # Reset filter cache so modulation takes over cleanly
                self._filter_cutoff_last_set = -1.0
        if "lfo_spread" in params:
            self.lfo_spread = max(0.0, min(1.0, float(params["lfo_spread"])))
        if "delay_enabled" in params:
            self.delay_enabled = bool(params["delay_enabled"])
        if "delay_time_mode" in params:
            m = str(params["delay_time_mode"])
            if m == "FREE" or m in self._DELAY_DIVISIONS:
                self.delay_time_mode = m
        if "delay_time_ms" in params:
            self.delay_time_ms = max(1.0, min(1000.0, float(params["delay_time_ms"])))
        if "delay_offset_ms" in params:
            self.delay_offset_ms = max(-200.0, min(200.0, float(params["delay_offset_ms"])))
        if "delay_feedback" in params:
            self.delay_feedback = max(0.0, min(0.85, float(params["delay_feedback"])))
        if "delay_wet" in params:
            self.delay_wet = max(0.0, min(1.0, float(params["delay_wet"])))
        if "bpm" in params:
            self.bpm = max(40.0, min(300.0, float(params["bpm"])))
        if "motion_mix" in params:
            self.motion_mix = max(0.0, min(1.0, float(params["motion_mix"])))
        if "freeze_enabled" in params:
            self.freeze_enabled = bool(params["freeze_enabled"])
            self.reverb.set_freeze(self.freeze_enabled)
        if "sympathetic_enabled" in params:
            self.sympathetic_enabled = bool(params["sympathetic_enabled"])
        if "sympathetic_level" in params:
            self.sympathetic_level = max(0.0, min(0.15, float(params["sympathetic_level"])))
        if "drone_enabled" in params:
            self.drone_enabled = bool(params["drone_enabled"])
            if not self.drone_enabled:
                self.drone_off()

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
            "reverb_filter_enabled": self.reverb_filter_enabled,
            "shimmer_enabled": self.shimmer_enabled,
            "shimmer_mix": self.shimmer_mix,
            "shimmer_high": self.shimmer_high,
            "shimmer_send": self.shimmer_send,
            "lfo_enabled": self.lfo_enabled,
            "lfo_rate_hz": self.lfo_rate_hz,
            "lfo_depth": self.lfo_depth,
            "lfo_shape": self.lfo_shape,
            "lfo_target": self.lfo_target,
            "lfo_spread": self.lfo_spread,
            "delay_enabled": self.delay_enabled,
            "delay_time_mode": self.delay_time_mode,
            "delay_time_ms": self.delay_time_ms,
            "delay_offset_ms": self.delay_offset_ms,
            "delay_feedback": self.delay_feedback,
            "delay_wet": self.delay_wet,
            "motion_mix": self.motion_mix,
            "freeze_enabled": self.freeze_enabled,
            "sympathetic_enabled": self.sympathetic_enabled,
            "sympathetic_level": self.sympathetic_level,
            "volume": self.volume,
        }
