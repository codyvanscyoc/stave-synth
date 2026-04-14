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
    """Gentle 6dB/oct (1-pole) lowpass — transparent for EQ use."""

    def __init__(self, cutoff_hz: float = 20000.0, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self._y1 = 0.0
        self.set_params(cutoff_hz)

    def set_params(self, cutoff_hz: float):
        cutoff_hz = max(20.0, min(cutoff_hz, self.sample_rate * 0.45))
        w = TWO_PI * cutoff_hz / self.sample_rate
        self._a = w / (1.0 + w)

    def process(self, samples: np.ndarray) -> np.ndarray:
        a = self._a
        y1 = self._y1
        out = np.empty(len(samples), dtype=np.float64)
        for i in range(len(samples)):
            y1 += a * (samples[i] - y1)
            out[i] = y1
        self._y1 = y1
        return out

    def reset(self):
        self._y1 = 0.0


class OnePole6dBHighpass:
    """Gentle 6dB/oct (1-pole) highpass — transparent for EQ use."""

    def __init__(self, cutoff_hz: float = 20.0, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self._x1 = 0.0
        self._y1 = 0.0
        self.set_params(cutoff_hz)

    def set_params(self, cutoff_hz: float):
        cutoff_hz = max(20.0, min(cutoff_hz, self.sample_rate * 0.45))
        w = TWO_PI * cutoff_hz / self.sample_rate
        self._a = 1.0 / (1.0 + w)

    def process(self, samples: np.ndarray) -> np.ndarray:
        a = self._a
        x1 = self._x1
        y1 = self._y1
        out = np.empty(len(samples), dtype=np.float64)
        for i in range(len(samples)):
            y1 = a * (y1 + samples[i] - x1)
            x1 = samples[i]
            out[i] = y1
        self._x1 = x1
        self._y1 = y1
        return out

    def reset(self):
        self._x1 = 0.0
        self._y1 = 0.0


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

        # ── Pre-delay: ~25ms gap before early reflections ──
        self.predelay_samples = int(0.025 * sample_rate)
        self.predelay_buf = np.zeros(self.predelay_samples + sample_rate, dtype=np.float64)
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
        # ±24-36 samples at 0.2-0.9 Hz: enough to fully smear comb modes
        self._mod_phases = np.zeros(self.n_lines, dtype=np.float64)
        self._mod_rates = np.array([0.23, 0.37, 0.47, 0.61, 0.73, 0.89, 0.31, 0.53],
                                    dtype=np.float64) * TWO_PI / sample_rate
        self._mod_depths = np.array([28, 32, 24, 36, 26, 30, 34, 25], dtype=np.float64)

        # Feedback frequency bounds
        self.fb_lowcut = BiquadHighpass(80.0, 0.707, sample_rate)
        self.fb_highcut = BiquadLowpass(7000.0, 0.707, sample_rate)
        self._fb_lc_zi = [np.zeros(2, dtype=np.float64) for _ in range(self.n_lines)]
        self._fb_hc_zi = [np.zeros(2, dtype=np.float64) for _ in range(self.n_lines)]

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

    def set_decay(self, seconds: float):
        if seconds > 0:
            avg_delay = sum(self.delays) / len(self.delays)
            loops_per_sec = self.sample_rate / avg_delay
            self.feedback = 10.0 ** (-3.0 / (seconds * loops_per_sec))
            self.feedback = min(self.feedback, 0.95)
        else:
            self.feedback = 0.0

    def set_low_cut(self, freq_hz: float):
        self.fb_lowcut.set_params(max(20.0, freq_hz), 0.707)
        for i in range(self.n_lines):
            self._fb_lc_zi[i][:] = 0.0

    def set_high_cut(self, freq_hz: float):
        self.fb_highcut.set_params(min(freq_hz, 20000.0), 0.707)
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
        """Process mono input, return stereo (2, n) output."""
        n = len(samples)
        if n == 0:
            return np.zeros((2, 0), dtype=np.float64)

        # ── Pre-delay ──
        pd = self.predelay_samples
        pd_bs = len(self.predelay_buf)
        pd_pos = self.predelay_pos
        end = pd_pos + n
        if end <= pd_bs:
            self.predelay_buf[pd_pos:end] = samples
        else:
            first = pd_bs - pd_pos
            self.predelay_buf[pd_pos:pd_bs] = samples[:first]
            self.predelay_buf[:end - pd_bs] = samples[first:]
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

        # When frozen: feedback only, no new input
        input_signal = diffused if not self.frozen else np.zeros(n, dtype=np.float64)

        for i in range(self.n_lines):
            fb_signal = mixed[i] * fb
            if HAS_SCIPY:
                fb_signal, self._damp_zi[i] = lfilter(b_lp, a_lp, fb_signal, zi=self._damp_zi[i])
                fb_signal, self._fb_lc_zi[i] = lfilter(lc_b, lc_a, fb_signal, zi=self._fb_lc_zi[i])
                fb_signal, self._fb_hc_zi[i] = lfilter(hc_b, hc_a, fb_signal, zi=self._fb_hc_zi[i])
            self._write_block(self.bufs[i], input_signal + fb_signal, n)

        self.write_pos = (self.write_pos + n) % self.buf_size

        # ── Soft-limit wet taps so FDN energy can't clip ──
        wet_l = np.tanh(wet_l)
        wet_r = np.tanh(wet_r)

        # ── Equal-power crossfade: more wet = washier, NOT louder ──
        angle = self.dry_wet * (np.pi / 2.0)
        dry_gain = np.cos(angle)
        wet_base = np.sin(angle) * self.wet_gain
        er_gain = wet_base * 0.4 if not self.frozen else 0.0

        out_l = samples * dry_gain + er_left * er_gain + wet_l * wet_base
        out_r = samples * dry_gain + er_right * er_gain + wet_r * wet_base

        return np.array([out_l, out_r])


class GranularOctaveUp:
    """Granular pitch shifter — octave up (2x frequency).

    Four overlapping grains read from a circular buffer at double speed,
    crossfaded with Hanning windows. More grains + longer grain size = smoother
    splicing with no tonal artifacts. Vectorized with numpy index arrays.
    """

    N_GRAINS = 4

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.grain_size = int(0.060 * sample_rate)  # 60ms grains (was 30ms)
        self.buf_size = sample_rate * 2
        self.buf = np.zeros(self.buf_size, dtype=np.float64)
        self.write_pos = 0
        # 4 grains evenly spaced across the grain window
        gs = self.grain_size
        self.read_phases = [float(i * gs // self.N_GRAINS) for i in range(self.N_GRAINS)]
        self._hann = np.hanning(gs)

    def process(self, samples: np.ndarray) -> np.ndarray:
        n = len(samples)
        gs = self.grain_size
        bs = self.buf_size

        # Write input block into circular buffer
        wp = self.write_pos
        end = wp + n
        if end <= bs:
            self.buf[wp:end] = samples
        else:
            first = bs - wp
            self.buf[wp:bs] = samples[:first]
            self.buf[:end - bs] = samples[first:]

        offsets = np.arange(n, dtype=np.float64)
        write_positions = (wp + offsets + 1).astype(np.int64) % bs

        out = np.zeros(n, dtype=np.float64)
        for g in range(self.N_GRAINS):
            phases = (self.read_phases[g] + offsets * 2.0) % gs
            pos = phases.astype(np.int64) % gs
            idx = (write_positions - gs + pos) % bs
            win = self._hann[pos]
            out += self.buf[idx] * win

        # Normalize by number of grains / 2 (Hanning windows sum to ~2 with 4 grains)
        out *= (2.0 / self.N_GRAINS)

        self.write_pos = end % bs
        for g in range(self.N_GRAINS):
            self.read_phases[g] = (self.read_phases[g] + n * 2.0) % gs

        return out

    def reset(self):
        self.buf[:] = 0.0
        gs = self.grain_size
        self.read_phases = [float(i * gs // self.N_GRAINS) for i in range(self.N_GRAINS)]
        self.write_pos = 0


class ShimmerReverb:
    """Shimmer reverb — pitch shifter INSIDE the feedback loop (Eno/Valhalla style).

    Architecture:
        input → diffusers → 8-line FDN (Hadamard) → taps → wet output
                                  ↑                          |
                                  |              ┌───────────┤
                                  |              |           |
                                  |         [damping]   [pitch shift → HP]
                                  |              |           |
                                  └──── (1-shim)*damped + shim*pitched ──┘

    Key: damping is applied to the CLEAN feedback path only. The pitch-shifted
    path stays bright — so each loop around, the octave-up content keeps its
    sparkle while the fundamental decays into warmth. This creates the
    cascading spectral cloud that defines shimmer.
    """

    def __init__(self, decay_seconds: float = 10.0, damping: float = 0.2,
                 shimmer_amount: float = 0.6, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.dry_wet = 1.0  # full wet — shimmer_mix fader controls the level
        self.shimmer_amount = shimmer_amount

        # 8 delay lines — long, prime-ish, well-spread for dense wash
        delay_times_ms = [61.3, 71.9, 79.3, 88.7, 97.1, 107.3, 117.9, 127.1]
        self.n_lines = len(delay_times_ms)
        self.delays = [int(t * sample_rate / 1000) for t in delay_times_ms]

        buf_size = max(self.delays) + sample_rate * 2
        self.bufs = [np.zeros(buf_size, dtype=np.float64) for _ in range(self.n_lines)]
        self.buf_size = buf_size
        self.write_pos = 0

        # Damping on CLEAN feedback path only — keeps shimmer highs bright
        self.damping = damping
        self._damp_zi = [np.zeros(1, dtype=np.float64) for _ in range(self.n_lines)]

        # Pitch shifter in the feedback loop — octave up
        self.pitch_shifter = GranularOctaveUp(sample_rate)

        # Highpass on pitch-shifted path — 1.8kHz so shimmer is pure sparkle/air
        self.shimmer_hp = BiquadHighpass(1800.0, 0.707, sample_rate)

        # Gentle lowpass on pitch-shifted path — tame harsh 10kHz+ artifacts
        self.shimmer_lp = BiquadLowpass(12000.0, 0.707, sample_rate)

        # 6 all-pass diffusers — varied gains for thorough decorrelation
        ap_config = [
            (17.3, 0.60), (23.9, 0.45), (31.3, 0.55),
            (38.7, 0.45), (47.1, 0.50), (58.3, 0.55),
        ]
        self.diffusers = [
            AllPassDiffuser(int(t * sample_rate / 1000), gain=g)
            for t, g in ap_config
        ]

        self.feedback = 0.0
        self.set_decay(decay_seconds)

    def set_decay(self, seconds: float):
        if seconds > 0:
            avg_delay = sum(self.delays) / len(self.delays)
            loops_per_sec = self.sample_rate / avg_delay
            self.feedback = 10.0 ** (-3.0 / (seconds * loops_per_sec))
            self.feedback = min(self.feedback, 0.95)
        else:
            self.feedback = 0.0

    def _read_block(self, buf, delay, n):
        start = (self.write_pos - delay) % self.buf_size
        if start + n <= self.buf_size:
            return buf[start:start + n].copy()
        first = self.buf_size - start
        return np.concatenate([buf[start:self.buf_size], buf[:n - first]])

    def _write_block(self, buf, data, n):
        end = self.write_pos + n
        if end <= self.buf_size:
            buf[self.write_pos:end] = data
        else:
            first = self.buf_size - self.write_pos
            buf[self.write_pos:self.buf_size] = data[:first]
            buf[:end - self.buf_size] = data[first:]

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Process mono input, return stereo (2, n) output."""
        n = len(samples)
        if n == 0:
            return np.zeros((2, 0), dtype=np.float64)

        fb = self.feedback
        damp = self.damping
        shim = self.shimmer_amount
        b_lp = np.array([1.0 - damp])
        a_lp = np.array([1.0, -damp])

        # Diffuse input through all-pass chain
        diffused = samples.copy()
        for ap in self.diffusers:
            diffused = ap.process(diffused)

        # Read delayed taps
        taps = [self._read_block(self.bufs[i], self.delays[i], n)
                for i in range(self.n_lines)]

        # Stereo wet: even taps → L, odd taps → R
        half = self.n_lines // 2
        wet_l = np.zeros(n, dtype=np.float64)
        wet_r = np.zeros(n, dtype=np.float64)
        wet_sum = np.zeros(n, dtype=np.float64)
        for i, tap in enumerate(taps):
            wet_sum += tap
            if i % 2 == 0:
                wet_l += tap
            else:
                wet_r += tap
        wet_l *= (1.0 / np.sqrt(half))
        wet_r *= (1.0 / np.sqrt(half))

        # Pitch-shift the mono sum for feedback
        wet_mono = wet_sum * (1.0 / self.n_lines)
        pitched = self.pitch_shifter.process(wet_mono)
        pitched = self.shimmer_hp.process(pitched)
        pitched = self.shimmer_lp.process(pitched)

        # Hadamard mixing of taps for maximally decorrelated feedback
        taps_matrix = np.array(taps)  # (8, n)
        mixed = _HADAMARD @ taps_matrix  # (8, n)

        for i in range(self.n_lines):
            clean_fb = mixed[i] * fb
            if HAS_SCIPY:
                clean_fb, self._damp_zi[i] = lfilter(
                    b_lp, a_lp, clean_fb, zi=self._damp_zi[i]
                )
            fb_signal = clean_fb * (1.0 - shim) + pitched * shim * fb
            self._write_block(self.bufs[i], diffused + fb_signal, n)

        self.write_pos = (self.write_pos + n) % self.buf_size

        dw = self.dry_wet
        out_l = samples * (1.0 - dw) + wet_l * dw
        out_r = samples * (1.0 - dw) + wet_r * dw
        return np.array([out_l, out_r])


class SmoothPitchShift:
    """Smooth granular pitch shifter — configurable ratio, long grains, heavy overlap.

    8 overlapping grains at 120ms with Hanning crossfade = nearly seamless pitch shift.
    No buzzy splice artifacts. Ratio is configurable (2.0 = octave, 1.5 = fifth).
    """

    N_GRAINS = 8

    def __init__(self, ratio: float = 2.0, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.ratio = ratio
        self.grain_size = int(0.120 * sample_rate)  # 120ms grains
        self.buf_size = sample_rate * 2
        self.buf = np.zeros(self.buf_size, dtype=np.float64)
        self.write_pos = 0
        gs = self.grain_size
        self.read_phases = [float(i * gs // self.N_GRAINS) for i in range(self.N_GRAINS)]
        self._hann = np.hanning(gs)

    def process(self, samples: np.ndarray) -> np.ndarray:
        n = len(samples)
        gs = self.grain_size
        bs = self.buf_size
        ratio = self.ratio

        wp = self.write_pos
        end = wp + n
        if end <= bs:
            self.buf[wp:end] = samples
        else:
            first = bs - wp
            self.buf[wp:bs] = samples[:first]
            self.buf[:end - bs] = samples[first:]

        offsets = np.arange(n, dtype=np.float64)
        write_positions = (wp + offsets + 1).astype(np.int64) % bs

        out = np.zeros(n, dtype=np.float64)
        for g in range(self.N_GRAINS):
            phases = (self.read_phases[g] + offsets * ratio) % gs
            pos = phases.astype(np.int64) % gs
            # Linear interpolation between samples for smoother output
            frac = phases - np.floor(phases)
            idx0 = (write_positions - gs + pos) % bs
            idx1 = (idx0 + 1) % bs
            win = self._hann[pos]
            out += (self.buf[idx0] * (1.0 - frac) + self.buf[idx1] * frac) * win

        # Normalize: 8 Hanning windows sum to ~4
        out *= (2.0 / self.N_GRAINS)

        self.write_pos = end % bs
        for g in range(self.N_GRAINS):
            self.read_phases[g] = (self.read_phases[g] + n * ratio) % gs

        return out

    def reset(self):
        self.buf[:] = 0.0
        gs = self.grain_size
        self.read_phases = [float(i * gs // self.N_GRAINS) for i in range(self.N_GRAINS)]
        self.write_pos = 0


class CascadeShimmer:
    """Ethereal shimmer — detuned pitch-shift chorus in a modulated feedback loop.

    What makes it sound angelic instead of buzzy:
    1. TWO pitch shifters at slightly different ratios (2.0 and 2.003) = natural chorus
       The beating between them creates width and movement without any LFO.
    2. Smooth pitch shifting: 8 grains at 120ms with linear interpolation.
    3. All-pass diffusion AFTER the pitch shift smooths any remaining artifacts
       before the signal feeds back.
    4. Modulated delay read positions (slow LFO) = dreamy, non-static tail.
    5. Gentle slopes on the HP/LP so the shimmer blends into the air.
    """

    def __init__(self, decay_seconds: float = 8.0, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.dry_wet = 1.0  # full wet — shimmer_mix fader controls the level

        # Two delay lines — modulated read positions
        d1_ms, d2_ms = 97.3, 131.7
        self.delay_a = int(d1_ms * sample_rate / 1000)
        self.delay_b = int(d2_ms * sample_rate / 1000)

        buf_size = max(self.delay_a, self.delay_b) + sample_rate * 2 + 512
        self.buf_a = np.zeros(buf_size, dtype=np.float64)
        self.buf_b = np.zeros(buf_size, dtype=np.float64)
        self.buf_size = buf_size
        self.write_pos = 0

        # TWO pitch shifters — slightly detuned for chorus shimmer
        # Default: perfect fifth (1.4983x = 7 semitones) — creates harmony, not just higher
        # The slight detune between A and B creates gentle beating = ethereal movement
        # Pitch shifters — octave up, slightly detuned pair for chorus
        self._init_pitch_shifters(sample_rate)

        # Gentle highpass — 600Hz with low Q for a soft knee
        self.shimmer_hp = BiquadHighpass(600.0, 0.5, sample_rate)

        # Gentle lowpass — 13kHz, keep the air, tame harshness
        self.shimmer_lp = BiquadLowpass(13000.0, 0.5, sample_rate)

        # All-pass diffusers AFTER the pitch shift — smooth the pitched signal
        # before it feeds back. This is key for ethereal vs buzzy.
        self.post_diffusers = [
            AllPassDiffuser(int(19.3 * sample_rate / 1000), gain=0.45),
            AllPassDiffuser(int(29.7 * sample_rate / 1000), gain=0.45),
            AllPassDiffuser(int(41.3 * sample_rate / 1000), gain=0.45),
        ]

        # Input diffusers — smear the dry signal gently
        self.pre_diffusers = [
            AllPassDiffuser(int(13.1 * sample_rate / 1000), gain=0.5),
            AllPassDiffuser(int(21.7 * sample_rate / 1000), gain=0.5),
        ]

        # Delay modulation — slow LFOs on read position for dreamy movement
        self._mod_phase_a = 0.0
        self._mod_phase_b = 0.0
        self._mod_rate_a = 0.37 * TWO_PI / sample_rate  # 0.37 Hz
        self._mod_rate_b = 0.53 * TWO_PI / sample_rate  # 0.53 Hz
        self._mod_depth = 18.0  # samples of modulation

        # Very light damping
        self.damping = 0.12
        self._damp_zi_a = np.zeros(1, dtype=np.float64)
        self._damp_zi_b = np.zeros(1, dtype=np.float64)

        self.feedback = 0.0
        self.set_decay(decay_seconds)

    def _init_pitch_shifters(self, sr):
        base = 2.0  # octave up
        detune = base * 0.002  # slight detune for chorus
        self.pitch_a = SmoothPitchShift(ratio=base, sample_rate=sr)
        self.pitch_b = SmoothPitchShift(ratio=base + detune, sample_rate=sr)

    def set_decay(self, seconds: float):
        if seconds > 0:
            avg_delay = (self.delay_a + self.delay_b) / 2
            loops_per_sec = self.sample_rate / avg_delay
            self.feedback = 10.0 ** (-3.0 / (seconds * loops_per_sec))
            self.feedback = min(self.feedback, 0.93)
        else:
            self.feedback = 0.0

    def _read_modulated(self, buf, base_delay, mod_phase, mod_rate, n):
        """Read with slow sinusoidal modulation on delay time (vectorized)."""
        bs = self.buf_size
        sample_idx = np.arange(n, dtype=np.float64)
        phases = mod_phase + mod_rate * sample_idx
        mod = np.sin(phases) * self._mod_depth

        float_pos = (self.write_pos + sample_idx - base_delay - mod) % bs
        pos0 = float_pos.astype(np.int64) % bs
        pos1 = (pos0 + 1) % bs
        frac = float_pos - np.floor(float_pos)
        return buf[pos0] * (1.0 - frac) + buf[pos1] * frac

    def _write(self, buf, data, n):
        end = self.write_pos + n
        if end <= self.buf_size:
            buf[self.write_pos:end] = data
        else:
            first = self.buf_size - self.write_pos
            buf[self.write_pos:self.buf_size] = data[:first]
            buf[:end - self.buf_size] = data[first:]

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Process mono input, return stereo (2, n) output."""
        n = len(samples)
        if n == 0:
            return np.zeros((2, 0), dtype=np.float64)

        fb = self.feedback
        damp = self.damping
        b_lp = np.array([1.0 - damp])
        a_lp = np.array([1.0, -damp])

        # Pre-diffuse input
        diffused = samples.copy()
        for ap in self.pre_diffusers:
            diffused = ap.process(diffused)

        # Read modulated delayed taps
        tap_a = self._read_modulated(self.buf_a, self.delay_a,
                                      self._mod_phase_a, self._mod_rate_a, n)
        tap_b = self._read_modulated(self.buf_b, self.delay_b,
                                      self._mod_phase_b, self._mod_rate_b, n)

        # Update mod phases
        self._mod_phase_a = (self._mod_phase_a + self._mod_rate_a * n) % TWO_PI
        self._mod_phase_b = (self._mod_phase_b + self._mod_rate_b * n) % TWO_PI

        # Mono sum for pitch shifting
        wet_mono = (tap_a + tap_b) * 0.5

        # Dual pitch shift — two slightly detuned octave-up copies
        pitched_a = self.pitch_a.process(wet_mono)
        pitched_b = self.pitch_b.process(wet_mono)
        pitched = (pitched_a + pitched_b) * 0.5

        # Filter the pitched signal
        pitched = self.shimmer_hp.process(pitched)
        pitched = self.shimmer_lp.process(pitched)

        # Post-diffuse
        for ap in self.post_diffusers:
            pitched = ap.process(pitched)

        # Feedback: cross-feed taps + pitched content
        fb_a = (tap_b * 0.2 + pitched * 0.8) * fb
        fb_b = (tap_a * 0.2 + pitched * 0.8) * fb

        if HAS_SCIPY:
            fb_a, self._damp_zi_a = lfilter(b_lp, a_lp, fb_a, zi=self._damp_zi_a)
            fb_b, self._damp_zi_b = lfilter(b_lp, a_lp, fb_b, zi=self._damp_zi_b)

        self._write(self.buf_a, diffused + fb_a, n)
        self._write(self.buf_b, diffused + fb_b, n)

        self.write_pos = (self.write_pos + n) % self.buf_size

        # Stereo: tap_a → L, tap_b → R (natural width from different delay times)
        dw = self.dry_wet
        out_l = samples * (1.0 - dw) + tap_a * dw
        out_r = samples * (1.0 - dw) + tap_b * dw
        return np.array([out_l, out_r])


@dataclass
class Voice:
    note: int = 0
    velocity: float = 1.0
    adsr: ADSREnvelope = field(default_factory=lambda: ADSREnvelope(ADSRConfig()))
    phases: list = field(default_factory=list)
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
        self.unison_detune = 0.05

        self.adsr_config = ADSRConfig()

        self.filter = BiquadLowpass(8000.0, 0.707, sample_rate)
        self.filter2 = BiquadLowpass(8000.0, 0.707, sample_rate)  # Second stage for 24dB
        self.filter_cutoff = 8000.0
        self._filter_cutoff_cur = 8000.0
        self.filter_resonance = 0.707
        self.filter_slope = 12  # 12 or 24 dB/oct
        self.osc1_filter_enabled = True
        self.osc2_filter_enabled = True

        # Independent per-osc filters (used when shared filter is unchecked for that osc)
        self.osc1_indep_filter = BiquadLowpass(20000.0, 0.707, sample_rate)
        self.osc1_indep_cutoff = 20000.0
        self._osc1_indep_cutoff_cur = 20000.0
        self.osc2_indep_filter = BiquadLowpass(20000.0, 0.707, sample_rate)
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
            return np.zeros(0, dtype=np.float64)

        if len(self._sample_indices) < n_samples:
            self._sample_indices = np.arange(1, n_samples + 1, dtype=np.float64)
        indices = self._sample_indices[:n_samples]

        # Smooth oscillator blend changes (~5ms), then apply blend dB curve
        smooth = 1.0 - np.exp(-n_samples / (0.005 * self.sample_rate))
        self._osc1_blend_cur += smooth * (self.osc1_blend - self._osc1_blend_cur)
        self._osc2_blend_cur += smooth * (self.osc2_blend - self._osc2_blend_cur)
        osc1_b = blend_to_amplitude(self._osc1_blend_cur)
        osc2_b = blend_to_amplitude(self._osc2_blend_cur)

        # If both oscs are on the shared filter, use fast single-buffer path.
        # Otherwise each osc gets its own buffer for independent filtering.
        both_filtered = self.osc1_filter_enabled and self.osc2_filter_enabled

        output_for_filter = np.zeros(n_samples, dtype=np.float64)
        osc1_indep_buf = np.zeros(n_samples, dtype=np.float64) if not self.osc1_filter_enabled else None
        osc2_indep_buf = np.zeros(n_samples, dtype=np.float64) if not self.osc2_filter_enabled else None
        # Synthesized shimmer: octave-up sines from held notes
        shimmer_sines = np.zeros(n_samples, dtype=np.float64)
        dead_voices = []

        osc1_wf = self.osc1_waveform
        osc2_wf = self.osc2_waveform

        for voice in self.voices:
            if not voice.is_active():
                dead_voices.append(voice)
                continue

            env = voice.adsr.process(n_samples)
            base_freq = 440.0 * (2.0 ** ((voice.note - 69) / 12.0))
            osc1_oct_mult = 2.0 ** self.osc1_octave
            osc2_oct_mult = 2.0 ** self.osc2_octave
            osc1_buf = np.zeros(n_samples, dtype=np.float64)
            osc2_buf = np.zeros(n_samples, dtype=np.float64)
            voice_shimmer = np.zeros(n_samples, dtype=np.float64)

            for u in range(self.unison_voices):
                if self.unison_voices > 1:
                    detune_st = self.unison_detune * (
                        2.0 * u / (self.unison_voices - 1) - 1.0
                    )
                else:
                    detune_st = 0.0

                freq = base_freq * (2.0 ** (detune_st / 12.0))
                phase_inc = TWO_PI * freq / self.sample_rate

                phases = voice.phases[u] + phase_inc * indices

                osc1_buf += generate_waveform(osc1_wf, phases * osc1_oct_mult) * osc1_b
                osc2_buf += generate_waveform(osc2_wf, phases * osc2_oct_mult) * osc2_b

                # Shimmer: pure sine one octave up (2x phase = double frequency)
                if self.shimmer_enabled:
                    voice_shimmer += np.sin(phases * 2.0)

                voice.phases[u] = phases[-1] % TWO_PI

            scale = (1.0 / max(self.unison_voices, 1) ** 0.5) * env * voice.velocity
            osc1_buf *= scale
            osc2_buf *= scale

            # Accumulate shimmer: envelope-shaped, quiet (reverb will amplify)
            if self.shimmer_enabled:
                shimmer_sines += voice_shimmer * scale * 0.15

            if both_filtered:
                output_for_filter += osc1_buf
                output_for_filter += osc2_buf
            else:
                if self.osc1_filter_enabled:
                    output_for_filter += osc1_buf
                else:
                    osc1_indep_buf += osc1_buf
                if self.osc2_filter_enabled:
                    output_for_filter += osc2_buf
                else:
                    osc2_indep_buf += osc2_buf

        for v in dead_voices:
            self.voices.remove(v)

        # Smooth filter cutoff in log space (~80ms time constant for musical sweeps)
        alpha_s = 1.0 - np.exp(-n_samples / (0.08 * self.sample_rate))

        log_cur = np.log(max(self._filter_cutoff_cur, 20.0))
        log_tgt = np.log(max(self.filter_cutoff, 20.0))
        log_cur += alpha_s * (log_tgt - log_cur)
        self._filter_cutoff_cur = np.exp(log_cur)
        self.filter.set_params(self._filter_cutoff_cur, self.filter_resonance)
        if self.filter_slope == 24:
            self.filter2.set_params(self._filter_cutoff_cur, self.filter_resonance)

        # Smooth independent per-osc filter cutoffs
        if not self.osc1_filter_enabled:
            lc = np.log(max(self._osc1_indep_cutoff_cur, 20.0))
            lt = np.log(max(self.osc1_indep_cutoff, 20.0))
            lc += alpha_s * (lt - lc)
            self._osc1_indep_cutoff_cur = np.exp(lc)
            self.osc1_indep_filter.set_params(self._osc1_indep_cutoff_cur, 0.707)
        if not self.osc2_filter_enabled:
            lc = np.log(max(self._osc2_indep_cutoff_cur, 20.0))
            lt = np.log(max(self.osc2_indep_cutoff, 20.0))
            lc += alpha_s * (lt - lc)
            self._osc2_indep_cutoff_cur = np.exp(lc)
            self.osc2_indep_filter.set_params(self._osc2_indep_cutoff_cur, 0.707)

        # Apply filters and combine
        output = np.zeros(n_samples, dtype=np.float64)
        if self.osc1_filter_enabled or self.osc2_filter_enabled:
            filtered = self.filter.process(output_for_filter)
            if self.filter_slope == 24:
                filtered = self.filter2.process(filtered)
            output += filtered
        if not self.osc1_filter_enabled:
            output += self.osc1_indep_filter.process(osc1_indep_buf)
        if not self.osc2_filter_enabled:
            output += self.osc2_indep_filter.process(osc2_indep_buf)

        # Add shimmer sines to reverb input — they become part of the reverb wash
        # Shimmer mix controls how much octave-up content enters the reverb
        if self.shimmer_enabled:
            # Highpass only — cut low rumble, let highs ring open and airy
            shimmer_filtered = self._shimmer_hp.process(shimmer_sines)
            reverb_input = output + shimmer_filtered * self.shimmer_mix
        else:
            reverb_input = output

        # Soft-limit reverb input so shimmer + pad can't overdrive the FDN
        reverb_input = np.tanh(reverb_input)

        # Main reverb — returns stereo (2, n)
        stereo_out = self.reverb.process(reverb_input)

        # No clip here — limiter applied after piano mix in render loop

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
            self.unison_voices = max(1, min(5, int(params["unison_voices"])))
        if "unison_detune" in params:
            self.unison_detune = float(params["unison_detune"])
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
        if "filter_slope" in params:
            slope = int(params["filter_slope"])
            if slope in (12, 24):
                self.filter_slope = slope
                if slope == 12:
                    self.filter2.reset()
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
            "reverb_decay_seconds": 6.0,
            "reverb_low_cut": 80.0,
            "reverb_high_cut": 7000.0,
            "shimmer_enabled": self.shimmer_enabled,
            "shimmer_mix": self.shimmer_mix,
            "freeze_enabled": self.freeze_enabled,
            "volume": self.volume,
        }
