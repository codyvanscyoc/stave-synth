"""Synth pad engine: oscillators, filter, ADSR, reverb, shimmer, voice management.

Performance-critical: all audio processing uses vectorized NumPy.
"""

import logging
import math
import threading
import numpy as np
from dataclasses import dataclass, field

from .config import (
    SAMPLE_RATE, LOW_RAM_MODE,
    USE_FAUST_REVERB, USE_FAUST_PING_PONG, USE_FAUST_OSC_BANK, USE_FAUST_SYMPATHETIC,
    USE_FAUST_PAD_BUS, USE_FAUST_MERGED,
)

from scipy.signal import lfilter

logger = logging.getLogger(__name__)

TWO_PI = 2.0 * np.pi

# Butterworth 4-pole decomposition — two cascaded biquads with STAGGERED Q.
# Used for the 24 dB/oct filter slope. Previously both stages shared Q=0.707
# (user's `filter_resonance`), which is a biquad² cascade with an effective
# passband Q ≈ 0.54 AND a squared resonance peak — "24 dB mode felt MORE
# resonant than 12 dB" because it literally was by ~6 dB. Butterworth 4-pole
# needs Q=0.5412 (damped) on stage 1 and Q=1.3066 (resonant) on stage 2; the
# two combine to flat passband + proper 24 dB slope. The ratios below scale
# the user's `filter_resonance` so that at the default res=0.707 the cascade
# lands exactly on Butterworth, and increasing res scales both stages up
# together to add resonance character.
_Q24_S1_RATIO = 0.5412 / 0.707   # ≈ 0.7654 — stage 1 damping factor
_Q24_S2_RATIO = 1.3066 / 0.707   # ≈ 1.8480 — stage 2 resonance factor

# dB volume curves: maps a 0-1 fader to amplitude via exponential (dB) scaling.
# Hard digital zero at fader=0.
MASTER_DB_RANGE = 60.0  # -60dB to 0dB for master/volume faders
BLEND_DB_RANGE = 24.0   # -24dB to 0dB for oscillator blend faders (gentler taper)

# Available oscillator waveforms
WAVEFORMS = ["sine", "square", "saw", "triangle", "saturated"]

# Voice pool size. Must not exceed the Faust osc bank's slot count: on
# LOW_RAM_MODE the wrappers load the 12-slot lite .so builds (see the lite
# pass in faust/build.sh and faust_osc_bank.NVOICES), so the pool shrinks
# to match. Full profile (Pi 5) stays at 24 — behavior unchanged.
MAX_VOICES = 12 if LOW_RAM_MODE else 24

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

def _poly_blep(t: np.ndarray, dt) -> np.ndarray:
    """Vectorised polyBLEP correction at a phase wrap (0/1 boundary).

    t  : phase fraction in [0, 1), shape (..., n_samples)
    dt : per-sample phase increment as fraction of cycle. Scalar OR a
         broadcast-compatible array (e.g. shape (n_uni, 1) when t is
         (n_uni, n_samples) for unison-detuned voices).

    Returns a correction array (same shape as t) to add to the naive
    waveform. Subtract for sawtooth, combine for square (2 discontinuities)."""
    blep_lo = np.zeros_like(t)
    blep_hi = np.zeros_like(t)
    dt_safe = np.maximum(dt, 1e-9)
    # Just past the wrap (t in [0, dt))
    mask_lo = t < dt
    if np.any(mask_lo):
        tt = t / dt_safe
        blep_lo = np.where(mask_lo, 2.0 * tt - tt * tt - 1.0, 0.0)
    # About to wrap (t in [1-dt, 1))
    mask_hi = t > (1.0 - dt)
    if np.any(mask_hi):
        tt = (t - 1.0) / dt_safe
        blep_hi = np.where(mask_hi, tt * tt + 2.0 * tt + 1.0, 0.0)
    return blep_lo + blep_hi


def generate_waveform(waveform: str, phases: np.ndarray, dt=None) -> np.ndarray:
    """Generate a waveform from phase array. All outputs are roughly -1..+1.

    dt (optional) is the per-sample phase increment as a fraction of cycle
    (= freq / sample_rate). When provided, sawtooth and square use polyBLEP
    for band-limited generation — eliminates aliasing buzz on bright voices.
    Pass None for slow signals (LFOs, etc.) where aliasing doesn't matter."""
    if waveform == "square":
        sq = np.sign(np.sin(phases))
        if dt is not None:
            # Square has rising AND falling discontinuities; apply polyBLEP
            # at the rising edge (t=0) and subtract at the falling edge (t=0.5).
            t = (phases / TWO_PI) % 1.0
            sq = sq + _poly_blep(t, dt) - _poly_blep((t + 0.5) % 1.0, dt)
        return sq
    elif waveform == "saw":
        t = (phases / TWO_PI) % 1.0
        naive = 2.0 * t - 1.0
        if dt is not None:
            return naive - _poly_blep(t, dt)
        return naive
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


# Exponential-factor table cache for ADSR decay/release:
# base ** [1..n] is constant per (base, n) but was recomputed per voice per
# block — a pow() per sample, ~4% of the small-Pi render budget under load.
# Envelope settings change rarely and n is the block size, so a tiny keyed
# cache turns it into a dict hit. Values are identical (same np computation).
# Bounded: settings changes evict via FIFO. Entries are treated as
# read-only; callers only multiply/add into fresh arrays.
_EXP_FACTOR_CACHE: dict = {}
_EXP_FACTOR_CACHE_MAX = 64


def _exp_factors(base: float, n: int) -> np.ndarray:
    key = (base, n)
    hit = _EXP_FACTOR_CACHE.get(key)
    if hit is None:
        if len(_EXP_FACTOR_CACHE) >= _EXP_FACTOR_CACHE_MAX:
            _EXP_FACTOR_CACHE.pop(next(iter(_EXP_FACTOR_CACHE)))
        hit = base ** np.arange(1, n + 1, dtype=np.float64)
        hit.flags.writeable = False  # guard against accidental in-place edits
        _EXP_FACTOR_CACHE[key] = hit
    return hit


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
            factors = _exp_factors(1.0 - rate, n_samples)
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
        factors = _exp_factors(1.0 - rate, n_samples)
        out = sustain + diff * factors
        self.level = out[-1] if n_samples > 0 else self.level
        if abs(self.level - sustain) < 0.001:
            self.level = sustain
            self.stage = self.SUSTAIN
        return out


# ── C fast paths for the small IIR filters ──────────────────────────────
# scipy.lfilter's per-call overhead on 512-sample blocks, times dozens of
# biquad/one-pole calls per block, measured ~8-10% of the small-Pi render
# budget. jack_bridge.c provides bit-identical DF2T kernels; JackEngine
# attaches them after loading the bridge. None → scipy (Mac / no-bridge).
_BIQUAD_C = None
_ONEPOLE_C = None
_PD = None  # ctypes.POINTER(c_double), set on attach


def attach_filter_bridge(bridge) -> None:
    """Enable C kernels for Biquad*/OnePole* (ctypes CDLL of jack_bridge.so)."""
    global _BIQUAD_C, _ONEPOLE_C, _PD
    import ctypes
    try:
        _PD = ctypes.POINTER(ctypes.c_double)
        bridge.bridge_biquad.restype = None
        bridge.bridge_biquad.argtypes = [_PD, _PD, ctypes.c_int, _PD, _PD, _PD]
        bridge.bridge_onepole.restype = None
        bridge.bridge_onepole.argtypes = [
            _PD, _PD, ctypes.c_int,
            ctypes.c_double, ctypes.c_double, ctypes.c_double, _PD]
        _BIQUAD_C = bridge.bridge_biquad
        _ONEPOLE_C = bridge.bridge_onepole
        logger.info("IIR filters: C fast path (bridge_biquad/bridge_onepole)")
    except AttributeError:
        logger.warning("jack_bridge.so lacks IIR kernels — filters on scipy "
                       "(rebuild the bridge)")
        _BIQUAD_C = _ONEPOLE_C = None


def _biquad_run(flt, samples: np.ndarray) -> np.ndarray:
    """Shared process body for the Biquad* classes. zi updated in place by C.

    ctypes pointer objects are CACHED on the filter instance — building
    them per call (5 casts x ~30 filters x 94 blocks/s) cost nearly as
    much as scipy's overhead did. Safe because b/a/zi are allocated once
    and mutated in place, and each filter owns a persistent out buffer
    (callers consume the result within the block, same contract as the
    Faust wrappers; in==out aliasing is safe in the C loop — x[i] is read
    before y[i] is written)."""
    if _BIQUAD_C is None:
        out, flt.zi = lfilter(flt.b, flt.a, samples, zi=flt.zi)
        return out
    if not samples.flags.c_contiguous:
        samples = np.ascontiguousarray(samples)
    n = samples.shape[0]
    if getattr(flt, "_c_n", 0) != n:
        flt._c_out = np.empty(n, dtype=np.float64)
        flt._c_pout = flt._c_out.ctypes.data_as(_PD)
        flt._c_pb = flt.b.ctypes.data_as(_PD)
        flt._c_pa = flt.a.ctypes.data_as(_PD)
        flt._c_pzi = flt.zi.ctypes.data_as(_PD)
        flt._c_n = n
    _BIQUAD_C(samples.ctypes.data_as(_PD), flt._c_pout, n,
              flt._c_pb, flt._c_pa, flt._c_pzi)
    return flt._c_out


def _onepole_run(flt, a_coeff, samples: np.ndarray) -> np.ndarray:
    """Shared process body for the OnePole6dB* classes. a_coeff is the
    class's denominator array (named _a on lowpass, _a_coeff on highpass).
    Same pointer-caching scheme as _biquad_run; scalar coefficients are
    re-read per call (they're plain floats, set_params mutates arrays)."""
    if _ONEPOLE_C is None:
        out, flt._zi = lfilter(flt._b, a_coeff, samples, zi=flt._zi)
        return out
    if not samples.flags.c_contiguous:
        samples = np.ascontiguousarray(samples)
    n = samples.shape[0]
    if getattr(flt, "_c_n", 0) != n:
        flt._c_out = np.empty(n, dtype=np.float64)
        flt._c_pout = flt._c_out.ctypes.data_as(_PD)
        flt._c_pzi = flt._zi.ctypes.data_as(_PD)
        flt._c_n = n
    b1 = float(flt._b[1]) if flt._b.shape[0] > 1 else 0.0
    _ONEPOLE_C(samples.ctypes.data_as(_PD), flt._c_pout, n,
               float(flt._b[0]), b1, float(a_coeff[1]), flt._c_pzi)
    return flt._c_out


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
        return _onepole_run(self, self._a, samples)

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
        return _onepole_run(self, self._a_coeff, samples)

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
        return _biquad_run(self, samples)

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
        return _biquad_run(self, samples)

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
        return _biquad_run(self, samples)

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
        return _biquad_run(self, samples)

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
    """⚠ FALLBACK PATH — DON'T DELETE. Faust port at `faust/reverb.dsp` + `faust_reverb.py`
    is the default on Pi (selected by `STAVE_FAUST_REVERB=1` in systemd unit). This
    Python class is load-bearing for: (1) Mac port where the .so isn't built,
    (2) `STAVE_FAUST_REVERB=0` env opt-out for A/B testing, (3) crash safety if the
    .so dlopen fails at runtime. See `project_mac_port_corrections.md`.

    Cathedral reverb: early reflections → diffusion → 8-line Hadamard FDN → stereo.

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
            new_fb = min(10.0 ** (-3.0 / (seconds * loops_per_sec)), 0.985)
        else:
            new_fb = 0.0
        # When frozen, don't yank live feedback (we're holding ~0.999) — only
        # update the captured "normal" value so unfreezing later honors the
        # user's new decay knob position. When not frozen, normal-path: write
        # both live and target.
        if getattr(self, "frozen", False):
            self._normal_feedback = new_fb
        else:
            self.feedback = new_fb
            self._feedback_target = new_fb

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


class SamplePlayer:
    """Single-slot WAV playback with attack/release envelope and seamless
    loop crossfade. NO pitch-shift — each pad note has its own native-pitch
    recording. Fully vectorized per block.

    Load via ``load(path)``. Trigger via ``trigger()``; fade out via
    ``release()``. Mix into output with ``process(n, out_l, out_r)``.
    """

    ATTACK_S = 4.0     # 4 s — linear ramp 0→1
    RELEASE_S = 4.0    # 4 s — linear ramp 1→0
    XFADE_MS = 500     # 500 ms — generous loop crossfade hides any seam

    # Pi4 policy bounds, not a measured whole-application RAM guarantee.
    MAX_SLOT_BYTES = 32 * 1024 * 1024
    MAX_SOURCE_BYTES = 64 * 1024 * 1024
    MAX_PREPARE_BYTES = 128 * 1024 * 1024
    MAX_RESAMPLE_FACTOR = 1024
    MIN_FRAMES = 4

    # RISE envelope — drawn on top of normal looping playback.
    # Volume: 0 → 1 over rise_seconds, then quick decay to RISE_SUSTAIN, hold.
    # Filter: log-sweep 200 → 3000 Hz over rise_seconds, then hold.
    # Peak volume is gated by the engine's drone_level (user's VOL slider).
    RISE_CUTOFF_CLOSED = 200.0
    RISE_CUTOFF_OPEN_DEFAULT = 3000.0   # peak cutoff (overridable per trigger)
    RISE_CUTOFF_SUSTAIN = 300.0         # filter falls back to here during volume decay
    RISE_SUSTAIN = 0.55                 # sustain volume after the post-peak decay
    RISE_DECAY_S = 0.8                  # decay from peak (1.0) to RISE_SUSTAIN over 0.8s

    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = int(sample_rate)
        self.samples_l = None   # immutable float32/float64 arrays when loaded
        self.samples_r = None
        self.source_signature = None
        self.last_load_error = None
        self.length = 0
        self.loaded = False
        self.active = False
        self.read_pos = 0.0
        self.env = 0.0
        self.env_target = 0.0
        self.xfade_len = int(self.XFADE_MS * sample_rate / 1000.0)
        # Rise-envelope state.
        # _rise_active: drives the custom env shape (cleared on release so
        #               normal release ramp can take env→0).
        # _rise_filter_engaged: keeps the LP filter running through the release
        #               tail so the spectrum doesn't jump (cleared at deactivate).
        self.rise_seconds = 0.0
        self.rise_cutoff_open = self.RISE_CUTOFF_OPEN_DEFAULT
        self._rise_t_samples = 0
        self._rise_active = False
        self._rise_filter_engaged = False
        self._rise_lp_l = BiquadLowpass(self.rise_cutoff_open, 0.707, self.sample_rate)
        self._rise_lp_r = BiquadLowpass(self.rise_cutoff_open, 0.707, self.sample_rate)

    @property
    def memory_bytes(self) -> int:
        if not self.loaded:
            return 0
        return self.samples_l.nbytes + (0 if self.samples_r is self.samples_l else self.samples_r.nbytes)

    @classmethod
    def _inspect_wave(cls, stream, sample_rate, max_bytes):
        """Read bounded RIFF metadata before allocating any sample payload.

        Supports little-endian mono/stereo PCM8/16/24/32 and IEEE float32/64,
        including their WAVE_FORMAT_EXTENSIBLE subtypes. No silent truncation,
        compressed formats, RF64, or multichannel downmix is performed.
        """
        import math
        import os
        import struct

        stat = os.fstat(stream.fileno())
        if stat.st_size > cls.MAX_SOURCE_BYTES:
            raise ValueError("Pad source exceeds the 64 MiB file limit")
        header = stream.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise ValueError("Pad must be a little-endian RIFF WAVE file")
        end = struct.unpack("<I", header[4:8])[0] + 8
        if end > stat.st_size or end < 12:
            raise ValueError("Truncated or invalid RIFF size")
        fmt = None
        payload = None
        chunks = 0
        while stream.tell() + 8 <= end:
            chunk_id, size = struct.unpack("<4sI", stream.read(8))
            offset = stream.tell()
            if offset + size > end:
                raise ValueError("Truncated WAV chunk")
            chunks += 1
            if chunks > 1024:
                raise ValueError("Too many WAV chunks")
            if chunk_id == b"fmt ":
                if fmt is not None or size < 16:
                    raise ValueError("Invalid or duplicate WAV format chunk")
                fmt = stream.read(min(size, 40))
            elif chunk_id == b"data":
                if payload is not None:
                    raise ValueError("Multiple WAV data chunks are unsupported")
                payload = (offset, size)
            stream.seek(offset + size + (size & 1))
        if fmt is None or payload is None:
            raise ValueError("WAV requires format and data chunks")
        tag, channels, rate, byte_rate, align, bits = struct.unpack("<HHIIHH", fmt[:16])
        if tag == 0xFFFE:
            if len(fmt) < 40 or struct.unpack("<H", fmt[16:18])[0] < 22:
                raise ValueError("Invalid extensible WAV format")
            valid_bits = struct.unpack("<H", fmt[18:20])[0]
            guid_tail = b"\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
            if fmt[28:40] != guid_tail or not 0 < valid_bits <= bits:
                raise ValueError("Unsupported extensible WAV subtype")
            tag = struct.unpack("<I", fmt[24:28])[0]
        if (channels not in (1, 2) or not 8000 <= rate <= 192000
                or not 8000 <= sample_rate <= 192000
                or (tag == 1 and bits not in (8, 16, 24, 32))
                or (tag == 3 and bits not in (32, 64)) or tag not in (1, 3)):
            raise ValueError("Unsupported pad format, channels, or sample rate")
        if align != channels * (bits // 8) or byte_rate != rate * align:
            raise ValueError("Invalid WAV frame alignment or byte rate")
        offset, size = payload
        if size % align:
            raise ValueError("WAV payload ends in a partial frame")
        frames = size // align
        out_frames = (frames * sample_rate + rate - 1) // rate
        if min(frames, out_frames) < cls.MIN_FRAMES:
            raise ValueError("Pad WAV must contain at least 4 complete frames")
        divisor = math.gcd(rate, sample_rate)
        up, down = sample_rate // divisor, rate // divisor
        if max(up, down) > cls.MAX_RESAMPLE_FACTOR:
            raise ValueError("Pad resampling ratio exceeds the preparation bound")
        # Exact float32 storage for native PCM <=24-bit and float32 samples.
        # Keep float64 for PCM32, float64 and resampled audio, preserving the
        # prior normalisation/polyphase precision instead of rounding it.
        dtype = np.float64 if rate != sample_rate or bits == 64 or (tag == 1 and bits == 32) else np.float32
        itemsize = np.dtype(dtype).itemsize
        output_bytes = out_frames * channels * itemsize
        if output_bytes > min(cls.MAX_SLOT_BYTES, max_bytes):
            raise ValueError("Pad decoded audio exceeds the slot or available bank byte budget")
        decode_bytes = frames * channels * itemsize
        # Conservative application-array estimate; SciPy/allocator internals
        # still require target RSS qualification. FIR ratio is separately capped.
        working = size + decode_bytes + output_bytes + 1024 * 1024
        if bits == 24:
            working += frames * channels * 8  # unpack/sign-extension scratch
        if rate != sample_rate:
            working += output_bytes * 2 + max(up, down) * 256
        if working > cls.MAX_PREPARE_BYTES:
            raise ValueError("Pad decoding/resampling exceeds the 128 MiB preparation budget")
        return {"offset": offset, "size": size, "frames": frames, "out_frames": out_frames,
                "channels": channels, "rate": rate, "tag": tag, "bits": bits,
                "dtype": dtype, "up": up, "down": down, "bytes": output_bytes,
                "signature": (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)}

    @classmethod
    def prepare(cls, path, sample_rate=SAMPLE_RATE, max_bytes=None):
        """Return a fresh validated player; never mutate an existing slot."""
        import os
        from pathlib import Path

        sample_rate = int(sample_rate)
        limit = cls.MAX_SLOT_BYTES if max_bytes is None else int(max_bytes)
        with Path(path).open("rb") as stream:
            info = cls._inspect_wave(stream, sample_rate, limit)
            stream.seek(info["offset"])
            payload = stream.read(info["size"])
            stat = os.fstat(stream.fileno())
            signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if len(payload) != info["size"] or signature != info["signature"]:
                raise ValueError("Pad source changed or truncated during preparation")
        bits, dtype = info["bits"], info["dtype"]
        if info["tag"] == 3:
            decoded = np.frombuffer(payload, dtype=f"<f{bits // 8}").astype(dtype)
        elif bits == 24:
            raw = np.frombuffer(payload, dtype=np.uint8).reshape(-1, 3)
            signed = raw[:, 0].astype(np.int32)
            signed |= raw[:, 1].astype(np.int32) << 8
            signed |= raw[:, 2].astype(np.int32) << 16
            signed = (signed ^ 0x800000) - 0x800000
            decoded = signed.astype(dtype)
            decoded /= 8388608.0
            del raw, signed
        else:
            source_dtype = np.uint8 if bits == 8 else f"<i{bits // 8}"
            decoded = np.frombuffer(payload, dtype=source_dtype).astype(dtype)
            if bits == 8:
                decoded -= 128.0
            decoded /= float(1 << (bits - 1))
        del payload
        if not np.isfinite(decoded).all():
            raise ValueError("Pad WAV contains non-finite audio samples")
        decoded = decoded.reshape(info["frames"], info["channels"])
        left = decoded[:, 0]
        right = decoded[:, 1] if info["channels"] == 2 else left
        if info["rate"] != sample_rate:
            # Failure is fatal: never acknowledge a pad playing at wrong pitch.
            from scipy.signal import resample_poly
            left = resample_poly(left, info["up"], info["down"])
            right = resample_poly(right, info["up"], info["down"]) if info["channels"] == 2 else left
        left = np.ascontiguousarray(left, dtype=dtype)
        right = np.ascontiguousarray(right, dtype=dtype) if info["channels"] == 2 else left
        if (left.size != info["out_frames"] or right.size != left.size
                or not np.isfinite(left).all() or not np.isfinite(right).all()):
            raise ValueError("Invalid resampled pad audio")
        left.setflags(write=False)
        right.setflags(write=False)
        player = cls(sample_rate)
        player.samples_l, player.samples_r = left, right
        player.length = left.size
        player.xfade_len = max(1, min(int(cls.XFADE_MS * sample_rate / 1000.0), player.length // 4))
        player.loaded = True
        player.source_signature = info["signature"]
        return player

    def load(self, path) -> bool:
        """Compatibility loader; an invalid replacement leaves playback intact."""
        try:
            prepared = self.prepare(path, self.sample_rate)
        except Exception as exc:
            self.last_load_error = str(exc)
            logger.warning("SamplePlayer.load(%s): %s", path, exc)
            return False
        self.samples_l, self.samples_r = prepared.samples_l, prepared.samples_r
        self.length, self.xfade_len = prepared.length, prepared.xfade_len
        self.source_signature = prepared.source_signature
        self.loaded = True
        self.last_load_error = None
        self.hard_stop()
        return True

    def trigger(self, rise_seconds: float = 0.0,
                rise_cutoff_open: float = None):
        """Start playback (or re-trigger). Resets read position if idle.
        rise_seconds > 0 arms the filter-sweep rise envelope for this trigger.
        rise_cutoff_open sets the peak cutoff the rise sweeps to (defaults
        to RISE_CUTOFF_OPEN_DEFAULT)."""
        if not self.loaded:
            return
        if not self.active:
            self.read_pos = 0.0
            self.env = 0.0
        self.env_target = 1.0
        self.active = True
        self.rise_seconds = max(0.0, float(rise_seconds))
        self.rise_cutoff_open = (float(rise_cutoff_open)
                                  if rise_cutoff_open is not None
                                  else self.RISE_CUTOFF_OPEN_DEFAULT)
        self._rise_t_samples = 0
        self._rise_active = self.rise_seconds > 0.0
        self._rise_filter_engaged = self._rise_active
        if self._rise_active:
            self._rise_lp_l.reset()
            self._rise_lp_r.reset()
            self._rise_lp_l.set_params(self.RISE_CUTOFF_CLOSED, 0.707)
            self._rise_lp_r.set_params(self.RISE_CUTOFF_CLOSED, 0.707)
            # Rise envelope drives volume directly via _rise_t — start from 0.
            self.env = 0.0

    def hard_stop(self):
        """Render-owner emergency reset; retain the loaded WAV for next trigger."""
        self.active = False
        self.env = self.env_target = 0.0
        self.read_pos = 0.0
        self._rise_active = self._rise_filter_engaged = False
        self._rise_t_samples = 0
        self._rise_lp_l.reset()
        self._rise_lp_r.reset()

    def release(self):
        """Begin fade-out. Voice deactivates when envelope hits ~0."""
        self.env_target = 0.0
        # Disengage rise envelope so the standard release ramp can take
        # env→0. Filter stays engaged (_rise_filter_engaged) so the spectrum
        # doesn't snap from muffled-sustain to bright on release — kills click.
        self._rise_active = False

    def process(self, n: int, out_l: np.ndarray, out_r: np.ndarray):
        """Additively mix (enveloped) playback into out_l / out_r. No-op
        when idle."""
        if n <= 0 or not self.active or not self.loaded:
            return
        sr = self.sample_rate
        # Envelope — when rise mode is active and not released, drive env
        # from a custom shape (rise → quick decay → sustain) instead of the
        # standard linear AR. After release, _rise_active is cleared and the
        # standard release ramp takes over from the current env value.
        if self._rise_active and self.env_target > 0:
            t_start = self._rise_t_samples / float(sr)
            t_end = (self._rise_t_samples + n) / float(sr)

            def _rise_env_at(t):
                rise_s = self.rise_seconds
                if t < rise_s:
                    return t / rise_s
                decay_end = rise_s + self.RISE_DECAY_S
                if t < decay_end:
                    p = (t - rise_s) / self.RISE_DECAY_S
                    return 1.0 - (1.0 - self.RISE_SUSTAIN) * p
                return self.RISE_SUSTAIN

            env_start = _rise_env_at(t_start)
            new_env = _rise_env_at(t_end)
            self._rise_t_samples += n
        else:
            if self.env_target > self.env:
                step = n / (self.ATTACK_S * sr)
                new_env = min(self.env_target, self.env + step)
                env_start = self.env
            elif self.env_target < self.env:
                step = n / (self.RELEASE_S * sr)
                new_env = max(self.env_target, self.env - step)
                env_start = self.env
            else:
                new_env = self.env
                env_start = self.env
            if self.env_target < 0.001 and new_env <= 0.0001:
                self.active = False
                self.env = 0.0
                self._rise_active = False
                self._rise_filter_engaged = False
                return
        env_ramp = np.linspace(env_start, new_env, n, dtype=np.float64)
        self.env = new_env

        # Read positions (speed = 1.0 by design). Playback goes 0..length-1 on
        # the first pass; when a position would pass `length`, we wrap by
        # subtracting loop_size (= length - xfade_len). That puts the next
        # iteration at position xfade_len, so samples [0..xfade_len) are only
        # heard during the crossfade's fade-in — never doubled.
        loop_size = max(1, self.length - self.xfade_len)
        raw_positions = self.read_pos + np.arange(n, dtype=np.float64)
        positions = raw_positions.copy()
        # Preserve the first pass, then wrap any number of times. Short but
        # valid samples can loop many times within one render block.
        mask = positions >= self.length
        positions[mask] = self.xfade_len + (positions[mask] - self.length) % loop_size
        idx = np.clip(positions.astype(np.int64), 0, self.length - 1)

        primary_l = self.samples_l[idx]
        primary_r = self.samples_r[idx]

        # Crossfade zone: last xfade_len samples blend with sample head. Primary
        # fades out, secondary (the head) fades in — equal-power (cos/sin).
        xfade_start = self.length - self.xfade_len
        in_xf = idx >= xfade_start
        if np.any(in_xf):
            sec_idx = np.clip(idx - xfade_start, 0, self.length - 1)
            t = np.zeros(n, dtype=np.float64)
            t[in_xf] = (idx[in_xf] - xfade_start) / float(self.xfade_len)
            angle = t * (np.pi * 0.5)
            fade_out = np.cos(angle)
            fade_in = np.sin(angle)
            sec_l = self.samples_l[sec_idx]
            sec_r = self.samples_r[sec_idx]
            out_l_block = np.where(in_xf,
                                   primary_l * fade_out + sec_l * fade_in,
                                   primary_l)
            out_r_block = np.where(in_xf,
                                   primary_r * fade_out + sec_r * fade_in,
                                   primary_r)
        else:
            out_l_block = primary_l
            out_r_block = primary_r

        # RISE filter sweep — log-lerp cutoff CLOSED → OPEN over rise_seconds,
        # then mirrors the volume decay back down to CUTOFF_SUSTAIN, holds.
        # Stays engaged through release tail so the spectrum doesn't snap on
        # release; only disengages when the voice fully deactivates.
        if self._rise_filter_engaged:
            t_sec = max(0.0, (self._rise_t_samples - n) / float(sr))
            rise_s = self.rise_seconds
            open_hz = self.rise_cutoff_open
            if t_sec < rise_s:
                p = t_sec / rise_s
                cutoff = self.RISE_CUTOFF_CLOSED * (
                    (open_hz / self.RISE_CUTOFF_CLOSED) ** p
                )
            elif t_sec < rise_s + self.RISE_DECAY_S:
                p = (t_sec - rise_s) / self.RISE_DECAY_S
                cutoff = open_hz * (
                    (self.RISE_CUTOFF_SUSTAIN / open_hz) ** p
                )
            else:
                cutoff = self.RISE_CUTOFF_SUSTAIN
            self._rise_lp_l.set_params(cutoff, 0.707)
            self._rise_lp_r.set_params(cutoff, 0.707)
            out_l_block = self._rise_lp_l.process(out_l_block)
            out_r_block = self._rise_lp_r.process(out_r_block)

        out_l += out_l_block * env_ramp
        out_r += out_r_block * env_ramp
        # Advance: next block's first position = last position + 1 (speed=1),
        # wrapped through loop_size exactly like the per-sample wrap above.
        next_pos = positions[-1] + 1.0
        if next_pos >= self.length:
            next_pos -= loop_size
        self.read_pos = next_pos

    def reset(self):
        self.active = False
        self.read_pos = 0.0
        self.env = 0.0
        self.env_target = 0.0


class BusCompressor:
    """⚠ FALLBACK PATH — DON'T DELETE. Even on Pi (Faust default), the Faust bus_comp
    only handles `bus_comp_source = "self"`. Sources `piano`, `lfo`, `bpm` ALWAYS
    route through this Python class (see `jack_engine.py:277-278`). Also the Mac
    port + `STAVE_FAUST_BUS_COMP=0` opt-out rely on it. See
    `project_mac_port_corrections.md`.

    SSL G-style stereo bus compressor.

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

        # Sidechain is MONO — process() only runs _hpf_l on the summed L+R
        # detector signal. _hpf_r never processes audio but is kept because
        # main.py's sidechain_hpf handler calls set_params on both filters.
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
        # Soft knee quadratic: slope·(over + knee/2)²/(2·knee) — the half
        # factor makes this meet the over-knee line exactly at over=+knee/2
        # (both give slope·knee/2). Matches the piano LA-2A comp and the
        # Faust bus_comp.dsp.
        x = (over + knee * 0.5) / knee  # 0..1
        return x * x * knee * 0.5 * (1.0 - 1.0 / ratio)

    def process(self, out_l: np.ndarray, out_r: np.ndarray,
                sc_l: np.ndarray = None, sc_r: np.ndarray = None,
                skip_hpf: bool = False):
        """Process stereo in-place. sc_l/sc_r: external sidechain; if None, uses
        bus signal (feedback-ish approximation using current input as detection
        source). skip_hpf: bypass sidechain HPF (for synthetic sources like BPM
        pulse train that are already clean). No-ops if disabled."""
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
        # Skipped for BPM sidechain: synthetic pulse train has all energy below
        # ~50 Hz and the HPF would strip most of it, weakening the pump.
        if not skip_hpf:
            sc = self._hpf_l.process(sc)

        # ── RMS envelope (one-pole on squared signal) ──
        sq = sc * sc
        a = self._rms_alpha
        rms, self._rms_state = lfilter([a], [1.0, -(1.0 - a)], sq, zi=self._rms_state)
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
    # Per-OSC envelopes — OSC1 and OSC2 can have independent ADSR shapes.
    # Triggered + released together; stage transitions diverge based on each
    # config's timing. Voice stays alive while either envelope is active.
    adsr_osc1: ADSREnvelope = field(default_factory=lambda: ADSREnvelope(ADSRConfig()))
    adsr_osc2: ADSREnvelope = field(default_factory=lambda: ADSREnvelope(ADSRConfig()))
    phases: list = field(default_factory=list)       # base phase per unison voice
    osc1_phases: list = field(default_factory=list)  # osc1 phase (with octave baked in)
    osc2_phases: list = field(default_factory=list)  # osc2 phase (with octave baked in)
    shimmer_phases: list = field(default_factory=list)  # shimmer phase (octave up)
    age: int = 0
    faust_slot: int = -1  # index in FaustOscBank (0..15), -1 = unassigned / Python path
    # Analog-warmth pitch drift: random-walk value in [-1, +1]. Scaled by
    # SynthEngine.analog_drift_cents at render time. Uncorrelated per voice
    # so a chord has the classic slight-detune analog quality.
    drift_val: float = 0.0
    # LAYER (split): per-source weights latched at note_on. Multiplied into
    # each source's gate every render block; 1.0 = full, 0.0 = silenced for
    # this voice. Value reflects the note's position in the source's key
    # range (smoothstep interpolation across xfade tails).
    osc1_weight: float = 1.0
    osc2_weight: float = 1.0
    shimmer_weight: float = 1.0

    def is_active(self):
        return self.adsr_osc1.is_active() or self.adsr_osc2.is_active()


class SynthEngine:
    """Main synth pad engine managing voices, oscillators, and effects."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, max_voices: int = MAX_VOICES):
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

        # Per-OSC ADSR configs. Voices create their own ADSREnvelope instances
        # from these; live config edits propagate to in-flight voices via the
        # shared config reference.
        self.adsr_osc1_config = ADSRConfig()
        self.adsr_osc2_config = ADSRConfig()

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
        # ── Analog warmth ──
        # Per-voice pitch drift: each voice's pitch wanders on a slow random
        # walk around its nominal note. analog_drift_cents caps the peak
        # deviation in cents (100 cents = one semitone). Subtle by default;
        # 0 = perfectly tuned digital behavior.
        self.analog_drift_cents = 3.0
        # Global filter drift: slow wander on the shared filter cutoff to
        # emulate thermal drift of an analog lowpass stage. Value is the
        # peak deviation in cents of a pitch-like log-domain offset applied
        # to cutoff_hz; ±20 cents ≈ ±1.2% cutoff movement.
        self.filter_drift_cents = 2.0
        self._filter_drift_val = 0.0
        # Filter self-resonance wobble: gated on resonance (silent below
        # 0.5). At amount=1 adds up to ±3% cutoff wander when resonance is
        # pushed near self-oscillation — the "unstable" analog character.
        self.filter_wobble_amount = 0.0
        self._filter_wobble_val = 0.0
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
        if USE_FAUST_REVERB:
            try:
                from .faust_reverb import FaustReverb
                self.reverb = FaustReverb(6.0, sample_rate)
                logger.info("reverb: Faust native path (STAVE_FAUST_REVERB=1)")
            except Exception as e:
                logger.warning("reverb: Faust init failed (%s); falling back to numpy", e)
                self.reverb = FeedbackDelayReverb(6.0, sample_rate)
        else:
            self.reverb = FeedbackDelayReverb(6.0, sample_rate)
        self._dry_wet_cur = 0.75  # smoothed dry_wet tracking
        self.reverb_filter_enabled = False  # route reverb wet through main filter
        self.reverb_filter_l = BiquadLowpass(8000.0, 0.707, sample_rate)
        self.reverb_filter_r = BiquadLowpass(8000.0, 0.707, sample_rate)
        self.reverb_filter2_l = BiquadLowpass(8000.0, 0.707, sample_rate)
        self.reverb_filter2_r = BiquadLowpass(8000.0, 0.707, sample_rate)

        # Per-OSC reverb send + fx-bypass state. Default sends=1.0 preserves
        # the original behavior (reverb_in tracks the full pad). When sends
        # differ from 1, a dedicated reverb-path filter (`_rev_send_filter_*`)
        # processes a weighted sum of pre-filter osc signals so the reverb
        # tail still matches the master filter cutoff.
        self.osc1_reverb_send = 1.0
        self.osc2_reverb_send = 1.0
        self.osc1_fx_bypass = False
        self.osc2_fx_bypass = False
        # Per-OSC LFO receive routing (amp/pan targets only)
        self.osc1_recv_lfo1 = True
        self.osc1_recv_lfo2 = True
        self.osc2_recv_lfo1 = True
        self.osc2_recv_lfo2 = True
        self._rev_send_filter_l = BiquadLowpass(8000.0, 0.707, sample_rate)
        self._rev_send_filter_r = BiquadLowpass(8000.0, 0.707, sample_rate)
        self._rev_send_filter2_l = BiquadLowpass(8000.0, 0.707, sample_rate)
        self._rev_send_filter2_r = BiquadLowpass(8000.0, 0.707, sample_rate)

        # Synthesized shimmer: octave-up sines fed into reverb input
        self.shimmer_enabled = False
        self.shimmer_mix = 0.5
        self.shimmer_high = False  # False = +12 (2x), True = +24 (4x)
        self.shimmer_send = 1.0    # CLOUD knob: wet level of pre-reverb multi-tap bouncing delay
        self._shimmer_mix_cur = 0.5
        # Shimmer low-cut — keeps sparkle in the top half while letting
        # shimmer fundamentals through. Cutoff depends on shimmer_high:
        # +24 mode (4×) → 1200 Hz (the original sparkle-HP).
        # +12 mode (2×) → 400 Hz so C3/C4-range shimmer actually sounds.
        # Previously fixed at 1200 Hz, which killed +12 shimmer below A4.
        self._shimmer_hp = BiquadHighpass(1200.0, 0.707, sample_rate)
        self._shimmer_hp_high_last = True  # tracks which cutoff is current

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

        # Sympathetic resonance: piano notes reinforce the pad subtly
        self.sympathetic_enabled = False
        self.sympathetic_level = 0.035
        self._sympathetic_level_cur = 0.035
        self._sympathetic_suppress = False  # set during preset crossfade to silence resonance
        self._sympathetic_state = {}  # note -> {phase_l, phase_r, gain, target, faust_slot?}
        self._faust_sympathetic = None
        self._faust_symp_slot_free: list[int] = []
        if USE_FAUST_SYMPATHETIC:
            try:
                from .faust_sympathetic import FaustSympathetic, N_SLOTS as _SN
                self._faust_sympathetic = FaustSympathetic(sample_rate)
                self._faust_symp_slot_free = list(range(_SN))
                logger.info("sympathetic: Faust native path (STAVE_FAUST_SYMPATHETIC=1)")
            except Exception as e:
                logger.warning("sympathetic: Faust init failed (%s); falling back", e)
        # Pre-alloc scratch for the Python-fallback sympathetic render — avoids
        # 5× np.empty(N) per block on the audio thread under sustained chord work.
        # 64 covers the Faust-FIFO cap of 24 plus margin for the rare unbounded
        # Python-only path (no FIFO eviction). 5×64×8B = 2.5 KB total.
        _SYM_MAX = 64
        self._sym_freqs = np.empty(_SYM_MAX, dtype=np.float64)
        self._sym_tgts = np.empty(_SYM_MAX, dtype=np.float64)
        self._sym_g0s = np.empty(_SYM_MAX, dtype=np.float64)
        self._sym_ph_l_init = np.empty(_SYM_MAX, dtype=np.float64)
        self._sym_ph_r_init = np.empty(_SYM_MAX, dtype=np.float64)

        # Pad-player volume + master-fade. Field names start with "drone_"
        # for legacy compatibility with state files and main.py's FADE-button
        # handler — they actually drive the pad-sample bus now (chord-drone
        # synthesis was decommissioned 2026-04-26).
        self.drone_enabled = False       # pad-player on/off (read by main.py UI)
        self.drone_level = 1.0           # pad-player volume multiplier
        self._drone_fade_scale = 1.0     # 0..1, ramped by FADE button
        # Pad sample library: MIDI note (60..71) → SamplePlayer if a WAV exists
        # in ~/.local/share/stave-synth/pad_samples/. Populated by load_pad_samples().
        self._pad_samples: dict = {}
        self._pad_samples_dir = None   # set by load_pad_samples()
        # Mellow filter — global LP applied to combined pad-sample bus when
        # a recorded sample sounds too bright. Bypassed when disabled.
        self.pad_mellow_enabled = False
        self.pad_mellow_cutoff_hz = 400.0
        self._pad_mellow_lp_l = BiquadLowpass(self.pad_mellow_cutoff_hz, 0.707, sample_rate)
        self._pad_mellow_lp_r = BiquadLowpass(self.pad_mellow_cutoff_hz, 0.707, sample_rate)
        # Coefficient cache — skip set_params in render when cutoff unchanged
        # (same pattern as the main filter's slope cache).
        self._pad_mellow_last_cutoff = self.pad_mellow_cutoff_hz

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
        self.delay_feedback = 0.35     # 0..0.99 (slider); oblivion overrides to 1.0
        self.delay_oblivion = False    # mirror-in-mirror infinite hold
        # Rate multiplier mirrors LFO's pattern. Sign carries polarity:
        # negative value = polarity-inverted feedback (comb-filter character).
        self.delay_rate_multiplier = 1.0
        self.delay_wet = 0.0           # 0..1 (dry always passes through)
        # Feedback-path tone shaping + character (only in Faust path).
        self.delay_low_cut_hz = 20.0       # 20..1000 Hz
        self.delay_high_cut_hz = 18000.0   # 500..20000 Hz
        self.delay_drive = 0.0             # 0..1 tanh saturation in feedback
        self.delay_width = 1.0             # 0 = mono echo per side, 1 = full ping-pong
        self.delay_mod_rate_hz = 0.5       # 0.05..8 Hz BBD wobble
        self.delay_mod_depth_ms = 0.0      # 0..15 ms fractional read offset
        # Reverse playback (parallel to ping-pong, additive into wet output)
        self.delay_reverse_amount = 0.0       # 0..1 — wet contribution from reversed input
        self.delay_reverse_window_ms = 500.0  # 50..3000 ms chunk size
        self.delay_reverse_window_mode = "FREE"  # "FREE" or subdivision string
        self.delay_reverse_feedback = 0.0     # 0..0.7 recursive feedback into reverse buffer
        self.delay_aurora_enabled = False     # AURORA = long-window rise mode
        self.delay_aurora_seconds = 5.0       # 3..15 sec rise length when AURORA on
        self.bpm = 120.0
        _delay_max_ms = 1000.0          # generous headroom; will clamp read offset
        _delay_buf_len = int((_delay_max_ms / 1000.0) * sample_rate)
        self._delay_buf_l = np.zeros(_delay_buf_len, dtype=np.float64)
        self._delay_buf_r = np.zeros(_delay_buf_len, dtype=np.float64)
        self._delay_buf_len = _delay_buf_len
        self._delay_write_pos = 0
        self._faust_ping_pong = None
        if USE_FAUST_PING_PONG:
            try:
                from .faust_ping_pong import FaustPingPong
                self._faust_ping_pong = FaustPingPong(sample_rate)
                logger.info("ping-pong: Faust native path (STAVE_FAUST_PING_PONG=1)")
            except Exception as e:
                logger.warning("ping-pong: Faust init failed (%s); falling back to numpy", e)

        # ═══ LFO (Motion tab) ═══
        # Single control-rate LFO. Cheap because it updates once per block, not per sample.
        # Targets: filter / amp / pan / sidechain. Depth is the sole gate — when
        # depth × motion_mix < 0.001 the LFO is idle.
        self.lfo_rate_hz = 1.0         # 0.05-20 Hz (used when lfo_rate_mode == "FREE")
        self.lfo_rate_mode = "FREE"    # "FREE" or subdivision ("1/4", "1/8", "1/8T", ...)
        self.lfo_rate_multiplier = 1.0  # tempo-sync rate multiplier: >1 = faster, <1 = slower
        self.lfo_depth = 0.0           # 0..1
        self.lfo_active = True         # master gate; multiplies depth by 0 when off
        self.lfo_shape = "sine"        # sine, triangle, square, sh
        self.lfo_target = "filter"     # filter, amp, pan
        self.lfo_spread = 0.0          # 0..1, 180° R/L offset at max
        self.lfo_key_sync = False      # reset LFO phase on every note_on for repeatable sweeps
        self.lfo_invert = False        # flip output polarity (peak becomes trough)
        # Phase offset expressed in absolute milliseconds — translates to a phase
        # fraction at eval time based on the effective rate. Bipolar ±200ms so
        # user can align LFO timing with Haas-style delays intuitively. Does NOT
        # mirror via LINK — lets linked LFOs sit counter-phase.
        self.lfo_offset_ms = 0.0
        # When on, the current haas_delay_ms is added to the effective offset at
        # render time. Lets LFO follow Haas changes live without manual re-sync.
        self.lfo_haas_compensate = False
        self._lfo_phase = 0.0
        self._lfo_sh_value = 0.0       # current sample&hold held value
        self._lfo_sh_value_r = 0.0
        self._lfo_prev_phase = 0.0     # to detect wraps for S&H
        self._lfo_mod_a_last = 0.0     # previous block's end mod value (for per-sample ramp)
        self._lfo_mod_b_last = 0.0
        # Smoothness — one-pole LP applied to the per-sample LFO mod ramp.
        # Tames sideband content (AM artifacts) when the LFO runs fast on
        # bright source material. 0 = no smoothing.
        self.lfo_smooth = 0.0
        self._lfo_smooth_a_state = 0.0
        self._lfo_smooth_b_state = 0.0
        # Polyphonic mode — when on AND the Faust osc_bank is active, each
        # voice gets its own LFO phase (random at note_on) so amp mod across
        # held notes is decorrelated, not lockstep. Pan/filter targets stay
        # mono on this path; AMP target only.
        self.lfo_poly = False

        # LFO 2 — independent second modulator. Can target filter/amp/pan same
        # as LFO 1; outputs stack additively at each target. Keeps its own
        # phase + tempo-sync + key-sync state so the two LFOs don't interlock.
        self.lfo2_rate_hz = 1.0
        self.lfo2_rate_mode = "FREE"
        self.lfo2_rate_multiplier = 1.0
        self.lfo2_depth = 0.0
        self.lfo2_active = True
        self.lfo2_shape = "sine"
        self.lfo2_target = "pan"       # default pan so a "second LFO on" feels different from LFO 1
        self.lfo2_spread = 0.0
        self.lfo2_key_sync = False
        self.lfo2_invert = False
        self.lfo2_offset_ms = 0.0
        self.lfo2_haas_compensate = False
        self._lfo2_phase = 0.0
        self._lfo2_sh_value = 0.0
        self._lfo2_sh_value_r = 0.0
        self._lfo2_mod_a_last = 0.0
        self._lfo2_mod_b_last = 0.0
        self.lfo2_smooth = 0.0
        self._lfo2_smooth_a_state = 0.0
        self._lfo2_smooth_b_state = 0.0
        self.lfo2_poly = False

        self.voices: list[Voice] = []
        self._age_counter = 0
        # ── Voice pool ──
        # Pre-allocate Voice instances (and their ADSR envelopes) at init
        # rather than constructing them on every note_on. Allocations in the
        # audio-facing render path are the classic cause of Python GC pauses
        # blocking the next block. Pool is kept on `_voice_pool` as a stack
        # of free instances; note_on pops, dead-voice cleanup + voice stealing
        # push back. `voices` still holds the active subset like before.
        # The ADSR holds a reference to the engine's shared config, so live
        # config edits continue to apply to pooled voices automatically.
        self._voice_pool: list[Voice] = [
            Voice(
                adsr_osc1=ADSREnvelope(self.adsr_osc1_config, sample_rate),
                adsr_osc2=ADSREnvelope(self.adsr_osc2_config, sample_rate),
            )
            for _ in range(max_voices)
        ]

        # Faust 24-voice oscillator bank (opt-in via STAVE_FAUST_OSC_BANK=1).
        # Python still owns voice allocation + ADSR + shimmer + Haas; Faust
        # owns only wave gen + unison + per-osc pan + blend for all 24 voices.
        self._faust_osc_bank = None
        self._faust_slot_free: list[int] = []
        self._faust_nvoices = 0  # populated below if Faust path activates

        # Pitch bend (MIDI 0xE0). _semitones is the live offset applied to
        # base_freq in render. Range default ±2 (industry standard); user can
        # widen later. Pad-only — piano/organ get pitch bend via their own
        # paths (FluidSynth native handler / Faust organ).
        self._pitch_bend_semitones = 0.0
        self._pitch_bend_range = 2.0

        # LAYER (split) — per-source MIDI ranges with crossfade tails. When
        # split_enabled is False, compute_split_weights returns all 1.0 (no
        # gate) so the engine behaves identically to pre-split state.
        self.split_enabled = False
        self.osc1_split_low = 0;     self.osc1_split_high = 127;    self.osc1_split_xfade = 0
        self.osc2_split_low = 0;     self.osc2_split_high = 127;    self.osc2_split_xfade = 0
        self.shimmer_split_low = 0;  self.shimmer_split_high = 127; self.shimmer_split_xfade = 0

        # Render serializes against control-thread mutators (note_on/off,
        # all_notes_off, multi-step transactions in update_params, panic).
        # RLock so re-entry (e.g. panic-from-render) is safe. Held for ~5ms
        # per render block — control-thread waits at most one block.
        self._render_lock = threading.RLock()
        # Panic is requested from any thread but executed at the top of the
        # next render block (single thread of mutation). request_panic()
        # is the public hook; _apply_panic() does the work.
        self._panic_pending = False
        if USE_FAUST_OSC_BANK:
            try:
                from .faust_osc_bank import FaustOscBank, NVOICES as _FN
                self._faust_osc_bank = FaustOscBank(sample_rate)
                self._faust_slot_free = list(range(_FN))
                self._faust_nvoices = _FN
                logger.info("osc bank: Faust native path (STAVE_FAUST_OSC_BANK=1)")
                if self.unison_voices != 3:
                    logger.warning("osc bank: Faust iteration only supports unison=3; "
                                   "current unison_voices=%d will mismatch", self.unison_voices)
            except Exception as e:
                logger.warning("osc bank: Faust init failed (%s); falling back to numpy", e)

        # Phase 1+2 render-skeleton port (FAUST_PORT_PLAN.md): Haas + filter
        # routing + shared/indep lowpass + comp + highpass + reverb-send
        # filter path + the shimmer chain (HP split, mix, CLOUD multi-tap)
        # as one Faust module. Engages per block only when the Faust osc
        # bank produced the 5-channel block; the Python skeleton below
        # stays byte-identical when the flag is off.
        self._faust_pad_bus = None
        if USE_FAUST_PAD_BUS:
            try:
                from .faust_pad_bus import FaustPadBus
                self._faust_pad_bus = FaustPadBus(sample_rate)
                logger.info("pad bus: Faust native path (STAVE_FAUST_PAD_BUS=1)")
                if self._faust_osc_bank is None:
                    logger.warning(
                        "pad bus: engages only on Faust-osc-bank blocks — "
                        "enable STAVE_FAUST_OSC_BANK too or it stays idle")
            except Exception as e:
                logger.warning("pad bus: Faust init failed (%s); falling back to numpy", e)

        # Phase 4 (FAUST_PORT_PLAN.md): ONE C call per block fuses the osc
        # bank compute, the f32→f64 widen and the pad-bus compute — kills
        # the Python buffer shuttle between the two islands — and unlocks
        # the in-Faust LFO amp/pan block ramps (fast-path blocks only).
        self._faust_merged = None
        if USE_FAUST_MERGED:
            if self._faust_pad_bus is None or self._faust_osc_bank is None:
                logger.warning(
                    "merged shim: STAVE_FAUST_MERGED needs STAVE_FAUST_OSC_BANK "
                    "and STAVE_FAUST_PAD_BUS both active — staying on the "
                    "two-call path")
            else:
                try:
                    from .faust_merged import FaustMergedShim
                    self._faust_merged = FaustMergedShim(
                        self._faust_osc_bank, self._faust_pad_bus)
                    logger.info("merged shim: single-call osc_bank→pad_bus "
                                "path (STAVE_FAUST_MERGED=1)")
                except Exception as e:
                    logger.warning(
                        "merged shim: init failed (%s); two-call path", e)

        self._sample_indices = np.arange(1, 513, dtype=np.float64)

        # Pre-allocated render buffers — avoids per-block allocation/GC jitter
        self._buf_size = 512  # resized if needed
        self._filter_buf = np.zeros((2, self._buf_size), dtype=np.float64)
        self._osc1_indep_buf = np.zeros((2, self._buf_size), dtype=np.float64)
        self._osc2_indep_buf = np.zeros((2, self._buf_size), dtype=np.float64)
        # Per-OSC pre-filter stereo accumulators for the reverb-send tap.
        self._osc1_pre_l = np.zeros(self._buf_size, dtype=np.float64)
        self._osc1_pre_r = np.zeros(self._buf_size, dtype=np.float64)
        self._osc2_pre_l = np.zeros(self._buf_size, dtype=np.float64)
        self._osc2_pre_r = np.zeros(self._buf_size, dtype=np.float64)
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

        # Render-path scratch buffers (Tier-1 perf cleanup) — previously
        # hasattr-lazy-allocated on first use inside the audio thread, or
        # `np.empty(...)` allocated every block. Pre-allocating both here
        # AND in _ensure_buffers (in case block size grows past 512) keeps
        # the render block free of malloc.
        self._dry_snap_l     = np.zeros(self._buf_size, dtype=np.float64)
        self._dry_snap_r     = np.zeros(self._buf_size, dtype=np.float64)
        self._bypass_snap_l  = np.zeros(self._buf_size, dtype=np.float64)
        self._bypass_snap_r  = np.zeros(self._buf_size, dtype=np.float64)
        self._pad_sample_l   = np.zeros(self._buf_size, dtype=np.float64)
        self._pad_sample_r   = np.zeros(self._buf_size, dtype=np.float64)
        self._lfo_ramp_a1    = np.zeros(self._buf_size, dtype=np.float64)
        self._lfo_ramp_b1    = np.zeros(self._buf_size, dtype=np.float64)
        self._lfo_ramp_a2    = np.zeros(self._buf_size, dtype=np.float64)
        self._lfo_ramp_b2    = np.zeros(self._buf_size, dtype=np.float64)
        # Cached [0..1] t-axis for linspace replacement. Built lazily on first
        # use when block size becomes known. Slicing a fixed-length cache
        # gives WRONG values (denominator is fixed), so we (re)build per n.
        self._linspace_t     = None
        self._linspace_t_n   = -1
        self._dry_bus_scratch = np.zeros((2, self._buf_size), dtype=np.float64)
        self._fx_bus_scratch  = np.zeros((2, self._buf_size), dtype=np.float64)
        self._smooth_b = np.zeros(1, dtype=np.float64)
        self._smooth_a = np.zeros(2, dtype=np.float64); self._smooth_a[0] = 1.0
        self._smooth_zi = np.zeros(1, dtype=np.float64)

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
            self._osc1_pre_l = np.zeros(n_samples, dtype=np.float64)
            self._osc1_pre_r = np.zeros(n_samples, dtype=np.float64)
            self._osc2_pre_l = np.zeros(n_samples, dtype=np.float64)
            self._osc2_pre_r = np.zeros(n_samples, dtype=np.float64)
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
            # Render-path scratch buffers — previously hasattr-lazy-allocated on
            # first use inside the audio thread. Pre-allocating here keeps the
            # render block free of malloc(), which idle GC otherwise has to
            # sweep up. See review #8 / Tier-1 perf cleanup.
            self._dry_snap_l     = np.zeros(n_samples, dtype=np.float64)
            self._dry_snap_r     = np.zeros(n_samples, dtype=np.float64)
            self._bypass_snap_l  = np.zeros(n_samples, dtype=np.float64)
            self._bypass_snap_r  = np.zeros(n_samples, dtype=np.float64)
            self._pad_sample_l   = np.zeros(n_samples, dtype=np.float64)
            self._pad_sample_r   = np.zeros(n_samples, dtype=np.float64)
            # LFO ramp scratches: 2 LFOs × 2 channels (a/b) used to be 4-8
            # per-block np.linspace allocs. We now compute the ramp in-place
            # via `t * (b-a) + a` against a cached t = arange(n)/(n-1).
            self._lfo_ramp_a1    = np.zeros(n_samples, dtype=np.float64)
            self._lfo_ramp_b1    = np.zeros(n_samples, dtype=np.float64)
            self._lfo_ramp_a2    = np.zeros(n_samples, dtype=np.float64)
            self._lfo_ramp_b2    = np.zeros(n_samples, dtype=np.float64)
            # _linspace_t is rebuilt lazily by _fill_ramp() since the
            # denominator depends on the actual block size in use, not the
            # max-allocation size.
            # FX-bypass split-path bus scratches (used when bus_comp_fx_bypass=True).
            self._dry_bus_scratch = np.zeros((2, n_samples), dtype=np.float64)
            self._fx_bus_scratch  = np.zeros((2, n_samples), dtype=np.float64)
            # _smooth_one_pole biquad coefs — 2 floats each, scalar at runtime
            # but as 1-element arrays so scipy.lfilter accepts them. Reused
            # across all 4 LFO ramps per block.
            self._smooth_b = np.zeros(1, dtype=np.float64)
            self._smooth_a = np.zeros(2, dtype=np.float64)
            self._smooth_a[0] = 1.0
            self._smooth_zi = np.zeros(1, dtype=np.float64)

    def note_on(self, note: int, velocity: float = 1.0,
                osc1_weight: float = 1.0, osc2_weight: float = 1.0,
                shimmer_weight: float = 1.0):
        """Trigger a pad voice. Per-source weights default to 1.0 so callers
        that don't know about LAYER (split) get unchanged behavior. With
        split on, jack_engine passes computed weights from compute_split_weights."""
        with self._render_lock:
            self._note_on_locked(note, velocity, osc1_weight, osc2_weight, shimmer_weight)

    def _note_on_locked(self, note: int, velocity: float,
                        osc1_weight: float = 1.0, osc2_weight: float = 1.0,
                        shimmer_weight: float = 1.0):
        # Key-synced LFOs: each independently resets phase on note_on so
        # modulation sweeps are reproducible per note. Matches classic
        # analog-synth behavior.
        # Phase resets to 0 for reproducible sweeps, but DON'T zero _lfo_mod_*_last —
        # the per-sample linspace ramp (see _osc_amp_pan) uses it as its smooth
        # anchor; zeroing it here re-introduces the click we paid the ramp to avoid.
        if self.lfo_key_sync:
            self._lfo_phase = 0.0
            self._lfo_sh_value = 0.0
            self._lfo_sh_value_r = 0.0
        if self.lfo2_key_sync:
            self._lfo2_phase = 0.0
            self._lfo2_sh_value = 0.0
            self._lfo2_sh_value_r = 0.0

        for v in self.voices:
            # "Still held" = neither envelope has been user-released. release()
            # is called on both simultaneously at note_off, so checking OSC1 is
            # sufficient but we check both for safety.
            if v.note == note and v.adsr_osc1.stage != ADSREnvelope.RELEASE and v.adsr_osc2.stage != ADSREnvelope.RELEASE:
                v.adsr_osc1.trigger()
                v.adsr_osc2.trigger()
                v.velocity = velocity
                v.age = self._age_counter
                self._age_counter += 1
                return

        stolen_slot = -1
        if len(self.voices) >= self.max_voices:
            # Voice stealing: prefer an already-releasing voice (quietest),
            # fall back to oldest. Then trigger release + drop immediately.
            # For the fallback-oldest case, set ADSR level to 0 before removal
            # so the freed voice buffer won't cause a waveform discontinuity
            # when its slot gets re-used by the new voice below.
            releasing = [v for v in self.voices if v.adsr_osc1.stage == ADSREnvelope.RELEASE]
            if releasing:
                victim = min(releasing, key=lambda v: max(v.adsr_osc1.level, v.adsr_osc2.level))
            else:
                victim = min(self.voices, key=lambda v: v.age)
                victim.adsr_osc1.level = 0.0    # zero so next block starts cleanly
                victim.adsr_osc2.level = 0.0
            victim.adsr_osc1.stage = ADSREnvelope.OFF
            victim.adsr_osc2.stage = ADSREnvelope.OFF
            # Transfer Faust slot to the replacement voice so the slot's phase
            # accumulator keeps flowing without click (new freq is written
            # first thing in render's Faust path). Clear gate immediately —
            # otherwise the slot rings with the victim's old freq+gate for
            # half a block before the next set_voice overwrites it (chirp).
            stolen_slot = victim.faust_slot
            victim.faust_slot = -1
            if stolen_slot >= 0 and self._faust_osc_bank is not None:
                self._faust_osc_bank.clear_voice(stolen_slot)
            self.voices.remove(victim)
            self._voice_pool.append(victim)

        # Grab a recycled Voice from the pool. Pool is sized to max_voices
        # and steal-path always returns one first, so this should never miss.
        if not self._voice_pool:
            logger.warning("voice pool empty at note_on; dropping note %d", note)
            return
        voice = self._voice_pool.pop()

        # Reset voice state for reuse. ADSR config reference stays bound to
        # the engine's shared config — live edits to attack/decay/etc. still
        # propagate to in-flight voices.
        voice.note = note
        voice.velocity = velocity
        voice.age = self._age_counter
        voice.drift_val = 0.0
        voice.faust_slot = -1
        voice.osc1_weight = osc1_weight
        voice.osc2_weight = osc2_weight
        voice.shimmer_weight = shimmer_weight
        voice.adsr_osc1.stage = ADSREnvelope.OFF
        voice.adsr_osc1.level = 0.0
        voice.adsr_osc2.stage = ADSREnvelope.OFF
        voice.adsr_osc2.level = 0.0

        # Randomize unison starting phases so detuned voices decorrelate from
        # the first sample — classic supersaw trick. Aligned phases (all 0)
        # cause audible beating/LFO-phasing when detune is small.
        n_u = self.unison_voices
        voice.phases = [0.0] * n_u
        voice.osc1_phases = list(np.random.uniform(0.0, TWO_PI, n_u))
        voice.osc2_phases = list(np.random.uniform(0.0, TWO_PI, n_u))
        voice.shimmer_phases = list(np.random.uniform(0.0, TWO_PI, n_u))

        voice.adsr_osc1.trigger()
        voice.adsr_osc2.trigger()
        self._age_counter += 1
        # Faust slot assignment: steal if replacing a voice, else pop from pool.
        # Use try/except instead of `if list: pop()` because the render thread's
        # dead-voice cleanup can `.append` between the check and the pop, but
        # MORE importantly two MIDI notes hitting back-to-back can both pass
        # the `if` guard then race on the pop — one gets IndexError, one wins.
        # try/except is GIL-atomic per CPython op semantics.
        if self._faust_osc_bank is not None:
            if stolen_slot >= 0:
                voice.faust_slot = stolen_slot
            else:
                try:
                    voice.faust_slot = self._faust_slot_free.pop(0)
                except IndexError:
                    voice.faust_slot = -1  # shouldn't happen; pool sized to max_voices
                    logger.warning("faust osc bank: no free slot at note_on (pool empty)")
            if voice.faust_slot >= 0:
                # Fresh random phase offsets — matches Python's np.random.uniform
                # init of osc1/osc2 phase lists at note_on. Prevents osc1+osc2
                # fundamentals from summing coherently (~2× gain overshoot).
                self._faust_osc_bank.randomize_phase(voice.faust_slot)
                # Per-voice LFO phase: random at note_on so poly LFO mode
                # gives each note a different starting position in the cycle.
                # Cheap to always set (zone write); only audible when an LFO
                # has poly+amp engaged.
                if hasattr(self._faust_osc_bank, "randomize_lfo_phase"):
                    self._faust_osc_bank.randomize_lfo_phase(voice.faust_slot)
        self.voices.append(voice)

    def note_off(self, note: int):
        with self._render_lock:
            for v in self.voices:
                if v.note == note and v.adsr_osc1.stage != ADSREnvelope.RELEASE:
                    v.adsr_osc1.release()
                    v.adsr_osc2.release()

    def all_notes_off(self):
        with self._render_lock:
            for v in self.voices:
                v.adsr_osc1.release()
                v.adsr_osc2.release()

    def set_pitch_bend(self, value: float):
        """value in [-1, +1] — bipolar normalized. Maps to ±range semitones.
        Called from MIDI thread; render reads _pitch_bend_semitones (single
        float assignment is GIL-atomic, no lock needed)."""
        v = max(-1.0, min(1.0, float(value)))
        self._pitch_bend_semitones = v * self._pitch_bend_range

    @staticmethod
    def split_weight(note: int, low: int, high: int, xfade: int) -> float:
        """Smoothstep-interpolated note weight for a source whose key range
        is [low..high] with `xfade` keys of crossfade tail on each side.

          note < low - xfade           → 0.0  (fully outside)
          low - xfade <= note < low    → smoothstep ramp 0..1 across the tail
          low <= note <= high          → 1.0  (in body)
          high < note <= high + xfade  → smoothstep ramp 1..0 across the tail
          note > high + xfade          → 0.0

        Cubic smoothstep (`t*t*(3-2t)`) feels more natural across the boundary
        than a linear ramp — slow at the edges, faster in the middle."""
        if xfade <= 0:
            return 1.0 if (low <= note <= high) else 0.0
        if note < low - xfade or note > high + xfade:
            return 0.0
        if note < low:
            t = (note - (low - xfade)) / xfade
        elif note > high:
            t = ((high + xfade) - note) / xfade
        else:
            return 1.0
        t = max(0.0, min(1.0, t))
        return t * t * (3.0 - 2.0 * t)

    def compute_split_weights(self, note: int):
        """Return (osc1_w, osc2_w, shimmer_w) for the given MIDI note. When
        split is off, all are 1.0 — engine behaves identically to pre-split."""
        if not self.split_enabled:
            return 1.0, 1.0, 1.0
        return (
            self.split_weight(note, self.osc1_split_low,    self.osc1_split_high,    self.osc1_split_xfade),
            self.split_weight(note, self.osc2_split_low,    self.osc2_split_high,    self.osc2_split_xfade),
            self.split_weight(note, self.shimmer_split_low, self.shimmer_split_high, self.shimmer_split_xfade),
        )

    def panic(self):
        """Public hook — set pending flag; render thread executes at next
        block start. Lets the WS thread return immediately and avoids the
        race where panic clears state mid-render iteration."""
        self._panic_pending = True

    def _apply_panic(self):
        """Hard-silence everything: kill voices, drone, sympathetic, freeze, flush buffers.
        Always called from the render thread under _render_lock."""
        # Center the pitch bend. A stuck/abandoned bend (MPK strip, disconnected
        # keyboard mid-bend) otherwise leaves the pad detuned with no MIDI
        # event coming to clear it — only STOP gets you out.
        self._pitch_bend_semitones = 0.0
        for player in self._pad_samples.values():
            player.hard_stop()
        self._pad_mellow_lp_l.reset()
        self._pad_mellow_lp_r.reset()
        self._drone_fade_scale = 1.0
        for v in self.voices:
            v.adsr_osc1.stage = ADSREnvelope.OFF
            v.adsr_osc1.level = 0.0
            v.adsr_osc2.stage = ADSREnvelope.OFF
            v.adsr_osc2.level = 0.0
        if self._faust_osc_bank is not None:
            self._faust_osc_bank.panic()
            self._faust_slot_free = list(range(self._faust_nvoices))
            for v in self.voices:
                v.faust_slot = -1
        self._sympathetic_state.clear()
        if self._faust_sympathetic is not None:
            self._faust_sympathetic.clear_all()
            from .faust_sympathetic import N_SLOTS as _SN
            self._faust_symp_slot_free = list(range(_SN))
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
        # Faust ping-pong keeps its own delay lines (incl. up to 15s of
        # reverse buffer); with OBLIVION the loop is self-sustaining, so
        # panic MUST flush it or the delay survives the emergency stop.
        if self._faust_ping_pong is not None:
            self._faust_ping_pong.clear()
        # Reverb-send filters (used by per-OSC sends split-path)
        self._rev_send_filter_l.reset()
        self._rev_send_filter_r.reset()
        self._rev_send_filter2_l.reset()
        self._rev_send_filter2_r.reset()
        # Faust pad bus carries the same filter + Haas state natively —
        # flush it with the rest so panic leaves no ringing.
        if self._faust_pad_bus is not None:
            self._faust_pad_bus.clear()
        # LFO state — both phases and the per-block mod_last values.
        # Without this a hard chord right after panic re-engages the LFO
        # from whatever value it was riding when the panic landed, causing
        # a one-block ramp from a stale offset.
        self._lfo_phase = 0.0
        self._lfo2_phase = 0.0
        self._lfo_mod_a_last = 0.0
        self._lfo_mod_b_last = 0.0
        self._lfo2_mod_a_last = 0.0
        self._lfo2_mod_b_last = 0.0
        self._lfo_smooth_a_state = 0.0
        self._lfo_smooth_b_state = 0.0
        self._lfo2_smooth_a_state = 0.0
        self._lfo2_smooth_b_state = 0.0

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

    def _effective_reverse_window_ms(self) -> float:
        """Reverse window length in ms. AURORA wins; else tempo-sync if mode != FREE; else slider."""
        if self.delay_aurora_enabled:
            return max(50.0, min(15000.0, self.delay_aurora_seconds * 1000.0))
        if self.delay_reverse_window_mode != "FREE":
            mult = self._DELAY_DIVISIONS.get(self.delay_reverse_window_mode, 1.0)
            beat_sec = 60.0 / max(40.0, self.bpm)
            return max(50.0, min(3000.0, beat_sec * mult * 1000.0))
        return max(50.0, min(3000.0, self.delay_reverse_window_ms))

    def _process_ping_pong(self, out_l: np.ndarray, out_r: np.ndarray):
        """Stereo cross-feedback delay applied in place on out_l/out_r.
        Dry signal passes through unchanged; wet taps mix in at self.delay_wet × motion_mix.
        Read happens before write so we never self-read within a block."""
        effective_wet = self.delay_wet * self.motion_mix
        effective_rev = self.delay_reverse_amount * self.motion_mix
        if not self.delay_enabled or (effective_wet < 0.001 and effective_rev < 0.001):
            return
        if self._faust_ping_pong is not None:
            mult_abs = max(0.1, abs(self.delay_rate_multiplier))
            polarity = -1.0 if self.delay_rate_multiplier < 0 else 1.0
            delay_samps = max(1, int(self._delay_time_samples() / mult_abs))
            off_samps = int(self.delay_offset_ms * 0.001 * self.sample_rate / mult_abs)
            fb_eff = 1.0 if self.delay_oblivion else self.delay_feedback
            self._faust_ping_pong.set_params(
                delay_l_samps=delay_samps,
                delay_r_samps=delay_samps + off_samps,
                feedback=fb_eff,
                wet=effective_wet,
                low_cut_hz=self.delay_low_cut_hz,
                high_cut_hz=self.delay_high_cut_hz,
                drive=self.delay_drive,
                width=self.delay_width,
                mod_rate_hz=self.delay_mod_rate_hz,
                mod_depth_ms=self.delay_mod_depth_ms,
                polarity=polarity,
                reverse_amount=effective_rev,
                reverse_window_ms=self._effective_reverse_window_ms(),
                reverse_feedback=self.delay_reverse_feedback,
            )
            # Add piano/organ send into the delay input so they ping-pong
            # too, then subtract it back out — leaves only the wet taps from
            # the piano contribution added to the pad output. (Piano dry is
            # mixed by jack_engine downstream.)
            ext = getattr(self, "_external_delay_send", None)
            n = out_l.shape[0]
            if ext is not None and ext.shape[1] >= n:
                send_l = ext[0, :n]
                send_r = ext[1, :n]
                out_l += send_l
                out_r += send_r
                self._faust_ping_pong.process_inplace(out_l, out_r)
                out_l -= send_l
                out_r -= send_r
            else:
                self._faust_ping_pong.process_inplace(out_l, out_r)
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

        # Write current input + feedback from opposite tap into each buffer.
        # OBLIVION = infinite repeat: fb 1.0 with tanh in the loop for
        # stability, mirroring the Faust path — previously the Python
        # fallback silently ignored delay_oblivion (a 0.85 echo on Mac
        # reads as a bug). Other Faust-only delay features (filters, drive,
        # width, mod, reverse) remain unsupported here; see MAC_PORT.md.
        if self.delay_oblivion:
            write_l = np.tanh(out_l + tap_l)
            write_r = np.tanh(out_r + tap_r)
        else:
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

    def _advance_lfo(self, n_samples: int, which: int = 1):
        """Advance one LFO's phase by one block; return (mod_a, mod_b) bipolar
        **normalized** in [-1, +1]. Depth + motion_mix scaling happens at each
        application site so targets can apply target-specific formulas (e.g.
        amp needs a gate formula, not symmetric swing).

        `which` = 1 or 2; reads/writes state via a prefix ("lfo"/"lfo2") so
        the same body handles both LFOs."""
        pfx = "lfo" if which == 1 else "lfo2"
        ipfx = "_lfo" if which == 1 else "_lfo2"

        depth = getattr(self, pfx + "_depth")
        effective_depth = depth * self.motion_mix
        if effective_depth < 0.001:
            # Zero the per-block "last" mod values so a downstream consumer
            # (e.g. bus comp with sidechain="lfo") doesn't get a frozen
            # constant pump from whatever was here when the LFO was last on.
            setattr(self, ipfx + "_mod_a_last", 0.0)
            setattr(self, ipfx + "_mod_b_last", 0.0)
            return 0.0, 0.0
        block_sec = n_samples / self.sample_rate
        # Tempo-synced rate: beats-per-cycle derived from the same subdivision
        # table the ping-pong delay uses. FREE mode keeps the user's Hz slider.
        rate_mode = getattr(self, pfx + "_rate_mode")
        if rate_mode == "FREE":
            effective_rate_hz = getattr(self, pfx + "_rate_hz")
        else:
            beats_per_cycle = self._DELAY_DIVISIONS.get(rate_mode, 1.0)
            beat_sec = 60.0 / max(40.0, self.bpm)
            cycle_sec = max(0.05, beat_sec * beats_per_cycle)  # clamp: no runaway at tiny bpm
            cycle_sec /= max(0.1, getattr(self, pfx + "_rate_multiplier"))
            effective_rate_hz = 1.0 / cycle_sec
        shape = getattr(self, pfx + "_shape")
        spread = getattr(self, pfx + "_spread")
        prev_phase = getattr(self, ipfx + "_phase")
        new_phase = (prev_phase + effective_rate_hz * block_sec) % 1.0
        setattr(self, ipfx + "_phase", new_phase)
        # Sample & Hold regenerates on every phase wrap
        if shape == "sh" and new_phase < prev_phase:
            setattr(self, ipfx + "_sh_value", float(np.random.uniform(-1.0, 1.0)))
            setattr(self, ipfx + "_sh_value_r", float(np.random.uniform(-1.0, 1.0)))
        # Phase offset in ms → phase fraction via the current effective rate.
        # Applied to eval phases only — the running _lfo_phase advances
        # unchanged, so LINK still sees the two LFOs at the same
        # "rate/position" but with a fixed time-lag between them. Haas-comp
        # adds the current haas_delay_ms so the LFO tracks Haas changes live.
        offset_ms = getattr(self, pfx + "_offset_ms")
        if getattr(self, pfx + "_haas_compensate"):
            offset_ms += self.haas_delay_ms
        phase_off = (offset_ms / 1000.0) * effective_rate_hz
        phase_a = (new_phase + phase_off) % 1.0
        phase_b = (new_phase + phase_off + 0.5 * spread) % 1.0
        sh_a = getattr(self, ipfx + "_sh_value")
        sh_b = getattr(self, ipfx + "_sh_value_r")

        def _eval(phase, is_b):
            if shape == "triangle":
                return 4.0 * abs(phase - 0.5) - 1.0
            if shape == "square":
                return 1.0 if phase < 0.5 else -1.0
            if shape == "saw":
                # Ramp up 0..1 → bipolar -1..1, resets at phase wrap
                return 2.0 * phase - 1.0
            if shape == "ramp":
                # Reverse saw: drops 1→-1 across the cycle
                return 1.0 - 2.0 * phase
            if shape == "peak":
                # Asymmetric spike: fast rise (0-0.2), longer fall (0.2-1.0).
                # Output sweeps -1 → +1 → -1, with the peak near 20% of the cycle.
                if phase < 0.2:
                    return (phase / 0.2) * 2.0 - 1.0
                return (1.0 - (phase - 0.2) / 0.8) * 2.0 - 1.0
            if shape == "sh":
                return sh_b if is_b else sh_a
            # default: sine
            return float(np.sin(phase * TWO_PI))

        invert = getattr(self, pfx + "_invert")
        mod_a = _eval(phase_a, False)
        mod_b = _eval(phase_b, True)
        if invert:
            mod_a = -mod_a
            mod_b = -mod_b
        return mod_a, mod_b

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

    def sympathetic_set_suppress(self, suppress: bool):
        """Block sympathetic rendering and re-arm. Used during preset crossfade so
        held keys don't keep pumping tone into the reverb at changing levels.

        Called from the WS/crossfade thread while the render thread inserts
        and deletes _sympathetic_state entries (slot-steal does `del`) —
        snapshot with list() so a concurrent mutation can't raise
        RuntimeError mid-iteration (which would abort the preset load)."""
        self._sympathetic_suppress = bool(suppress)
        if suppress:
            for st in list(self._sympathetic_state.values()):
                st["target"] = 0.0

    def set_sympathetic_notes(self, notes: set):
        """Update which piano notes resonate sympathetically (with fade envelopes).

        Faust path uses a fixed 24-slot bank. When all slots are taken and a
        new note arrives (25+-note sustain-pedal chord), steal the oldest
        active slot instead of silently tracking the new note with slot=-1
        (which would be book-kept but produce no sound).
        """
        for n in notes:
            if n not in self._sympathetic_state:
                slot = -1
                if self._faust_sympathetic is not None:
                    if self._faust_symp_slot_free:
                        slot = self._faust_symp_slot_free.pop(0)
                    else:
                        # Pool empty → steal oldest. Dict iteration order is
                        # insertion order, so the first entry with an active
                        # slot is the oldest sympathetic note. Clear it from
                        # Faust and evict it from state so the new note takes
                        # its place audibly.
                        victim_note = None
                        for old_n, old_st in self._sympathetic_state.items():
                            if old_st.get("faust_slot", -1) >= 0:
                                victim_note = old_n
                                slot = old_st["faust_slot"]
                                break
                        if victim_note is not None:
                            self._faust_sympathetic.clear_slot(slot)
                            del self._sympathetic_state[victim_note]
                self._sympathetic_state[n] = {
                    "phase_l": 0.0, "phase_r": 0.0,
                    "gain": 0.0, "target": 1.0,
                    "faust_slot": slot,
                }
            else:
                self._sympathetic_state[n]["target"] = 1.0
        for n in list(self._sympathetic_state):
            if n not in notes:
                self._sympathetic_state[n]["target"] = 0.0

    # set_drone_key + drone_off removed 2026-04-26 — chord-drone (root+fifth)
    # synthesis was superseded by recorded pad samples. Pad-player keys without
    # a loaded sample are now silent. Internal `self.drone_enabled` stays False
    # forever; the render-path drone branch is unreachable.

    # ═══ Pad sample library ═══
    #
    # On startup, scan ~/.local/share/stave-synth/pad_samples/ for per-note WAVs.
    # Filename convention: pad_<NOTE>.wav where NOTE is C, Cs, D, Ds, E, F, Fs,
    # G, Gs, A, As, B (MIDI 60..71).
    #
    # When the pad-player UI sends a key, JackEngine / main.py calls
    # trigger_pad_sample(note) first — if that returns True, the sampler
    # produced the drone for that key. If False (no WAV for this slot), the
    # caller falls back to the live root+fifth oscillator bed.

    _PAD_NOTE_FILENAMES = {
        60: "pad_C.wav", 61: "pad_Cs.wav", 62: "pad_D.wav", 63: "pad_Ds.wav",
        64: "pad_E.wav", 65: "pad_F.wav", 66: "pad_Fs.wav", 67: "pad_G.wav",
        68: "pad_Gs.wav", 69: "pad_A.wav", 70: "pad_As.wav", 71: "pad_B.wav",
    }

    MAX_PAD_BANK_BYTES = 192 * 1024 * 1024
    _PAD_LIBRARY_LOCK = threading.RLock()  # control-only; never held by render

    class _PreparedPadSlot:
        def __init__(self, note):
            self.note = note
            self.player = None

    def prepare_pad_sample(self, note: int, path):
        """Reserve one inactive target and prepare its replacement off-render.

        ``path=None`` reserves a clear transaction. Caller must install or
        discard the returned opaque reservation, including on file failures.
        One outstanding reservation bounds staging and prevents bank-budget
        races between validation and an external atomic file commit.
        """
        if type(note) is not int or note not in self._PAD_NOTE_FILENAMES:
            raise ValueError("Invalid pad slot")
        with self._PAD_LIBRARY_LOCK:
            if getattr(self, "_pad_prepared", None) is not None:
                raise RuntimeError("Another pad update is already being prepared")
            with self._render_lock:
                old = self._pad_samples.get(note)
                if old is not None and old.active:
                    raise RuntimeError("Stop this pad before replacing it")
                others = sum(player.memory_bytes for key, player in self._pad_samples.items() if key != note)
                available = max(0, self.MAX_PAD_BANK_BYTES - others)
                reservation = self._PreparedPadSlot(note)
                self._pad_prepared = reservation
        try:
            if path is not None:
                reservation.player = SamplePlayer.prepare(path, self.sample_rate, max_bytes=available)
            return reservation
        except Exception:
            self.discard_prepared_pad(reservation)
            raise

    def install_pad_sample(self, note: int, prepared):
        """Install only the reserved slot at a render boundary; no file I/O."""
        with self._PAD_LIBRARY_LOCK:
            with self._render_lock:
                if (getattr(self, "_pad_prepared", None) is not prepared
                        or prepared is None or prepared.note != note):
                    raise ValueError("Pad preparation does not own this slot")
                # Public trigger methods reject reserved targets; this check
                # also fails safely if another caller bypasses that contract.
                old = self._pad_samples.get(note)
                if old is not None and old.active:
                    raise RuntimeError("Stop this pad before replacing it")
                updated = dict(self._pad_samples)
                if prepared.player is None:
                    updated.pop(note, None)
                else:
                    updated[note] = prepared.player
                self._pad_samples = updated
                self._pad_prepared = None
                errors = getattr(self, "_pad_load_errors", {})
                errors.pop(note, None)

    def discard_prepared_pad(self, prepared):
        """Release a failed/cancelled file transaction without touching slots."""
        with self._PAD_LIBRARY_LOCK:
            with self._render_lock:
                if getattr(self, "_pad_prepared", None) is prepared:
                    self._pad_prepared = None
                    return True
        return False

    def clear_pad_sample(self, note: int):
        """Clear an inactive in-memory target; file transactions use prepare(None)."""
        prepared = self.prepare_pad_sample(note, None)
        try:
            self.install_pad_sample(note, prepared)
        finally:
            self.discard_prepared_pad(prepared)

    def pad_sample_status(self) -> dict:
        with self._render_lock:
            pending = getattr(self, "_pad_prepared", None)
            return {
                "memory_bytes": sum(player.memory_bytes for player in self._pad_samples.values()),
                "max_bank_bytes": self.MAX_PAD_BANK_BYTES,
                "max_slot_bytes": SamplePlayer.MAX_SLOT_BYTES,
                "preparing_note": pending.note if pending is not None else None,
                "errors": dict(getattr(self, "_pad_load_errors", {})),
                "slots": {note: {"loaded": player.loaded, "active": player.active,
                                 "duration_seconds": player.length / self.sample_rate,
                                 "memory_bytes": player.memory_bytes}
                          for note, player in self._pad_samples.items()},
            }

    def load_pad_samples(self, pad_dir=None) -> int:
        """Load changed valid slots, retaining every unaffected/invalid old slot.

        Startup and explicit directory scans are bounded one slot at a time.
        Missing files do not discard a previously loaded bed; an explicit
        clear transaction does that. Errors are available via pad_sample_status.
        """
        from pathlib import Path
        if pad_dir is None:
            from .config import DATA_DIR
            pad_dir = DATA_DIR / "pad_samples"
        pad_dir = Path(pad_dir)
        pad_dir.mkdir(parents=True, exist_ok=True)
        self._pad_samples_dir = pad_dir
        errors = {}
        for note, fname in self._PAD_NOTE_FILENAMES.items():
            path = pad_dir / fname
            if not path.exists():
                continue
            prepared = None
            try:
                stat = path.stat()
                signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                with self._render_lock:
                    old = self._pad_samples.get(note)
                    if old is not None and old.loaded and old.source_signature == signature:
                        continue
                prepared = self.prepare_pad_sample(note, path)
                self.install_pad_sample(note, prepared)
            except Exception as exc:
                errors[note] = str(exc)
                logger.warning("Pad slot %s was not replaced: %s", note, exc)
            finally:
                if prepared is not None:
                    self.discard_prepared_pad(prepared)
        with self._render_lock:
            self._pad_load_errors = errors
            loaded = len(self._pad_samples)
        logger.info("Pad samples loaded: %d / 12 from %s", loaded, pad_dir)
        return loaded

    def trigger_pad_sample(self, note: int, rise_seconds: float = 0.0,
                           rise_cutoff_open: float = None) -> bool:
        """Trigger the pad slot for this MIDI note. Returns True if a sample
        was available and triggered. False means no slot file — caller should
        fall back to the live synth drone. rise_seconds > 0 enables RISE mode."""
        note = int(note)
        with self._render_lock:
            prepared = getattr(self, "_pad_prepared", None)
            if prepared is not None and prepared.note == note:
                raise RuntimeError("This pad slot is being updated; try again when it finishes")
            player = self._pad_samples.get(note)
            if player is None or not player.loaded:
                return False
            # Release any other active slots so we don't stack notes.
            for n, p in self._pad_samples.items():
                if n != note and p.active:
                    p.release()
            player.trigger(rise_seconds=rise_seconds, rise_cutoff_open=rise_cutoff_open)
            return True

    def release_pad_samples(self):
        """Fade out all active pad samples (e.g., when switching to live mode)."""
        with self._render_lock:
            for p in self._pad_samples.values():
                if p.active:
                    p.release()

    def render(self, n_samples: int, separate_fx: bool = False,
               external_reverb_send: np.ndarray = None,
               external_delay_send: np.ndarray = None):
        """Render the pad bus. `external_reverb_send` and `external_delay_send`
        are optional stereo (2, n) float64 buffers fed by jack_engine — they
        let piano/organ contribute to the reverb / ping-pong taps without
        changing the dry mix.

        Held under `_render_lock` for the full block so control-thread
        mutators (note_on/off, panic, multi-step setting transactions)
        can't race with iteration over `self.voices` / `_sympathetic_state`
        or with multi-step state transitions that render reads."""
        with self._render_lock:
            return self._render_locked(n_samples, separate_fx,
                                       external_reverb_send, external_delay_send)

    def _render_locked(self, n_samples: int, separate_fx: bool,
                       external_reverb_send, external_delay_send):
        # Drain pending panic (queued from any thread). Single thread of
        # mutation = no race with render iterating voices/sympathetic_state.
        if self._panic_pending:
            self._panic_pending = False
            self._apply_panic()
        self._external_delay_send = external_delay_send
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
        skip_voices = not render_osc1 and not render_osc2 and not render_shimmer

        both_filtered = self.osc1_filter_enabled and self.osc2_filter_enabled

        # Re-use pre-allocated buffers (zero and slice to current block size)
        # Per-OSC pre-filter accumulators for reverb-send tapping.
        self._osc1_pre_l[:n_samples] = 0.0
        self._osc1_pre_r[:n_samples] = 0.0
        self._osc2_pre_l[:n_samples] = 0.0
        self._osc2_pre_r[:n_samples] = 0.0
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

        # ── Faust path: write per-block global osc params once ──
        # Per-voice freq+gate get written inside the voice loop below.
        # NOTE: osc1_b/osc2_b are the -24dB-curved amplitudes, NOT raw fader
        # positions — Faust expects linear amplitude (it multiplies the wave
        # by the passed value directly). Passing raw faders here would over-
        # gain by ~2x (fader 0.6 = 0.331 linear on the dB curve).
        # Faust osc_bank.dsp hardcodes UNI=3 — route to Python path for any
        # other unison count so users picking 1 or 5 don't silently get 3.
        unison_ok = True
        if self._faust_osc_bank is not None and hasattr(self._faust_osc_bank, "supports_unison"):
            unison_ok = self._faust_osc_bank.supports_unison(self.unison_voices)
        use_faust = (self._faust_osc_bank is not None) and (not skip_voices) and unison_ok
        # Faust pad bus consumes the osc bank's 5-ch block directly — it can
        # only engage on blocks the Faust bank actually produced.
        use_pad_bus = use_faust and (self._faust_pad_bus is not None)
        # Phase 4: single-call bank→pad_bus shim (subset of pad-bus blocks).
        use_merged = use_pad_bus and (self._faust_merged is not None)
        if use_faust:
            self._faust_osc_bank.set_osc_params(
                osc1_wf=osc1_wf, osc2_wf=osc2_wf,
                osc1_blend=osc1_b, osc2_blend=osc2_b,
                osc1_octave=self.osc1_octave, osc2_octave=self.osc2_octave,
                unison_detune=self.unison_detune, unison_spread=spread,
                osc1_pan=o1_pan, osc2_pan=o2_pan,
            )
            self._faust_osc_bank.set_shimmer_params(
                enabled=render_shimmer, high=self.shimmer_high,
            )
            # Poly-LFO push: only the AMP target gets per-voice mod here.
            # Pan/Filter targets continue to apply globally via Python below.
            if hasattr(self._faust_osc_bank, "set_lfo_params"):
                lfo1_active = (self.lfo_active
                               and self.lfo_poly
                               and (self.lfo_depth * self.motion_mix) > 0.001
                               and self.lfo_target == "amp")
                lfo2_active = (self.lfo2_active
                               and self.lfo2_poly
                               and (self.lfo2_depth * self.motion_mix) > 0.001
                               and self.lfo2_target == "amp")
                # Effective rate (matches _advance_lfo logic) — Faust LFO
                # phasor uses this Hz value directly.
                def _eff_rate(prefix):
                    rate_mode = getattr(self, prefix + "_rate_mode")
                    if rate_mode == "FREE":
                        return getattr(self, prefix + "_rate_hz")
                    beats = self._DELAY_DIVISIONS.get(rate_mode, 1.0)
                    cycle_sec = max(0.05, (60.0 / max(40.0, self.bpm)) * beats)
                    cycle_sec /= max(0.1, getattr(self, prefix + "_rate_multiplier"))
                    return 1.0 / cycle_sec
                self._faust_osc_bank.set_lfo_params(
                    1, active=lfo1_active,
                    rate_hz=_eff_rate("lfo"),
                    depth=min(self.lfo_depth * self.motion_mix, 0.7),
                    shape=self.lfo_shape,
                )
                self._faust_osc_bank.set_lfo_params(
                    2, active=lfo2_active,
                    rate_hz=_eff_rate("lfo2"),
                    depth=min(self.lfo2_depth * self.motion_mix, 0.7),
                    shape=self.lfo2_shape,
                )

        voice_idx = 0
        for voice in self.voices:
            if not voice.is_active():
                dead_voices.append(voice)
                continue

            # Per-OSC ADSR. Sustain fast-path returns scalar (no np.full alloc).
            # Each env can be scalar or array; numpy broadcasting handles the mix.
            if voice.adsr_osc1.stage == ADSREnvelope.SUSTAIN:
                env1 = voice.adsr_osc1.config.sustain_percent / 100.0
            else:
                env1 = voice.adsr_osc1.process(n_samples)
            if voice.adsr_osc2.stage == ADSREnvelope.SUSTAIN:
                env2 = voice.adsr_osc2.config.sustain_percent / 100.0
            else:
                env2 = voice.adsr_osc2.process(n_samples)

            if skip_voices:
                voice_idx += 1
                continue

            base_freq = 440.0 * (2.0 ** ((voice.note - 69) / 12.0))
            # Pitch bend (pad-only). Read once per voice — _pitch_bend_semitones
            # is GIL-atomic float; if MIDI thread updates mid-block the next
            # voice/block sees the new value.
            if self._pitch_bend_semitones != 0.0:
                base_freq *= 2.0 ** (self._pitch_bend_semitones / 12.0)
            # Per-voice analog drift: slow random walk → slight detune per
            # voice. Uncorrelated across voices so a chord has the classic
            # analog "breathing" quality. Zero-cost when drift_cents <= 0.01.
            if self.analog_drift_cents > 0.01:
                voice.drift_val += float(np.random.uniform(-0.08, 0.08))
                voice.drift_val *= 0.998  # weak self-centering restoring force
                if voice.drift_val > 1.0:
                    voice.drift_val = 1.0
                elif voice.drift_val < -1.0:
                    voice.drift_val = -1.0
                base_freq *= 2.0 ** (self.analog_drift_cents * voice.drift_val / 1200.0)
            base_inc = TWO_PI * base_freq / self.sample_rate
            voice_shimmer = self._voice_shimmer[voice_idx, :n_samples]
            voice_shimmer[:] = 0

            # Per-unison phase increments for this voice (n_uni,)
            osc1_inc_per_u = base_inc * detune_mult * osc1_oct_mult
            osc2_inc_per_u = base_inc * detune_mult * osc2_oct_mult
            # Shimmer: octave-up (+12 = 2x) or two octaves up (+24 = 4x) when shimmer_high
            shim_mult = 4.0 if self.shimmer_high else 2.0
            shim_inc_per_u = base_inc * shim_mult * detune_mult

            # ── Oscillator generation: Faust writes slot, Python fills buffers ──
            if use_faust:
                # Write this voice's slot. Faust owns osc1/osc2 generation.
                # Per-OSC gates let OSC1 and OSC2 have independent envelope shapes.
                # LAYER weights silence/scale this voice's per-OSC contribution
                # without changing the global osc1_blend / osc2_blend.
                env1_scalar = float(env1) if np.isscalar(env1) else float(env1[-1])
                env2_scalar = float(env2) if np.isscalar(env2) else float(env2[-1])
                g1 = env1_scalar * voice.velocity * voice.osc1_weight
                g2 = env2_scalar * voice.velocity * voice.osc2_weight
                self._faust_osc_bank.set_voice(voice.faust_slot, base_freq, g1, g2)
                if hasattr(self._faust_osc_bank, "set_shimmer_weight"):
                    self._faust_osc_bank.set_shimmer_weight(voice.faust_slot, voice.shimmer_weight)
                osc1_l = osc1_r = osc2_l = osc2_r = None  # filled via Faust post-loop
            else:
                osc1_l = self._voice_osc1_l[voice_idx, :n_samples]
                osc1_l[:] = 0
                osc1_r = self._voice_osc1_r[voice_idx, :n_samples]
                osc1_r[:] = 0
                osc2_l = self._voice_osc2_l[voice_idx, :n_samples]
                osc2_l[:] = 0
                osc2_r = self._voice_osc2_r[voice_idx, :n_samples]
                osc2_r[:] = 0

                if render_osc1:
                    uni_starts = np.asarray(voice.osc1_phases[:n_uni], dtype=np.float64)
                    ph_2d = uni_starts[:, None] + osc1_inc_per_u[:, None] * indices[None, :]
                    # dt = per-sample phase increment as fraction of cycle, per
                    # unison voice. Drives polyBLEP anti-aliasing for saw/square.
                    dt_2d = (osc1_inc_per_u / TWO_PI)[:, None]
                    wave_2d = generate_waveform(osc1_wf, ph_2d, dt=dt_2d) * osc1_b
                    osc1_l += (wave_2d * o1_gl_arr[:, None]).sum(axis=0)
                    osc1_r += (wave_2d * o1_gr_arr[:, None]).sum(axis=0)
                    for u in range(n_uni):
                        voice.osc1_phases[u] = float(ph_2d[u, -1]) % TWO_PI

                if render_osc2:
                    uni_starts = np.asarray(voice.osc2_phases[:n_uni], dtype=np.float64)
                    ph_2d = uni_starts[:, None] + osc2_inc_per_u[:, None] * indices[None, :]
                    dt_2d = (osc2_inc_per_u / TWO_PI)[:, None]
                    wave_2d = generate_waveform(osc2_wf, ph_2d, dt=dt_2d) * osc2_b
                    osc2_l += (wave_2d * o2_gl_arr[:, None]).sum(axis=0)
                    osc2_r += (wave_2d * o2_gr_arr[:, None]).sum(axis=0)
                    for u in range(n_uni):
                        voice.osc2_phases[u] = float(ph_2d[u, -1]) % TWO_PI

            if render_shimmer and not use_faust:
                # Faust path: shimmer generated in osc_bank.dsp (5th output channel),
                # taken post-loop and assigned directly to shimmer_sines — skip here.
                uni_starts = np.asarray(voice.shimmer_phases[:n_uni], dtype=np.float64)
                shim_2d = uni_starts[:, None] + shim_inc_per_u[:, None] * indices[None, :]
                voice_shimmer += np.sin(shim_2d).sum(axis=0)
                # Advance phases by full block length for consistent tracking
                new_phases = (uni_starts + shim_inc_per_u * n_samples) % TWO_PI
                for u in range(n_uni):
                    voice.shimmer_phases[u] = float(new_phases[u])

            # Unison gain-normalization common factor (envelope applied per-OSC below)
            scale_common = (1.0 / max(n_uni, 1)) * (1.0 + 0.15 * (n_uni - 1)) * voice.velocity
            # LAYER weights fold into the per-OSC scale on the Python fallback
            # path (Faust path multiplies them into g1/g2 above).
            scale1 = scale_common * env1 * voice.osc1_weight
            scale2 = scale_common * env2 * voice.osc2_weight

            if render_shimmer and not use_faust:
                # Shimmer tracks the voice lifetime (louder of the two envelopes)
                # so it doesn't cut out when one OSC releases faster than the other.
                if np.isscalar(env1) and np.isscalar(env2):
                    env_voice = max(env1, env2)
                else:
                    e1 = env1 if not np.isscalar(env1) else np.full(n_samples, env1)
                    e2 = env2 if not np.isscalar(env2) else np.full(n_samples, env2)
                    env_voice = np.maximum(e1, e2)
                shimmer_sines += voice_shimmer * scale_common * env_voice * 0.30 * voice.shimmer_weight

            if not use_faust:
                # Python path: apply per-OSC env*vel scale and route osc1/osc2 by flag
                osc1_l *= scale1
                osc1_r *= scale1
                osc2_l *= scale2
                osc2_r *= scale2

                # OSC1 goes directly to filter buffers + pre-filter accumulator
                if self.osc1_filter_enabled:
                    filter_buf[0] += osc1_l
                    filter_buf[1] += osc1_r
                else:
                    osc1_indep_buf[0] += osc1_l
                    osc1_indep_buf[1] += osc1_r
                self._osc1_pre_l[:n_samples] += osc1_l
                self._osc1_pre_r[:n_samples] += osc1_r

                # OSC2 accumulates separately for Haas delay
                osc2_accum_l += osc2_l
                osc2_accum_r += osc2_r

            voice_idx += 1

        # ── Faust path: one call after loop, 4-channel output → routing ──
        # Faust has env*vel baked in (via gate) and unison_scale baked in too.
        if use_faust:
            # Clear gates for unused slots (voices not in this block's active set)
            active_slots = {v.faust_slot for v in self.voices if v.is_active() and v.faust_slot >= 0}
            for slot in range(self._faust_nvoices):
                if slot not in active_slots:
                    self._faust_osc_bank.clear_voice(slot)

            if use_merged:
                # Phase 4 merged path: the bank compute is fused into the
                # single C call at the pad-bus site below. All bank zones
                # (set_voice / gate clears above) are already written, and
                # the gate clears land before the bank compute on BOTH
                # paths — same relative order. The pre-filter tap adds and
                # the _osc2_pre snapshot are deferred to that site.
                osc_out = None
            else:
                osc_out = self._faust_osc_bank.process(n_samples)  # (5, n)
                if use_pad_bus:
                    # Faust pad bus takes the raw block — skip the Python
                    # filter-buffer routing (those buffers only feed the Python
                    # filter path). Pre-filter taps below still fill so the LFO
                    # split-path / FX-bypass magnitude ratios keep working.
                    pass
                elif self.osc1_filter_enabled:
                    filter_buf[0] += osc_out[0]
                    filter_buf[1] += osc_out[1]
                else:
                    osc1_indep_buf[0] += osc_out[0]
                    osc1_indep_buf[1] += osc_out[1]
                # OSC1 pre-filter snapshot for the reverb-send tap.
                self._osc1_pre_l[:n_samples] += osc_out[0]
                self._osc1_pre_r[:n_samples] += osc_out[1]
                osc2_accum_l += osc_out[2]
                osc2_accum_r += osc_out[3]
                if render_shimmer and not use_pad_bus:
                    # Pad-bus path: the shimmer channel reaches the Faust
                    # shimmer chain directly as pad-bus input 5 — the Python
                    # chain (which is what consumes shimmer_sines) is skipped.
                    shimmer_sines += osc_out[4]

        # Apply Haas delay to OSC2 if pans are separated and osc2 is audible.
        # (Faust pad bus applies Haas internally — incl. for its send tap —
        # so on that path the Python ring buffer stays untouched and
        # _osc2_pre_* below holds the pre-Haas signal; it only feeds the
        # LFO-split / FX-bypass magnitude ratios there, where a <=40 ms
        # shift is immaterial.)
        if haas_active and render_osc2 and not use_pad_bus:
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
                osc2_accum_l = self._haas_buf_l[rd_start:rd_start + n_samples]
                osc2_accum_r = self._haas_buf_r[rd_start:rd_start + n_samples]
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

        # Snapshot OSC2 post-Haas for the reverb-send tap BEFORE routing into
        # shared or indep filter (we want this signal with Haas applied but
        # before any filter stage). (Merged path: osc2_accum is still empty
        # here — the snapshot is taken right after the merged call instead.)
        if not use_merged:
            np.copyto(self._osc2_pre_l[:n_samples], osc2_accum_l)
            np.copyto(self._osc2_pre_r[:n_samples], osc2_accum_r)

        # Route OSC2 (possibly delayed) to filter buffers
        if render_osc2 and not use_pad_bus:
            if self.osc2_filter_enabled:
                filter_buf[0] += osc2_accum_l
                filter_buf[1] += osc2_accum_r
            else:
                osc2_indep_buf[0] += osc2_accum_l
                osc2_indep_buf[1] += osc2_accum_r

        for v in dead_voices:
            if self._faust_osc_bank is not None and v.faust_slot >= 0:
                self._faust_osc_bank.clear_voice(v.faust_slot)
                self._faust_slot_free.append(v.faust_slot)
                v.faust_slot = -1
            self.voices.remove(v)
            self._voice_pool.append(v)

        # Smooth filter cutoff in log space (~80ms time constant)
        alpha_s = 1.0 - np.exp(-n_samples / (0.08 * self.sample_rate))

        log_cur = np.log(max(self._filter_cutoff_cur, 20.0))
        log_tgt = np.log(max(self.filter_cutoff, 20.0))
        log_cur += alpha_s * (log_tgt - log_cur)
        self._filter_cutoff_cur = np.exp(log_cur)

        # ─── LFO compute (once per block) + filter modulation ───
        # Both LFOs advance every block. Each has its own target; their mod
        # contributions sum at the target (filter/amp/pan). Zero-cost when
        # disabled — _advance_lfo returns (0, 0) early.
        lfo1_a, lfo1_b = self._advance_lfo(n_samples, which=1)
        lfo2_a, lfo2_b = self._advance_lfo(n_samples, which=2)

        # Depth × motion_mix scales raw LFO into its modulation contribution.
        # Capped at 0.7 — beyond that, the per-voice amplitude swing is wide
        # enough that AM sidebands push the master limiter on transients
        # (audible as faint "buzz" clicks). The cap engages only when both
        # depth AND motion fader are pushed near full; normal use lands well
        # below it. Removes the need to baby the motion fader for live work.
        _LFO_DEPTH_CAP = 0.7
        # Master enable per LFO — gates the depth without touching the
        # depth slider value, so disabling and re-enabling restores the
        # exact prior modulation amount. depth=0 → nothing to mod; same
        # net effect as depth slider at 0.
        lfo1_d = min(self.lfo_depth  * self.motion_mix, _LFO_DEPTH_CAP) if self.lfo_active  else 0.0
        lfo2_d = min(self.lfo2_depth * self.motion_mix, _LFO_DEPTH_CAP) if self.lfo2_active else 0.0

        filter_mod = 0.0
        # Filter is shared between OSCs, so route by "any OSC receives this
        # LFO" — if neither OSC receives, the LFO has no business moving the
        # shared cutoff. Per-OSC filter routing would need per-OSC filters
        # (deferred — see project_next_work memo).
        any_recv_lfo1 = self.osc1_recv_lfo1 or self.osc2_recv_lfo1
        any_recv_lfo2 = self.osc1_recv_lfo2 or self.osc2_recv_lfo2
        # Filter coefficients update once per block, so use the block's
        # ramp midpoint instead of its end value. Imperceptibly shifts steady-state
        # mod by half a block (~2.5ms) but halves the per-block jump on key-sync,
        # which is the dominant click source for filter target.
        if any_recv_lfo1 and self.lfo_target == "filter" and lfo1_d > 0.001:
            filter_mod += (lfo1_a + self._lfo_mod_a_last) * 0.5 * lfo1_d
        if any_recv_lfo2 and self.lfo2_target == "filter" and lfo2_d > 0.001:
            filter_mod += (lfo2_a + self._lfo2_mod_a_last) * 0.5 * lfo2_d

        effective_cutoff = self._filter_cutoff_cur
        if abs(filter_mod) > 0.001:
            # ±2 octaves at combined depth = 1
            effective_cutoff = self._filter_cutoff_cur * (2.0 ** (filter_mod * 2.0))

        # ── Analog filter drift (global, slow) ──
        # Simulates thermal drift of a real analog lowpass. One random walk
        # applied to the shared cutoff. Peak deviation in cents; zero-cost
        # when drift_cents <= 0.01.
        if self.filter_drift_cents > 0.01:
            self._filter_drift_val += float(np.random.uniform(-0.06, 0.06))
            self._filter_drift_val *= 0.9985
            if self._filter_drift_val > 1.0:
                self._filter_drift_val = 1.0
            elif self._filter_drift_val < -1.0:
                self._filter_drift_val = -1.0
            effective_cutoff *= 2.0 ** (self.filter_drift_cents * self._filter_drift_val / 1200.0)

        # ── Filter self-resonance wobble ──
        # Gated on resonance — silent below 0.5, scales up as res → 1.0 so
        # the effect is only audible near self-oscillation (analog filters
        # wobble audibly there; perfectly still at low resonance).
        if self.filter_wobble_amount > 0.001 and self.filter_resonance > 0.5:
            self._filter_wobble_val += float(np.random.uniform(-0.12, 0.12))
            self._filter_wobble_val *= 0.995
            if self._filter_wobble_val > 1.0:
                self._filter_wobble_val = 1.0
            elif self._filter_wobble_val < -1.0:
                self._filter_wobble_val = -1.0
            res_scale = (self.filter_resonance - 0.5) * 2.0   # 0..1 as res 0.5..1
            effective_cutoff *= 1.0 + self.filter_wobble_amount * res_scale * self._filter_wobble_val * 0.03

        effective_cutoff = max(20.0, min(20000.0, effective_cutoff))

        # Only recalculate filter coefficients if cutoff or resonance actually changed
        cutoff_changed = (abs(effective_cutoff - self._filter_cutoff_last_set) > 0.1
                          or self.filter_resonance != self._filter_res_last_set)
        if cutoff_changed:
            self._filter_cutoff_last_set = effective_cutoff
            self._filter_res_last_set = self.filter_resonance
            # For 24 dB slope we cascade two biquads with STAGGERED Q
            # (Butterworth 4-pole) instead of biquad². See _Q24_S*_RATIO
            # comment at module top.
            if self.filter_slope == 24:
                q_s1 = self.filter_resonance * _Q24_S1_RATIO
                q_s2 = self.filter_resonance * _Q24_S2_RATIO
                self.filter_l.set_params(effective_cutoff, q_s1)
                self.filter_r.set_params(effective_cutoff, q_s1)
                self.filter2_l.set_params(effective_cutoff, q_s2)
                self.filter2_r.set_params(effective_cutoff, q_s2)
            else:
                self.filter_l.set_params(effective_cutoff, self.filter_resonance)
                self.filter_r.set_params(effective_cutoff, self.filter_resonance)
            if self.reverb_filter_enabled:
                if self.filter_slope == 24:
                    q_s1 = self.filter_resonance * _Q24_S1_RATIO
                    q_s2 = self.filter_resonance * _Q24_S2_RATIO
                    self.reverb_filter_l.set_params(effective_cutoff, q_s1)
                    self.reverb_filter_r.set_params(effective_cutoff, q_s1)
                    self.reverb_filter2_l.set_params(effective_cutoff, q_s2)
                    self.reverb_filter2_r.set_params(effective_cutoff, q_s2)
                else:
                    self.reverb_filter_l.set_params(effective_cutoff, self.filter_resonance)
                    self.reverb_filter_r.set_params(effective_cutoff, self.filter_resonance)
            # Reverb-send path filter tracks master cutoff so the weighted
            # pre-filter osc sum reaches the reverb with the same tonal shape
            # the dry signal has. Only bites when sends differ from 1.0, but
            # keeping the instance warm avoids a cold-filter click the first
            # time a user moves a send slider.
            if self.filter_slope == 24:
                q_s1 = self.filter_resonance * _Q24_S1_RATIO
                q_s2 = self.filter_resonance * _Q24_S2_RATIO
                self._rev_send_filter_l.set_params(effective_cutoff, q_s1)
                self._rev_send_filter_r.set_params(effective_cutoff, q_s1)
                self._rev_send_filter2_l.set_params(effective_cutoff, q_s2)
                self._rev_send_filter2_r.set_params(effective_cutoff, q_s2)
            else:
                self._rev_send_filter_l.set_params(effective_cutoff, self.filter_resonance)
                self._rev_send_filter_r.set_params(effective_cutoff, self.filter_resonance)

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
        # Filter gain compensation: reduces volume as filter opens to prevent brightness = loudness
        # Saw/square have massive harmonic energy above the cutoff
        f_min = max(self.filter_range_min, 20.0)
        f_max = max(self.filter_range_max, f_min + 1.0)
        f_pos = np.log(max(self._filter_cutoff_cur, f_min) / f_min) / np.log(f_max / f_min)
        f_pos = max(0.0, min(1.0, f_pos))
        # Shallow power curve: spreads compensation evenly across the range
        # f_pos^1.3 with -15dB max: even attenuation from low-mids through highs
        filter_comp = 10.0 ** (-15.0 * f_pos ** 1.3 / 20.0)

        # ─── Phase 4 (merged path): decide where LFO amp/pan applies ───
        # all_recv is the legacy fast path — one combined mod on the dry
        # bus. Only that case moves into pad_bus.dsp: the per-OSC split
        # path's ratio is data-dependent on THIS block's osc magnitudes,
        # which don't exist until the merged call has run, so it stays in
        # Python as a per-block fallback (smoother state is canonical in
        # Python and handed to/from Faust each block, so the transition is
        # sample-exact). Conditions mirror _osc_amp_pan's branches exactly
        # (recv=True on the fast path; used_here excludes filter/bus
        # targets and poly-amp-via-Faust — no double application).
        all_recv = (self.osc1_recv_lfo1 and self.osc1_recv_lfo2
                    and self.osc2_recv_lfo1 and self.osc2_recv_lfo2)
        merged_lfo_block = use_merged and all_recv
        _l1_via_faust = use_faust and self.lfo_poly and self.lfo_target == "amp"
        _l2_via_faust = use_faust and self.lfo2_poly and self.lfo2_target == "amp"
        faust_lfo1 = (merged_lfo_block and lfo1_d > 0.001
                      and (self.lfo_target == "pan"
                           or (self.lfo_target == "amp" and not _l1_via_faust)))
        faust_lfo2 = (merged_lfo_block and lfo2_d > 0.001
                      and (self.lfo2_target == "pan"
                           or (self.lfo2_target == "amp" and not _l2_via_faust)))

        output_l = self._output_l[:n_samples]
        output_l[:] = 0
        output_r = self._output_r[:n_samples]
        output_r[:] = 0
        # Faust pad bus send output for the reverb section below (None on
        # the Python path or when the fast copy path applies).
        pad_bus_send_l = None
        pad_bus_send_r = None
        if use_pad_bus:
            # ── Faust pad bus: push per-block zones, run the 6-ch module ──
            # Python keeps ALL parameter-side work (log-space cutoff
            # smoothing, LFO/drift/wobble effective cutoff, the >0.1 Hz
            # set_params skip via _filter_cutoff_last_set, f_pos math);
            # Faust consumes the zone values verbatim — no double-smoothing.
            if not self.osc1_filter_enabled:
                f1_pos = np.log(max(self._osc1_indep_cutoff_cur, f_min) / f_min) / np.log(f_max / f_min)
                f1_pos = max(0.0, min(1.0, f1_pos))
            else:
                f1_pos = 0.0  # branch input is zero — comp value is moot
            if not self.osc2_filter_enabled:
                f2_pos = np.log(max(self._osc2_indep_cutoff_cur, f_min) / f_min) / np.log(f_max / f_min)
                f2_pos = max(0.0, min(1.0, f2_pos))
            else:
                f2_pos = 0.0
            # Highpass smoothing — mirrors the Python block in the else
            # branch (which is skipped on this path).
            hp_on = False
            if self.filter_highpass_hz > 25.0 or self._filter_highpass_cur > 25.0:
                lc_hp = np.log(max(self._filter_highpass_cur, 20.0))
                lt_hp = np.log(max(self.filter_highpass_hz, 20.0))
                lc_hp += alpha_s * (lt_hp - lc_hp)
                self._filter_highpass_cur = np.exp(lc_hp)
                hp_on = self._filter_highpass_cur > 25.0
            # Per-OSC sends with fx-bypass zeroing — same formula as the
            # reverb section below; send_filter_active mirrors Python's
            # slow-path condition so the Faust send-filter state advances
            # only on blocks where _rev_send_filter_* would process.
            _s1 = 0.0 if self.osc1_fx_bypass else float(self.osc1_reverb_send)
            _s2 = 0.0 if self.osc2_fx_bypass else float(self.osc2_reverb_send)
            _send_slow = not (abs(_s1 - 1.0) < 1e-6 and abs(_s2 - 1.0) < 1e-6)
            # Phase 2 shimmer chain (Faust-side): Python keeps the ~80 ms
            # mix one-pole, advancing it ONLY on active blocks — exactly
            # like the Python chain below (skipped on this path) does at
            # its own gate. The HP cutoff zone carries the mode-dependent
            # value; Faust swaps coefficients with state carried, so the
            # deferred-flip bookkeeping (_shimmer_hp_high_last) is not
            # needed here and Python's _shimmer_hp stays untouched.
            _shimmer_on = render_shimmer and voice_idx > 0
            if _shimmer_on:
                self._shimmer_mix_cur += alpha_s * (self.shimmer_mix - self._shimmer_mix_cur)
            self._faust_pad_bus.set_block_params(
                cutoff_hz=self._filter_cutoff_last_set,
                resonance=self._filter_res_last_set,
                slope24=(self.filter_slope == 24),
                f_pos=f_pos,
                osc1_filter_enabled=self.osc1_filter_enabled,
                osc2_filter_enabled=self.osc2_filter_enabled,
                osc1_indep_cutoff=self._osc1_indep_cutoff_cur,
                osc2_indep_cutoff=self._osc2_indep_cutoff_cur,
                osc1_f_pos=f1_pos, osc2_f_pos=f2_pos,
                hp_on=hp_on, hp_cutoff=self._filter_highpass_cur,
                osc1_send=_s1, osc2_send=_s2,
                send_filter_active=_send_slow,
                haas_on=(haas_active and render_osc2),
                haas_samps=self._haas_delay_samples,
                osc2_audible=render_osc2,
                shimmer_active=_shimmer_on,
                shimmer_hp_hz=(1200.0 if self.shimmer_high else 400.0),
                shimmer_mix_cur=self._shimmer_mix_cur,
                shimmer_send=self.shimmer_send,
            )
            if use_merged:
                # ── Phase 4: push LFO zones, then ONE C call runs the
                # bank compute → f32→f64 widen → pad-bus compute. Python
                # stays the owner of every scalar (LFO advance, ramp
                # endpoints, depth caps, smoother coefficient) AND the
                # smoother state — pushed in, read back post-compute — so
                # fallback blocks (recv split path, bus target) continue
                # sample-exact in Python on the same state variables.
                _sm1 = self.lfo_smooth
                _sm2 = self.lfo2_smooth
                self._faust_pad_bus.push_lfo_block(
                    n_samples=n_samples,
                    en1=faust_lfo1, amp1=(self.lfo_target == "amp"),
                    d1=lfo1_d,
                    a1_start=self._lfo_mod_a_last, a1_end=lfo1_a,
                    b1_start=self._lfo_mod_b_last, b1_end=lfo1_b,
                    # Exact _smooth_one_pole coefficient formula (0.0 ==
                    # its <=0.001 passthrough — identical arithmetic).
                    sm1=(0.0 if _sm1 <= 0.001 else math.exp(
                        -1.0 / max((0.0005 + 0.0995 * _sm1) * self.sample_rate, 1.0))),
                    st1_a=self._lfo_smooth_a_state,
                    st1_b=self._lfo_smooth_b_state,
                    en2=faust_lfo2, amp2=(self.lfo2_target == "amp"),
                    d2=lfo2_d,
                    a2_start=self._lfo2_mod_a_last, a2_end=lfo2_a,
                    b2_start=self._lfo2_mod_b_last, b2_end=lfo2_b,
                    sm2=(0.0 if _sm2 <= 0.001 else math.exp(
                        -1.0 / max((0.0005 + 0.0995 * _sm2) * self.sample_rate, 1.0))),
                    st2_a=self._lfo2_smooth_a_state,
                    st2_b=self._lfo2_smooth_b_state,
                )
                pad_out, osc_out = self._faust_merged.process(n_samples)
                # Sync smoother states for exactly the LFOs Faust ramped
                # (mirrors which _smooth_one_pole calls Python would make).
                if faust_lfo1:
                    self._lfo_smooth_a_state, self._lfo_smooth_b_state = \
                        self._faust_pad_bus.read_lfo_states(1)
                if faust_lfo2:
                    self._lfo2_smooth_a_state, self._lfo2_smooth_b_state = \
                        self._faust_pad_bus.read_lfo_states(2)
                # Deferred pre-filter taps + OSC2 post-loop accumulation
                # (the two-call path does these at the bank-compute site;
                # they only feed the LFO-split / FX-bypass magnitude
                # ratios on this path — pure reads of osc_out, so the
                # later position is order-equivalent).
                self._osc1_pre_l[:n_samples] += osc_out[0]
                self._osc1_pre_r[:n_samples] += osc_out[1]
                osc2_accum_l += osc_out[2]
                osc2_accum_r += osc_out[3]
                np.copyto(self._osc2_pre_l[:n_samples], osc2_accum_l)
                np.copyto(self._osc2_pre_r[:n_samples], osc2_accum_r)
            else:
                pad_out = self._faust_pad_bus.process(osc_out)  # (9, n)
            np.copyto(output_l, pad_out[0])
            np.copyto(output_r, pad_out[1])
            if _send_slow:
                pad_bus_send_l = pad_out[2]
                pad_bus_send_r = pad_out[3]
            # pad_out[4]/[5] (fx-bypass bus) are reserved — always zero;
            # the bypass carve stays in Python below (post-LFO,
            # data-dependent magnitude ratio). pad_out[6..8] (shimmer +
            # CLOUD) are added to reverb_in in the reverb section below.
        else:
            if self.osc1_filter_enabled or self.osc2_filter_enabled:
                filtered_l = self.filter_l.process(filter_buf[0]) * filter_comp
                filtered_r = self.filter_r.process(filter_buf[1]) * filter_comp
                if self.filter_slope == 24:
                    filtered_l = self.filter2_l.process(filtered_l)
                    filtered_r = self.filter2_r.process(filtered_r)
                output_l += filtered_l
                output_r += filtered_r
            # Independent (non-shared) filter paths get the same
            # brightness-vs-loudness compensation, derived from each OSC's own
            # cutoff. Without it, unchecking "shared filter" jumped that OSC up
            # by the shared path's comp amount (~11 dB at the default cutoff).
            if not self.osc1_filter_enabled:
                f1_pos = np.log(max(self._osc1_indep_cutoff_cur, f_min) / f_min) / np.log(f_max / f_min)
                f1_pos = max(0.0, min(1.0, f1_pos))
                comp1 = 10.0 ** (-15.0 * f1_pos ** 1.3 / 20.0)
                output_l += self.osc1_indep_filter_l.process(osc1_indep_buf[0]) * comp1
                output_r += self.osc1_indep_filter_r.process(osc1_indep_buf[1]) * comp1
            if not self.osc2_filter_enabled:
                f2_pos = np.log(max(self._osc2_indep_cutoff_cur, f_min) / f_min) / np.log(f_max / f_min)
                f2_pos = max(0.0, min(1.0, f2_pos))
                comp2 = 10.0 ** (-15.0 * f2_pos ** 1.3 / 20.0)
                output_l += self.osc2_indep_filter_l.process(osc2_indep_buf[0]) * comp2
                output_r += self.osc2_indep_filter_r.process(osc2_indep_buf[1]) * comp2

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
        # Each LFO's contribution ramps linearly from its own previous block's
        # normalized end value to its new normalized value — avoids the block-rate
        # step that would otherwise click (audible in reverb tail).
        #
        # AMP uses a gate-style formula so depth=1 = full cut at trough:
        #   gate = 1 - d + d * (1 + lfo_norm) / 2   ∈ [0, 1] at d=1, centered at 1 at d=0
        # No makeup gain: the gate peaks at exactly 1.0 (tremolo-style dip-only),
        # so it can never push the signal above unity regardless of depth or
        # LFO stacking — inherently safe against limiter overshoot.
        # Two amp LFOs compose multiplicatively (both must be "open" for sound through).
        #
        # PAN stays with the symmetric ±(d/2) swing on L (+) / R (−), which stacks
        # additively across multiple LFOs targeting pan.
        #
        # Per-OSC routing: the amp/pan mods can be selectively applied to each
        # OSC via the *_recv_lfo* checkboxes. When all four receives are true
        # (default), the math reduces to the legacy combined-bus mod. When
        # routes differ, we split output_l/r by per-OSC pre-filter magnitude
        # ratio (a cheap approximation), apply each OSC's mods independently,
        # then sum back. Linear filter timbre means the ratio split is a tiny
        # spectrum approximation but musically transparent.
        def _amp_gate(r, d):
            # Tremolo-style gate: peak naturally at 1.0, trough at 1-d.
            # No makeup boost — depth controls how much the amp DIPS, never
            # pumps above unity. Inherently safe against limiter overshoot
            # regardless of depth or LFO stacking.
            return 1.0 - d + d * (1.0 + r) * 0.5

        def _fill_ramp(buf, start, end):
            """Fill `buf` with linspace(start, end, len(buf)) — but reusing
            a cached [0..1] axis. Allocates ONCE the first time a given
            n_samples is seen; subsequent calls are 2 in-place numpy ops."""
            n = buf.shape[0]
            if self._linspace_t_n != n:
                self._linspace_t = np.linspace(0.0, 1.0, n, dtype=np.float64)
                self._linspace_t_n = n
            np.multiply(self._linspace_t, end - start, out=buf)
            buf += start
            return buf

        def _smooth_one_pole(x, prev_state, smooth_amt):
            """One-pole LP on per-sample LFO ramp. smooth_amt in [0,1] maps
            to time constant 0.5ms..100ms. Returns (filtered, new_state).

            Coefficient arrays are reused from `self._smooth_b/_a/_zi` so this
            never allocates inside the audio thread. (Was 3 small numpy allocs
            per call × 4 calls per block × 187 blocks/sec = ~2200 allocs/sec
            of churn for idle GC to clean up.)"""
            if smooth_amt <= 0.001:
                return x, x[-1]
            tau_s = 0.0005 + 0.0995 * smooth_amt
            a = math.exp(-1.0 / max(tau_s * self.sample_rate, 1.0))
            self._smooth_b[0] = 1.0 - a
            self._smooth_a[1] = -a
            self._smooth_zi[0] = a * prev_state
            y, _zf = lfilter(self._smooth_b, self._smooth_a, x, zi=self._smooth_zi)
            return y, float(y[-1])
        def _osc_amp_pan(recv1, recv2):
            amul_l = None; amul_r = None
            pmod_l = None; pmod_r = None
            # Suppress AMP mod here when Faust handles it per-voice (poly mode).
            # Pan target always stays Python-side regardless of poly setting.
            lfo1_amp_via_faust = use_faust and self.lfo_poly and self.lfo_target == "amp"
            lfo2_amp_via_faust = use_faust and self.lfo2_poly and self.lfo2_target == "amp"
            # Only compute ramps + advance the one-pole smoother state when this
            # function will actually consume them (target amp-in-Python or pan).
            # For "filter"/"bus" targets (or amp handled per-voice in Faust) the
            # work was wasted AND the bus branch below would double-step the
            # same smoother state.
            # TODO: in the per-OSC split path _osc_amp_pan runs once per OSC, so
            # an LFO received by both OSCs still steps the smoother twice per
            # block (pre-existing; benign at typical smooth settings).
            lfo1_used_here = (self.lfo_target == "pan"
                              or (self.lfo_target == "amp" and not lfo1_amp_via_faust))
            lfo2_used_here = (self.lfo2_target == "pan"
                              or (self.lfo2_target == "amp" and not lfo2_amp_via_faust))
            if recv1 and lfo1_d > 0.001 and lfo1_used_here:
                r1a = _fill_ramp(self._lfo_ramp_a1[:n_samples], self._lfo_mod_a_last, lfo1_a)
                r1b = _fill_ramp(self._lfo_ramp_b1[:n_samples], self._lfo_mod_b_last, lfo1_b)
                r1a, self._lfo_smooth_a_state = _smooth_one_pole(r1a, self._lfo_smooth_a_state, self.lfo_smooth)
                r1b, self._lfo_smooth_b_state = _smooth_one_pole(r1b, self._lfo_smooth_b_state, self.lfo_smooth)
                if self.lfo_target == "amp" and not lfo1_amp_via_faust:
                    amul_l = _amp_gate(r1a, lfo1_d)
                    amul_r = _amp_gate(r1b, lfo1_d)
                elif self.lfo_target == "pan":
                    pmod_l = r1a * lfo1_d * 0.5
                    pmod_r = -r1a * lfo1_d * 0.5
            if recv2 and lfo2_d > 0.001 and lfo2_used_here:
                r2a = _fill_ramp(self._lfo_ramp_a2[:n_samples], self._lfo2_mod_a_last, lfo2_a)
                r2b = _fill_ramp(self._lfo_ramp_b2[:n_samples], self._lfo2_mod_b_last, lfo2_b)
                r2a, self._lfo2_smooth_a_state = _smooth_one_pole(r2a, self._lfo2_smooth_a_state, self.lfo2_smooth)
                r2b, self._lfo2_smooth_b_state = _smooth_one_pole(r2b, self._lfo2_smooth_b_state, self.lfo2_smooth)
                if self.lfo2_target == "amp" and not lfo2_amp_via_faust:
                    g2l = _amp_gate(r2a, lfo2_d)
                    g2r = _amp_gate(r2b, lfo2_d)
                    amul_l = g2l if amul_l is None else amul_l * g2l
                    amul_r = g2r if amul_r is None else amul_r * g2r
                elif self.lfo2_target == "pan":
                    add_l = r2a * lfo2_d * 0.5
                    add_r = -r2a * lfo2_d * 0.5
                    pmod_l = add_l if pmod_l is None else pmod_l + add_l
                    pmod_r = add_r if pmod_r is None else pmod_r + add_r
            # No cap needed — gate naturally stays in [1-d, 1.0].
            return amul_l, amul_r, pmod_l, pmod_r

        # all_recv was computed pre-pad-bus (Phase 4 hoist) — the merged
        # path needed it before the compute to route the LFO zones.
        if all_recv:
            if merged_lfo_block:
                # Phase 4: pad_bus.dsp already applied the amp gate + pan
                # (same ramps, same one-pole, states synced post-compute).
                # _osc_amp_pan must NOT run here — it would double-step
                # the smoother state.
                pass
            else:
                # Fast path — single combined mod, identical to legacy behaviour.
                amp_mul_l, amp_mul_r, pan_mod_l_add, pan_mod_r_add = _osc_amp_pan(True, True)
                if amp_mul_l is not None:
                    output_l *= amp_mul_l
                    output_r *= amp_mul_r
                if pan_mod_l_add is not None:
                    output_l *= (1.0 + pan_mod_l_add)
                    output_r *= (1.0 + pan_mod_r_add)
        else:
            # Per-OSC split path. Ratio from pre-filter magnitudes; if both
            # OSCs are silent, ratios fall back to 0.5/0.5 (mod has nothing
            # to act on so the value is moot).
            o1l = self._osc1_pre_l[:n_samples]
            o1r = self._osc1_pre_r[:n_samples]
            o2l = self._osc2_pre_l[:n_samples]
            o2r = self._osc2_pre_r[:n_samples]
            m1 = float(np.abs(o1l).sum() + np.abs(o1r).sum())
            m2 = float(np.abs(o2l).sum() + np.abs(o2r).sum())
            tot = m1 + m2
            if tot > 1e-6:
                ratio1 = m1 / tot
                ratio2 = m2 / tot
            else:
                ratio1 = 0.5
                ratio2 = 0.5
            split1_l = output_l * ratio1
            split1_r = output_r * ratio1
            split2_l = output_l * ratio2
            split2_r = output_r * ratio2
            a1l, a1r, p1l, p1r = _osc_amp_pan(self.osc1_recv_lfo1, self.osc1_recv_lfo2)
            a2l, a2r, p2l, p2r = _osc_amp_pan(self.osc2_recv_lfo1, self.osc2_recv_lfo2)
            if a1l is not None:
                split1_l *= a1l; split1_r *= a1r
            if a2l is not None:
                split2_l *= a2l; split2_r *= a2r
            if p1l is not None:
                split1_l *= (1.0 + p1l); split1_r *= (1.0 + p1r)
            if p2l is not None:
                split2_l *= (1.0 + p2l); split2_r *= (1.0 + p2r)
            output_l[:] = split1_l + split2_l
            output_r[:] = split1_r + split2_r
        # Bus-target amp mod — computed once using the previous block's ramp
        # end values (_lfo_mod_a_last is not yet updated below). Stored here,
        # applied post-reverb on stereo_out so the full pad + reverb tail
        # pumps like a sidechain, instead of just the dry OSC signal.
        bus_amul_l = None
        bus_amul_r = None
        if self.lfo_target == "bus" and lfo1_d > 0.001:
            r1a = _fill_ramp(self._lfo_ramp_a1[:n_samples], self._lfo_mod_a_last, lfo1_a)
            r1b = _fill_ramp(self._lfo_ramp_b1[:n_samples], self._lfo_mod_b_last, lfo1_b)
            r1a, self._lfo_smooth_a_state = _smooth_one_pole(r1a, self._lfo_smooth_a_state, self.lfo_smooth)
            r1b, self._lfo_smooth_b_state = _smooth_one_pole(r1b, self._lfo_smooth_b_state, self.lfo_smooth)
            bus_amul_l = _amp_gate(r1a, lfo1_d)
            bus_amul_r = _amp_gate(r1b, lfo1_d)
        if self.lfo2_target == "bus" and lfo2_d > 0.001:
            r2a = _fill_ramp(self._lfo_ramp_a2[:n_samples], self._lfo2_mod_a_last, lfo2_a)
            r2b = _fill_ramp(self._lfo_ramp_b2[:n_samples], self._lfo2_mod_b_last, lfo2_b)
            r2a, self._lfo2_smooth_a_state = _smooth_one_pole(r2a, self._lfo2_smooth_a_state, self.lfo2_smooth)
            r2b, self._lfo2_smooth_b_state = _smooth_one_pole(r2b, self._lfo2_smooth_b_state, self.lfo2_smooth)
            g2l = _amp_gate(r2a, lfo2_d)
            g2r = _amp_gate(r2b, lfo2_d)
            bus_amul_l = g2l if bus_amul_l is None else bus_amul_l * g2l
            bus_amul_r = g2r if bus_amul_r is None else bus_amul_r * g2r

        # Remember this block's end values (normalized) for the next block's ramp start
        self._lfo_mod_a_last = lfo1_a
        self._lfo_mod_b_last = lfo1_b
        self._lfo2_mod_a_last = lfo2_a
        self._lfo2_mod_b_last = lfo2_b

        # Snapshot dry pad (post-LFO, pre-FX) so callers asking for FX-bypass
        # routing can subtract and extract a clean dry bus for the bus comp.
        # Buffers pre-allocated in _ensure_buffers.
        if separate_fx:
            _pre_fx_dry_l = self._dry_snap_l[:n_samples]
            _pre_fx_dry_r = self._dry_snap_r[:n_samples]
            np.copyto(_pre_fx_dry_l, output_l)
            np.copyto(_pre_fx_dry_r, output_r)
        else:
            _pre_fx_dry_l = None
            _pre_fx_dry_r = None

        # Per-OSC FX Bypass: carve the bypassed osc's fraction out of output_l/r
        # before delay + reverb so they process only the non-bypass portion.
        # The carved fraction (bypass_l/r) is added back post-FX at full dry
        # level, so bypassed oscs stay audible at any wet/dry setting and do
        # not pump with the bus-target LFO. Magnitude ratio approximation is
        # exact for single-osc-playing cases and close-enough for dual.
        bypass_ratio = 0.0
        bypass_l = None
        bypass_r = None
        if self.osc1_fx_bypass or self.osc2_fx_bypass:
            o1l = self._osc1_pre_l[:n_samples]
            o1r = self._osc1_pre_r[:n_samples]
            o2l = self._osc2_pre_l[:n_samples]
            o2r = self._osc2_pre_r[:n_samples]
            m1 = float(np.abs(o1l).sum() + np.abs(o1r).sum())
            m2 = float(np.abs(o2l).sum() + np.abs(o2r).sum())
            tot = m1 + m2
            if tot > 1e-6:
                bypass_mag = 0.0
                if self.osc1_fx_bypass:
                    bypass_mag += m1
                if self.osc2_fx_bypass:
                    bypass_mag += m2
                bypass_ratio = bypass_mag / tot
            if bypass_ratio > 1e-6:
                # Pre-allocated in _ensure_buffers.
                bypass_l = self._bypass_snap_l[:n_samples]
                bypass_r = self._bypass_snap_r[:n_samples]
                np.multiply(output_l, bypass_ratio, out=bypass_l)
                np.multiply(output_r, bypass_ratio, out=bypass_r)
                output_l *= (1.0 - bypass_ratio)
                output_r *= (1.0 - bypass_ratio)

        # ─── Ping-pong delay (pre-reverb so taps get cathedral treatment) ───
        self._process_ping_pong(output_l, output_r)

        # Reverb gets stereo input — preserves stereo image in tail.
        # Per-OSC reverb send: when both sends are 1.0 and neither osc is
        # fx-bypassed, reverb_in mirrors output exactly (fast path, matches
        # legacy behaviour). When sends diverge OR an osc is bypassed, build
        # a weighted sum from pre-filter osc signals and push it through a
        # dedicated reverb-path filter tracking the master cutoff.
        s1 = 0.0 if self.osc1_fx_bypass else float(self.osc1_reverb_send)
        s2 = 0.0 if self.osc2_fx_bypass else float(self.osc2_reverb_send)
        reverb_in_l = self._reverb_in_l[:n_samples]
        reverb_in_r = self._reverb_in_r[:n_samples]
        if abs(s1 - 1.0) < 1e-6 and abs(s2 - 1.0) < 1e-6:
            np.copyto(reverb_in_l, output_l)
            np.copyto(reverb_in_r, output_r)
        elif pad_bus_send_l is not None:
            # Faust pad bus already produced the weighted + send-filtered
            # slow-path signal (incl. filter_comp and the 24 dB stage).
            np.copyto(reverb_in_l, pad_bus_send_l)
            np.copyto(reverb_in_r, pad_bus_send_r)
        else:
            osc1_pre_l = self._osc1_pre_l[:n_samples]
            osc1_pre_r = self._osc1_pre_r[:n_samples]
            osc2_pre_l = self._osc2_pre_l[:n_samples]
            osc2_pre_r = self._osc2_pre_r[:n_samples]
            weighted_l = osc1_pre_l * s1 + osc2_pre_l * s2
            weighted_r = osc1_pre_r * s1 + osc2_pre_r * s2
            filtered_l = self._rev_send_filter_l.process(weighted_l) * filter_comp
            filtered_r = self._rev_send_filter_r.process(weighted_r) * filter_comp
            if self.filter_slope == 24:
                filtered_l = self._rev_send_filter2_l.process(filtered_l)
                filtered_r = self._rev_send_filter2_r.process(filtered_r)
            np.copyto(reverb_in_l, filtered_l)
            np.copyto(reverb_in_r, filtered_r)

        if use_pad_bus:
            # Faust pad bus already produced the whole shimmer chain (HP
            # split × smoothed mix on ch 6, CLOUD taps ×shimmer_send on
            # ch 7/8) — outputs are gated to exact zeros when the chain is
            # inactive, and the adds below mirror the Python chain's order
            # (shimmer both channels, then cloud per channel).
            if render_shimmer and voice_idx > 0:
                reverb_in_l += pad_out[6]
                reverb_in_r += pad_out[6]
                if self.shimmer_send > 0.001:
                    reverb_in_l += pad_out[7]
                    reverb_in_r += pad_out[8]
        elif render_shimmer and voice_idx > 0:
            # Update shimmer HP cutoff when the mode flips so +12 mode
            # (2× pitch) gets a 400 Hz HP that actually lets fundamentals
            # through, instead of the 1200 Hz HP that made +12 silent below A4.
            if self.shimmer_high != self._shimmer_hp_high_last:
                self._shimmer_hp.set_params(
                    1200.0 if self.shimmer_high else 400.0, 0.707,
                )
                self._shimmer_hp_high_last = self.shimmer_high
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

            if self._faust_sympathetic is not None:
                # ── Faust path: write per-slot freq + (target × rolloff) ──
                self._faust_sympathetic.set_sym_level(sym_level)
                dead_sym = []
                for note, st in self._sympathetic_state.items():
                    slot = st.get("faust_slot", -1)
                    freq = 440.0 * 2.0 ** ((note - 69) / 12.0)
                    rolloff = min(1.0, 523.0 / max(freq, 523.0))
                    target = float(st["target"])
                    if slot >= 0:
                        self._faust_sympathetic.set_slot(slot, freq, target * rolloff)
                    # Track a Python-side gain mirror for death detection.
                    # Use same 150ms time constant as Faust's smoother.
                    decay_rate = np.exp(-n_samples / (0.15 * self.sample_rate))
                    st["gain"] += (1.0 - decay_rate) * (target - st["gain"])
                    if st["gain"] < 0.001 and target <= 0:
                        dead_sym.append(note)
                sym_out = self._faust_sympathetic.process(n_samples)
                reverb_in_l += sym_out[0]
                reverb_in_r += sym_out[1]
                for note in dead_sym:
                    st = self._sympathetic_state[note]
                    slot = st.get("faust_slot", -1)
                    if slot >= 0:
                        self._faust_sympathetic.clear_slot(slot)
                        self._faust_symp_slot_free.append(slot)
                    del self._sympathetic_state[note]
            else:
                decay_rate = np.exp(-1.0 / (0.15 * self.sample_rate))
                # Shared across all entries: exponential ramp factors over the block
                ramp_factors = decay_rate ** indices

                notes_list = list(self._sympathetic_state.keys())
                N = len(notes_list)
                freqs = self._sym_freqs[:N]
                tgts = self._sym_tgts[:N]
                g0s = self._sym_g0s[:N]
                ph_l_init = self._sym_ph_l_init[:N]
                ph_r_init = self._sym_ph_r_init[:N]
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
                # R channel detune: 0.3% ratio (~5.2 cents) gives pleasant
                # chorus at low registers, but the *beat frequency* scales
                # linearly with pitch — so at C7 (2093 Hz) the flutter is
                # 6.3 Hz (too fast). Cap at +3 Hz offset so high registers
                # max out at a 3 Hz beat, which is chorus-y rather than
                # tremolo-fast. np.minimum picks the smaller of the two:
                #   - ratio detune dominates below ~1 kHz (0.3 % < 3 Hz)
                #   - absolute-offset detune dominates above ~1 kHz
                detuned_freqs_r = np.minimum(freqs * 1.003, freqs + 3.0)
                inc_r = TWO_PI * detuned_freqs_r / self.sample_rate
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

        # Sum in external reverb send (piano + organ contributions from
        # jack_engine). Happens BEFORE the tanh so external content gets the
        # same soft-limit behavior as pad sources.
        if external_reverb_send is not None and external_reverb_send.shape[1] >= n_samples:
            reverb_in_l += external_reverb_send[0, :n_samples]
            reverb_in_r += external_reverb_send[1, :n_samples]

        # Pre-trim before the soft limit. With piano + organ + shimmer +
        # sympathetic + dry pad all summed in, reverb_in routinely peaked
        # past ±2 and tanh squashed hard, dirtying the reverb tail. 0.6
        # gives ~4dB headroom while keeping body — limiter still catches
        # the rest.
        reverb_in_l *= 0.6
        reverb_in_r *= 0.6
        np.tanh(reverb_in_l, out=reverb_in_l)
        np.tanh(reverb_in_r, out=reverb_in_r)

        # Main reverb — stereo in, stereo out (reuse _stereo_out as temp)
        self._stereo_out[0, :n_samples] = reverb_in_l
        self._stereo_out[1, :n_samples] = reverb_in_r
        reverb_out = self.reverb.process(self._stereo_out[:, :n_samples])

        # Upstream NaN trap. A feedback-net reverb (FDN, drone resonators)
        # that somehow ingests a NaN will propagate NaN forever through its
        # recurrence — every subsequent block is silent by the time tanh and
        # nan_to_num at the master stage clamp it. Catch here and reset the
        # reverb so the next block recovers instead of staying silent.
        # Fast path: np.isfinite on two 256-length floats ~= 15 µs on Pi 5.
        if not np.isfinite(reverb_out).all():
            logger.warning(
                "REVERB_NAN detected — zeroing wet buffer + panicking reverb state"
            )
            np.nan_to_num(reverb_out, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
            try:
                self.reverb.panic()
            except Exception as _e:
                logger.warning("reverb.panic() failed during NaN recovery: %s", _e)

        # Optionally filter the reverb wet output through main filter.
        # NO filter_comp here — the fast path already tapped `output_l/r`
        # (which was compensated at :2938) into reverb_in, so applying comp
        # a second time on the wet double-dips by up to -30 dB at low cutoff,
        # making the reverb vanish under a closed filter. Review #7 fix.
        if self.reverb_filter_enabled:
            reverb_out[0] = self.reverb_filter_l.process(reverb_out[0])
            reverb_out[1] = self.reverb_filter_r.process(reverb_out[1])
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

        # Post-FX bus-target LFO amp mod. Applied here so the dry pad AND the
        # reverb tail both pump together — delivers sidechain "breathing" feel
        # instead of the pre-FX "amp" target's OSC-only tremolo. Applied BEFORE
        # the bypass add-back so bypassed oscs stay anchored (they're
        # intentionally out of the sidechain bus).
        if bus_amul_l is not None:
            stereo_out[0] *= bus_amul_l
            stereo_out[1] *= bus_amul_r

        # Add bypass portion at full dry level. Bypassed oscs get no reverb,
        # no delay, no sidechain — just clean dry audible at any wet setting.
        if bypass_l is not None:
            stereo_out[0] += bypass_l
            stereo_out[1] += bypass_r

        # Pad sample playback → dedicated stereo scratch so we can also add it
        # to dry_bus in fx-bypass mode. Each SamplePlayer applies its own
        # attack/release envelope + loop crossfade. Buffers pre-allocated.
        pad_l = self._pad_sample_l[:n_samples]
        pad_r = self._pad_sample_r[:n_samples]
        pad_l[:] = 0.0
        pad_r[:] = 0.0
        for _player in self._pad_samples.values():
            if _player.active:
                _player.process(n_samples, pad_l, pad_r)

        # Mellow filter — applied to combined pad sample bus for tonal cut.
        # Bypassed when any pad voice is in rise mode (rise has its own filter
        # sweep already; stacking mellow on top would over-darken the swell).
        any_rise = any(getattr(p, "_rise_filter_engaged", False)
                       for p in self._pad_samples.values() if p.active)
        if self.pad_mellow_enabled and not any_rise:
            if self.pad_mellow_cutoff_hz != self._pad_mellow_last_cutoff:
                self._pad_mellow_lp_l.set_params(self.pad_mellow_cutoff_hz, 0.707)
                self._pad_mellow_lp_r.set_params(self.pad_mellow_cutoff_hz, 0.707)
                self._pad_mellow_last_cutoff = self.pad_mellow_cutoff_hz
            pad_l[:] = self._pad_mellow_lp_l.process(pad_l)
            pad_r[:] = self._pad_mellow_lp_r.process(pad_r)

        # Pad bus — pad-sample playback. VOL fader (drone_level, name kept
        # for legacy state-file compat) × master-fade scale.
        pad_vol = self.drone_level * self._drone_fade_scale
        stereo_out[0] += pad_l * pad_vol
        stereo_out[1] += pad_r * pad_vol

        if separate_fx:
            # dry bus = pre-FX pad × eff_dry_gain + pad samples
            # fx bus = final − dry = delay taps × dry_gain + reverb wet × wet_gain_val.
            # eff_dry_gain accounts for per-OSC fx_bypass: the bypass fraction
            # stays at unity regardless of dry/wet, the rest tracks dry_gain.
            # Pre-allocated dry_bus + fx_bus scratches replace the per-block
            # `np.empty((2, n_samples))` allocs (~4 KB × 187/sec each).
            eff_dry_gain = bypass_ratio + (1.0 - bypass_ratio) * dry_gain
            dry_bus = self._dry_bus_scratch[:, :n_samples]
            # Compose into dry_bus channel-by-channel via in-place ops. The
            # `pad_l * pad_vol` temp is small (one block of float64) and
            # CPython reuses the same allocation slot for short-lived numpy
            # temps — not zero-cost but ~10× cheaper than the full bus alloc.
            np.multiply(_pre_fx_dry_l, eff_dry_gain, out=dry_bus[0])
            dry_bus[0] += pad_l * pad_vol
            np.multiply(_pre_fx_dry_r, eff_dry_gain, out=dry_bus[1])
            dry_bus[1] += pad_r * pad_vol
            fx_bus = self._fx_bus_scratch[:, :n_samples]
            np.subtract(stereo_out[0], dry_bus[0], out=fx_bus[0])
            np.subtract(stereo_out[1], dry_bus[1], out=fx_bus[1])
            # Pad bus headroom trim — applied to both dry and fx splits so
            # the FX-bypass routing stays in sync with the unified path.
            dry_bus *= 0.85
            fx_bus *= 0.85
            return dry_bus, fx_bus

        # Pad bus headroom trim — ~1.4dB to give the master limiter clearance
        # against LFO peaks + dense FX summing + poly-LFO higher avg loudness.
        # Calibrated against the post-system-volume-honoring chain (ART USB
        # DI fix made everything ~6dB hotter than original tuning).
        stereo_out *= 0.85
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
            if LOW_RAM_MODE and new_count != 3:
                # Small-Pi profile: the Faust fast path (osc bank + merged
                # pad_bus chain) only engages at unison_voices == 3; any other
                # value drops the whole render to the Python skeleton, which
                # a Pi 4 cannot run in real time. Pin to 3.
                logger.info("LOW_RAM_MODE: clamping unison_voices %d -> 3 "
                            "(Faust fast path requires 3)", new_count)
                new_count = 3
            if new_count != self.unison_voices:
                # Multi-step transaction: count + per-voice phase lists must
                # change atomically w.r.t. the render thread, which snapshots
                # unison_voices then slices osc*_phases against (n_uni,)-shaped
                # detune arrays — interleaving broadcasts a short list into a
                # ValueError → dropped block (audible gap). Same pattern as
                # the lfo_target handler below.
                with self._render_lock:
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
                    self.unison_voices = new_count
        if "unison_detune" in params:
            self.unison_detune = float(params["unison_detune"])
        if "unison_spread" in params:
            self.unison_spread = max(0.0, min(1.0, float(params["unison_spread"])))
        if "osc1_pan" in params:
            self.osc1_pan = max(-1.0, min(1.0, float(params["osc1_pan"])))
        if "osc2_pan" in params:
            self.osc2_pan = max(-1.0, min(1.0, float(params["osc2_pan"])))
        if "osc_hard_pan" in params:
            new_hard_pan = bool(params["osc_hard_pan"])
            if new_hard_pan != self.osc_hard_pan:
                self._haas_buf_l[:] = 0.0
                self._haas_buf_r[:] = 0.0
            self.osc_hard_pan = new_hard_pan
        if "haas_delay_ms" in params:
            ms = max(5.0, min(40.0, float(params["haas_delay_ms"])))
            if abs(ms - self.haas_delay_ms) > 0.01:
                self._haas_buf_l[:] = 0.0
                self._haas_buf_r[:] = 0.0
            self.haas_delay_ms = ms
            self._haas_delay_samples = int(ms * 0.001 * self.sample_rate)

        # Per-OSC ADSR. Back-compat: a legacy "adsr" dict splats to both OSCs.
        # Per-OSC "adsr_osc1" / "adsr_osc2" keys win if present.
        legacy_adsr = params.get("adsr", {})
        adsr_osc1 = params.get("adsr_osc1", legacy_adsr)
        adsr_osc2 = params.get("adsr_osc2", legacy_adsr)
        if "attack_ms" in adsr_osc1:
            self.adsr_osc1_config.attack_ms = float(adsr_osc1["attack_ms"])
        if "decay_ms" in adsr_osc1:
            self.adsr_osc1_config.decay_ms = float(adsr_osc1["decay_ms"])
        if "sustain_percent" in adsr_osc1:
            self.adsr_osc1_config.sustain_percent = float(adsr_osc1["sustain_percent"])
        if "release_ms" in adsr_osc1:
            self.adsr_osc1_config.release_ms = float(adsr_osc1["release_ms"])
        if "attack_ms" in adsr_osc2:
            self.adsr_osc2_config.attack_ms = float(adsr_osc2["attack_ms"])
        if "decay_ms" in adsr_osc2:
            self.adsr_osc2_config.decay_ms = float(adsr_osc2["decay_ms"])
        if "sustain_percent" in adsr_osc2:
            self.adsr_osc2_config.sustain_percent = float(adsr_osc2["sustain_percent"])
        if "release_ms" in adsr_osc2:
            self.adsr_osc2_config.release_ms = float(adsr_osc2["release_ms"])

        if "filter_cutoff_hz" in params:
            self.filter_cutoff = float(params["filter_cutoff_hz"])
        if "filter_resonance" in params:
            self.filter_resonance = float(params["filter_resonance"])
        if "analog_drift_cents" in params:
            self.analog_drift_cents = max(0.0, min(20.0, float(params["analog_drift_cents"])))
        if "filter_drift_cents" in params:
            self.filter_drift_cents = max(0.0, min(40.0, float(params["filter_drift_cents"])))
        if "filter_wobble_amount" in params:
            self.filter_wobble_amount = max(0.0, min(1.0, float(params["filter_wobble_amount"])))
        if "filter_range_min" in params:
            self.filter_range_min = float(params["filter_range_min"])
        if "filter_range_max" in params:
            self.filter_range_max = float(params["filter_range_max"])
        if "filter_slope" in params:
            slope = int(params["filter_slope"])
            if slope in (12, 24) and slope != self.filter_slope:
                with self._render_lock:
                    self.filter_slope = slope
                    if slope == 12:
                        self.filter2_l.reset()
                        self.filter2_r.reset()
                    # Invalidate coefficient cache so filter2 picks up the
                    # current cutoff on the next render when switching 12→24.
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
        if "reverb_type" in params and hasattr(self.reverb, "set_type"):
            self.reverb.set_type(str(params["reverb_type"]))
        if "osc1_reverb_send" in params:
            self.osc1_reverb_send = max(0.0, min(1.0, float(params["osc1_reverb_send"])))
        if "osc2_reverb_send" in params:
            self.osc2_reverb_send = max(0.0, min(1.0, float(params["osc2_reverb_send"])))
        if "osc1_fx_bypass" in params:
            self.osc1_fx_bypass = bool(params["osc1_fx_bypass"])
        if "osc2_fx_bypass" in params:
            self.osc2_fx_bypass = bool(params["osc2_fx_bypass"])
        for _k in ("osc1_recv_lfo1", "osc1_recv_lfo2", "osc2_recv_lfo1", "osc2_recv_lfo2"):
            if _k in params:
                setattr(self, _k, bool(params[_k]))
        if "reverb_decay_seconds" in params:
            self.reverb.set_decay(float(params["reverb_decay_seconds"]))
        # Direct zone writes for expert reverb sliders — only applies when the
        # Faust backend is active (Python fallback silently ignores them).
        if "reverb_damp" in params and hasattr(self.reverb, "set_damp"):
            # Fans out to plate/drone backends too (FaustReverb.set_damp);
            # Python fallback has no set_damp → silently ignored, as before.
            self.reverb.set_damp(float(params["reverb_damp"]))
        if "reverb_shimmer_fb" in params and hasattr(self.reverb, "set_shimmer_feedback"):
            self.reverb.set_shimmer_feedback(float(params["reverb_shimmer_fb"]))
        if "reverb_noise_mod" in params and hasattr(self.reverb, "set_noise_mod"):
            self.reverb.set_noise_mod(float(params["reverb_noise_mod"]))
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
        # ── LAYER (split) per-source key ranges. All values clamped to MIDI
        # 0..127; xfade clamped to 0..24 keys (a 2-octave xfade is the max
        # anyone's likely to want). `split_enabled` itself lives on master.
        for src in ("osc1", "osc2", "shimmer"):
            for fld, lo, hi in ((f"{src}_split_low", 0, 127),
                                (f"{src}_split_high", 0, 127),
                                (f"{src}_split_xfade", 0, 24)):
                if fld in params:
                    setattr(self, fld, max(lo, min(hi, int(params[fld]))))
        if "lfo_rate_hz" in params:
            self.lfo_rate_hz = max(0.05, min(20.0, float(params["lfo_rate_hz"])))
        if "lfo_rate_mode" in params:
            m = str(params["lfo_rate_mode"])
            if m == "FREE" or m in self._DELAY_DIVISIONS:
                self.lfo_rate_mode = m
        if "lfo_rate_multiplier" in params:
            self.lfo_rate_multiplier = max(0.1, min(10.0, float(params["lfo_rate_multiplier"])))
        if "lfo_key_sync" in params:
            self.lfo_key_sync = bool(params["lfo_key_sync"])
        if "lfo_depth" in params:
            self.lfo_depth = max(0.0, min(1.0, float(params["lfo_depth"])))
        if "lfo_active" in params:
            self.lfo_active = bool(params["lfo_active"])
        if "lfo_shape" in params:
            s = str(params["lfo_shape"])
            if s in ("sine", "triangle", "square", "saw", "ramp", "peak", "sh"):
                self.lfo_shape = s
        if "lfo_invert" in params:
            self.lfo_invert = bool(params["lfo_invert"])
        if "lfo_offset_ms" in params:
            self.lfo_offset_ms = max(-500.0, min(500.0, float(params["lfo_offset_ms"])))
        if "lfo_haas_compensate" in params:
            self.lfo_haas_compensate = bool(params["lfo_haas_compensate"])
        if "lfo_smooth" in params:
            self.lfo_smooth = max(0.0, min(1.0, float(params["lfo_smooth"])))
        if "lfo_poly" in params:
            self.lfo_poly = bool(params["lfo_poly"])
        if "lfo_target" in params:
            t = str(params["lfo_target"])
            if t in ("filter", "amp", "pan", "bus") and t != self.lfo_target:
                # Multi-step transaction — render must not see the new target
                # against old smoother state mid-block.
                with self._render_lock:
                    self.lfo_target = t
                    # Reset filter cache so modulation takes over cleanly
                    self._filter_cutoff_last_set = -1.0
                    # Zero per-block ramp + smoothing state. Without this the new
                    # target's first block ramps from the old target's final mod
                    # value (and inherits the old target's smoother state),
                    # producing a one-block glitch or click when switching.
                    self._lfo_mod_a_last = 0.0
                    self._lfo_mod_b_last = 0.0
                    self._lfo_smooth_a_state = 0.0
                    self._lfo_smooth_b_state = 0.0
        if "lfo_spread" in params:
            self.lfo_spread = max(0.0, min(1.0, float(params["lfo_spread"])))

        # LFO 2 — parallel routing
        if "lfo2_rate_hz" in params:
            self.lfo2_rate_hz = max(0.05, min(20.0, float(params["lfo2_rate_hz"])))
        if "lfo2_rate_mode" in params:
            m = str(params["lfo2_rate_mode"])
            if m == "FREE" or m in self._DELAY_DIVISIONS:
                self.lfo2_rate_mode = m
        if "lfo2_rate_multiplier" in params:
            self.lfo2_rate_multiplier = max(0.1, min(10.0, float(params["lfo2_rate_multiplier"])))
        if "lfo2_key_sync" in params:
            self.lfo2_key_sync = bool(params["lfo2_key_sync"])
        if "lfo2_depth" in params:
            self.lfo2_depth = max(0.0, min(1.0, float(params["lfo2_depth"])))
        if "lfo2_active" in params:
            self.lfo2_active = bool(params["lfo2_active"])
        if "lfo2_shape" in params:
            s = str(params["lfo2_shape"])
            if s in ("sine", "triangle", "square", "saw", "ramp", "peak", "sh"):
                self.lfo2_shape = s
        if "lfo2_invert" in params:
            self.lfo2_invert = bool(params["lfo2_invert"])
        if "lfo2_offset_ms" in params:
            self.lfo2_offset_ms = max(-500.0, min(500.0, float(params["lfo2_offset_ms"])))
        if "lfo2_haas_compensate" in params:
            self.lfo2_haas_compensate = bool(params["lfo2_haas_compensate"])
        if "lfo2_smooth" in params:
            self.lfo2_smooth = max(0.0, min(1.0, float(params["lfo2_smooth"])))
        if "lfo2_poly" in params:
            self.lfo2_poly = bool(params["lfo2_poly"])
        if "lfo2_target" in params:
            t = str(params["lfo2_target"])
            if t in ("filter", "amp", "pan", "bus") and t != self.lfo2_target:
                # See lfo_target above — multi-step transaction.
                with self._render_lock:
                    self.lfo2_target = t
                    self._filter_cutoff_last_set = -1.0
                    self._lfo2_mod_a_last = 0.0
                    self._lfo2_mod_b_last = 0.0
                    self._lfo2_smooth_a_state = 0.0
                    self._lfo2_smooth_b_state = 0.0
        if "lfo2_spread" in params:
            self.lfo2_spread = max(0.0, min(1.0, float(params["lfo2_spread"])))
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
            self.delay_feedback = max(0.0, min(0.99, float(params["delay_feedback"])))
        if "delay_oblivion" in params:
            self.delay_oblivion = bool(params["delay_oblivion"])
        if "delay_rate_multiplier" in params:
            v = float(params["delay_rate_multiplier"])
            if v == 0:
                v = 1.0
            self.delay_rate_multiplier = max(-10.0, min(10.0, v))
        if "delay_wet" in params:
            self.delay_wet = max(0.0, min(1.0, float(params["delay_wet"])))
        if "delay_low_cut_hz" in params:
            self.delay_low_cut_hz = max(20.0, min(1000.0, float(params["delay_low_cut_hz"])))
        if "delay_high_cut_hz" in params:
            self.delay_high_cut_hz = max(500.0, min(20000.0, float(params["delay_high_cut_hz"])))
        if "delay_drive" in params:
            self.delay_drive = max(0.0, min(1.0, float(params["delay_drive"])))
        if "delay_width" in params:
            self.delay_width = max(0.0, min(1.0, float(params["delay_width"])))
        if "delay_mod_rate_hz" in params:
            self.delay_mod_rate_hz = max(0.05, min(8.0, float(params["delay_mod_rate_hz"])))
        if "delay_mod_depth_ms" in params:
            self.delay_mod_depth_ms = max(0.0, min(15.0, float(params["delay_mod_depth_ms"])))
        if "delay_reverse_amount" in params:
            self.delay_reverse_amount = max(0.0, min(1.0, float(params["delay_reverse_amount"])))
        if "delay_reverse_window_ms" in params:
            self.delay_reverse_window_ms = max(50.0, min(3000.0, float(params["delay_reverse_window_ms"])))
        if "delay_reverse_window_mode" in params:
            m = str(params["delay_reverse_window_mode"])
            if m == "FREE" or m in self._DELAY_DIVISIONS:
                self.delay_reverse_window_mode = m
        if "delay_reverse_feedback" in params:
            self.delay_reverse_feedback = max(0.0, min(0.7, float(params["delay_reverse_feedback"])))
        if "delay_aurora_enabled" in params:
            self.delay_aurora_enabled = bool(params["delay_aurora_enabled"])
        if "delay_aurora_seconds" in params:
            self.delay_aurora_seconds = max(3.0, min(15.0, float(params["delay_aurora_seconds"])))
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
        if "drone_level" in params:
            self.drone_level = max(0.0, min(1.0, float(params["drone_level"])))
        if "pad_mellow_enabled" in params:
            new_mellow = bool(params["pad_mellow_enabled"])
            if new_mellow and not self.pad_mellow_enabled:
                with self._render_lock:
                    self._pad_mellow_lp_l.reset()
                    self._pad_mellow_lp_r.reset()
                    self.pad_mellow_enabled = new_mellow
            else:
                self.pad_mellow_enabled = new_mellow
        if "pad_mellow_cutoff_hz" in params:
            self.pad_mellow_cutoff_hz = max(100.0, min(8000.0, float(params["pad_mellow_cutoff_hz"])))
