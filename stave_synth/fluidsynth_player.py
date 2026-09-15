"""FluidSynth player: manages FluidSynth for piano/e-piano soundfont playback."""

import logging
import math
import os
import threading
import time
from collections import deque

try:
    import fluidsynth
except ImportError:
    fluidsynth = None

import numpy as np

from pathlib import Path

from .config import SOUNDFONT_DIR, SAMPLE_RATE, LOW_RAM_MODE
from .config import USE_FAUST_PIANO_CHAIN

logger = logging.getLogger(__name__)

_MIDI_EVENT_QUEUE_CAPACITY = 256
_MIDI_EVENT_RENDER_BATCH_MAX = 64
_LOCK_DIAGNOSTIC_CAPACITY = 64
_LOCK_DIAGNOSTIC_OPERATIONS = {
    "sfload", "program_change", "all_notes_off", "shutdown",
}

# Soundfont presets: what the UI dropdown actually lists. Each preset maps
# a user-facing name → the underlying .sf2/.sf3 file stem + optional tremolo
# effect. Two presets can share the same file (e.g. "Rhodes" and "Suitcase"
# both load FluidR3_GM program 4; Suitcase layers the Rhodes-Suitcase
# tremolo on top, which is what acoustically distinguishes the two models).
#
# A preset is only shown in the dropdown when its `file` exists in
# SOUNDFONT_DIR — keeps the UI honest about what's actually installed.
SOUNDFONT_PRESETS = {
    # `program` is the GM program number inside the underlying SF2. Salamander
    # and FluidR3_GM put the acoustic grand at program 0; Rhodes/Suitcase
    # both use FluidR3_GM's GM "Electric Piano 1" at program 4. Getting this
    # right per-preset is essential — the old code relied on a stored
    # `sound` state key and broke when switching between files with
    # different program layouts.
    #
    # `velocity_curve` (default 1.0) is an exponential velocity bias. 1.0
    # keeps response linear; a higher value would bias toward a soundfont's
    # hard/top layer. FluidR3_GM's EP1 sits well at 1.0, so all current
    # presets stay neutral.
    "Salamander": {"file": "Salamander", "program": 0, "tremolo_hz": 0.0, "tremolo_depth": 0.0, "velocity_curve": 1.0},
    "Fluid":      {"file": "FluidR3_GM", "program": 0, "tremolo_hz": 0.0, "tremolo_depth": 0.0, "velocity_curve": 1.0},
    # Rhodes/Suitcase both sourced from FluidR3_GM's GM Electric Piano 1
    # (program 4). Cleaner and more "classic Rhodes" than the Pianoteq-sampled
    # Rhodes.sf2 which had a hot barky top layer that slammed into distortion
    # and a bell tone that clashed with the acoustic voicing's 2.8kHz cut.
    "Rhodes":     {"file": "FluidR3_GM", "program": 4, "tremolo_hz": 0.0, "tremolo_depth": 0.0, "velocity_curve": 1.0},
    "Suitcase":   {"file": "FluidR3_GM", "program": 4, "tremolo_hz": 5.5, "tremolo_depth": 0.50, "velocity_curve": 1.0},
}

# Small-Pi profile (see config.LOW_RAM_MODE): Salamander's 1.2 GB cannot be
# resident on a 2 GB box. Drop it from the presets so the remaining FluidR3_GM
# bank can stay fully resident without that cost. This removes Salamander from
# the preload loop AND the UI dropdown in one place. config.load_state remaps
# any saved "Salamander" selection to "Fluid" on these boxes.
if LOW_RAM_MODE:
    SOUNDFONT_PRESETS = {k: v for k, v in SOUNDFONT_PRESETS.items()
                         if v["file"] != "Salamander"}


# Voicing presets: pure TONE shaping (4-band EQ + low/high cuts). Independent
# of the Sound dropdown — voicings never change GM program. That separation
# lets the player stack any sound (Rhodes, DX7, etc.) with any voicing.
#
# `acoustic_grand` defaults encode the published Salamander correction curve
# (AKG C414 close-mic @ ~12cm over Yamaha C5 strings is forward in the
# 2–3 kHz band and a little thin in the low-mids). Other voicings layer
# character on top of that base.
#
# Bands are ordered low → high (convention). Each: freq_hz, gain_db, q.
PIANO_VOICINGS = {
    # Baseline Salamander correction (flat grand character). The reference
    # chain is a Yamaha C5 recorded with two AKG C414s in AB position 12 cm
    # above the strings — close-mic'd, no room capture. That chain has three
    # fingerprints we correct for: hot 2.5-3 kHz (C414 presence × close-mic
    # hammer strike), low-mid buildup 200-400 Hz (AB spaced pair proximity),
    # and short "air" above 10 kHz (no room tail). See project_piano_voicings
    # memory for the full rationale.
    "acoustic": {
        "lowcut_hz": 40.0, "highcut_hz": 20000.0,
        "bands": [
            (180.0,    1.5, 0.7),   # body/warmth, gentle wide Q
            (250.0,   -2.0, 1.0),   # textbook Salamander mud cut
            (2800.0,  -2.0, 1.2),   # tame close-mic presence (gentler than before)
            (12000.0,  1.5, 0.7),   # LIFT air (was -1.5 @ 10k — flipped to restore room feel)
        ],
    },
    # Forward + airy — sparkly studio feel. Air now lives up at 13k where
    # real air lives, not at 10k where upper-mid sits.
    "bright": {
        "lowcut_hz": 50.0, "highcut_hz": 20000.0,
        "bands": [
            (100.0,    0.5, 0.8),
            (300.0,   -1.5, 1.0),
            (4000.0,   2.0, 1.2),   # softened from +2.5 to keep from compounding with shared filter
            (13000.0,  2.5, 0.7),   # air, not upper-mid
        ],
    },
    # Rolled top, sweet top-mids. Not dark — just soft. Gentler 2.8k cut +
    # a small 5k dip replaces the old -4 @ 2.8k / -2 @ 7k / LP9k stack,
    # which was scooping the piano into "under-a-blanket" territory.
    "mellow": {
        "lowcut_hz": 40.0, "highcut_hz": 10000.0,
        "bands": [
            (150.0,    1.0, 0.8),
            (250.0,   -1.5, 1.0),
            (2800.0,  -2.5, 1.2),
            (5000.0,  -2.5, 1.0),
        ],
    },
    # Fat low-mid body, gentle top. Rewritten to body-boost + low-mid cut,
    # the classic "warm piano" shape. The old +3 @ 200 + +1 @ 500 was a
    # broad low boost that bloomed into muddy territory.
    "warm": {
        "lowcut_hz": 50.0, "highcut_hz": 14000.0,
        "bands": [
            (180.0,    2.0, 0.8),   # body
            (700.0,   -1.5, 1.0),   # CUT "wool" — key change
            (2800.0,  -2.0, 1.2),
            (10000.0, -1.5, 0.8),
        ],
    },
    # Heavy top roll + cut presence. Late-night/lounge. Old curve stacked
    # -5 @ 4.5k on top of LP 5k — double-cutting into muted-piano land.
    # New curve is dark from one coherent slope, still playable.
    "dark": {
        "lowcut_hz": 60.0, "highcut_hz": 6500.0,
        "bands": [
            (180.0,    2.0, 0.8),
            (300.0,   -1.0, 1.0),
            (2500.0,  -2.5, 1.3),
            (4000.0,  -3.5, 1.0),
        ],
    },
    # Honky mid-forward with narrow band — "old upright". HP was 90 Hz
    # (aggressive); pulled back to 70 to keep low fundamentals. Tape-feel
    # top rolloff pushed harder for more character.
    "vintage": {
        "lowcut_hz": 70.0, "highcut_hz": 9000.0,
        "bands": [
            (200.0,   -1.0, 1.0),
            (500.0,    2.5, 1.2),   # signature "honk" of vintage recordings
            (3000.0,  -2.5, 1.2),
            (6500.0,  -2.5, 0.9),
        ],
    },
    # Tight low punch, crisp attack — live performance tone. Low lift
    # moved up to 150 Hz (stays out of bass-guitar fundamentals) and a
    # deeper 350 Hz scoop gives the classic "cut through the mix" shape.
    "stage": {
        "lowcut_hz": 70.0, "highcut_hz": 18000.0,
        "bands": [
            (150.0,    1.5, 0.8),
            (350.0,   -2.0, 1.0),
            (2500.0,  -1.5, 1.2),
            (6000.0,   2.0, 1.0),
        ],
    },
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
        # Placeholder only — always overwritten by start()'s startup resolve.
        self.current_soundfont = ""
        self.reverb_dry_wet = 0.4
        self._lock = threading.RLock()
        # Musical FluidSynth calls are owned by the render thread.  The JACK
        # MIDI reader only appends small commands here, so a chord cannot hold
        # _lock while render_block() is trying to produce the next audio block.
        # The event lock is never waited on by the render thread.
        self._midi_event_lock = threading.Lock()
        self._midi_events = deque()
        self._render_owner_attached = False
        self._program_request = None
        self._midi_recovery_pending = False
        self._midi_recovery_failed_latched = False
        self._midi_events_enqueued = 0
        self._midi_events_applied = 0
        self._midi_event_overflows = 0
        self._midi_event_recoveries = 0
        self._midi_events_discarded = 0
        self._midi_queue_lock_deferrals = 0
        self._midi_status_lock_deferrals = 0
        self._native_render_lock_misses = 0
        self._midi_native_errors = 0
        self._midi_recovery_failures = 0
        self._midi_noteoff_unmatched = 0
        self._diagnostics_enabled = os.environ.get("STAVE_DIAGNOSTICS") == "1"
        self._diagnostic_lock = threading.Lock()
        self._diagnostic_native_owner = None
        self._diagnostic_owner_generation = 0
        self._diagnostic_render_block_index = 0
        self._diagnostic_lock_misses = (
            deque(maxlen=_LOCK_DIAGNOSTIC_CAPACITY)
            if self._diagnostics_enabled else None
        )
        self._diagnostic_records_overwritten = 0
        self._diagnostic_records_dropped = 0
        self._piano_room_lock = threading.RLock()
        self._preload_stop = threading.Event()
        self._preload_thread = None
        self._closing = False

        # Our own DSP chain — 24dB/oct (cascaded biquads) for audible piano EQ
        from .synth_engine import BiquadLowpass, BiquadHighpass, BiquadPeakingEQ
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

        # 4-band parametric EQ (pre-comp). Bands are shipped with the
        # Salamander correction curve (forum/KVR consensus: close-mic C414
        # on a C5 is hot at 2-3 kHz, a bit thin 120-180 Hz). Each band is a
        # peaking/bell biquad; wide Q on band 4 approximates a high shelf.
        # The `voicing` dropdown in the UI re-applies all four bands at
        # once, so these defaults are just the initial state before the
        # user (or a voicing) overwrites them.
        self.eq_bands = [
            {"freq_hz": 150.0,   "gain_db":  2.0, "q": 0.8, "enabled": True},
            {"freq_hz": 300.0,   "gain_db": -2.5, "q": 1.0, "enabled": True},
            {"freq_hz": 2800.0,  "gain_db": -3.0, "q": 1.5, "enabled": True},
            {"freq_hz": 10000.0, "gain_db": -1.5, "q": 0.7, "enabled": True},
        ]
        self.eq_filters_l = [BiquadPeakingEQ(b["freq_hz"], b["gain_db"], b["q"], sample_rate)
                             for b in self.eq_bands]
        self.eq_filters_r = [BiquadPeakingEQ(b["freq_hz"], b["gain_db"], b["q"], sample_rate)
                             for b in self.eq_bands]
        self.current_voicing = "acoustic"
        # Tremolo for Suitcase-preset character. Ring-buffer phase counter;
        # sinusoidal amp mod applied per sample in render_block. depth=0 = bypass.
        self.tremolo_hz = 0.0
        self.tremolo_depth = 0.0
        self._tremolo_phase = 0.0
        # Per-preset velocity bias. 1.0 = linear (pass-through). >1.0 applies
        # an exponential curve (vel ^ 1/curve) that lifts mid velocities into
        # a soundfont's hard/top layer while keeping max velocity at 1.0 — no
        # clipping and no "slam every note" overkill.
        self.velocity_curve = 1.0

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

        # ── Piano-room reverb (replaces FluidSynth's legacy Schroeder) ──
        # Loaded lazily on start() so construction cost is paid once, and
        # any CFFI/.so load failure surfaces there rather than at import.
        self._piano_room = None
        self.piano_room_enabled = True
        self._piano_room_was_enabled = True
        # reverb_dry_wet (already defined above) doubles as the piano_room
        # wet level — same semantic, same range, just a different algorithm
        # behind the fader. No preset migration needed.
        self._piano_room_wet_cur = float(self.reverb_dry_wet)

        # ── Velocity-aware brightness ──
        # Tracks a smoothed "recent velocity" level (0..1) updated on every
        # note_on. In render_block we apply a dynamic lowpass whose cutoff
        # is a linear function of the tracker — soft notes roll off top, hard
        # notes sparkle. Off by default so existing presets sound unchanged.
        self.vel_bright_enabled = False
        self.vel_bright_amount = 0.5     # 0 = off, 1 = maximum 1.5kHz-18kHz sweep
        self._vel_tracker = 0.7          # smoothed recent velocity (0..1)
        self._vel_tracker_cur = 0.7      # per-block interpolated value
        self._vel_bright_filter_l = BiquadLowpass(18000.0, 0.707, sample_rate)
        self._vel_bright_filter_r = BiquadLowpass(18000.0, 0.707, sample_rate)
        self._vel_bright_last_cutoff = 18000.0

        # ── Faust piano chain (Phase 3, FAUST_PORT_PLAN.md) ──
        # Volume gain + low/high-cut cascades + 4-band EQ run as one Faust
        # module; the LA-2A comp (block-rate envelope) + velocity
        # brightness + tremolo + piano-room mix stay in Python. Flag OFF
        # by default — the Python DSP path below is byte-identical.
        # Module-global read (not config.) so the parity harness can flip
        # it per instance, same pattern as synth_engine's flags.
        self._faust_chain = None
        if USE_FAUST_PIANO_CHAIN:
            try:
                from .faust_piano_chain import FaustPianoChain
                self._faust_chain = FaustPianoChain(sample_rate)
                logger.info("Faust piano chain enabled")
            except Exception as e:
                logger.warning("Faust piano chain unavailable, Python DSP "
                               "path stays: %s", e)
                self._faust_chain = None

    def _diagnostic_owner_enter(self, operation: str):
        """Publish native ownership only after the real lock is acquired."""
        if not getattr(self, "_diagnostics_enabled", False):
            return None
        self._diagnostic_owner_generation += 1
        token = (
            operation if operation in _LOCK_DIAGNOSTIC_OPERATIONS else "unknown",
            time.monotonic_ns(),
            self._diagnostic_owner_generation,
        )
        previous = self._diagnostic_native_owner
        self._diagnostic_native_owner = token
        return token, previous

    def _diagnostic_owner_exit(self, context) -> None:
        """Clear/restore only the owner token installed by this context."""
        if context is None:
            return
        token, previous = context
        if self._diagnostic_native_owner is token:
            self._diagnostic_native_owner = previous

    def _record_native_lock_miss(self, block_index: int, owner_before) -> None:
        """Record one conservative attribution without waiting on diagnostics."""
        if not getattr(self, "_diagnostics_enabled", False):
            return
        owner_after = self._diagnostic_native_owner
        now_ns = time.monotonic_ns()
        if owner_before is not None and owner_before is owner_after:
            operation, acquired_ns, generation = owner_before
        else:
            operation, acquired_ns, generation = "unknown", None, None
        record = (now_ns, block_index, operation, acquired_ns, generation)
        if not self._diagnostic_lock.acquire(blocking=False):
            self._diagnostic_records_dropped += 1
            return
        try:
            records = self._diagnostic_lock_misses
            if len(records) == records.maxlen:
                self._diagnostic_records_overwritten += 1
            records.append(record)
        finally:
            self._diagnostic_lock.release()

    def _diagnostic_snapshot(self) -> dict:
        """Copy bounded diagnostic state; render only ever try-locks this."""
        if not getattr(self, "_diagnostics_enabled", False):
            return {
                "enabled": False,
                "capacity": 64,
                "records": [],
                "overwritten": 0,
                "dropped": 0,
                "current_owner": None,
            }
        with self._diagnostic_lock:
            records = list(self._diagnostic_lock_misses)
            overwritten = self._diagnostic_records_overwritten
            dropped = self._diagnostic_records_dropped
        owner_before = self._diagnostic_native_owner
        owner_after = self._diagnostic_native_owner
        owner = owner_before if owner_before is owner_after else None
        current_owner = None
        if owner is not None:
            current_owner = {
                "operation": owner[0],
                "acquired_monotonic_ns": owner[1],
                "generation": owner[2],
            }
        return {
            "enabled": True,
            "capacity": self._diagnostic_lock_misses.maxlen,
            "records": [
                {
                    "monotonic_ns": record[0],
                    "render_block_index": record[1],
                    "native_owner_operation": record[2],
                    "owner_acquired_monotonic_ns": record[3],
                    "owner_generation": record[4],
                }
                for record in records
            ],
            "overwritten": overwritten,
            "dropped": dropped,
            "current_owner": current_owner,
        }

    def start(self, soundfont_name: str = "Salamander"):
        """Initialize FluidSynth and PRE-LOAD every available soundfont so
        mid-set preset switching performs no sample loading or unloading.

        Audio is rendered via render_block() — no JACK driver needed.

        Pre-loading all soundfonts at startup trades a few seconds of
        boot time for instant live switching. Previously `set_soundfont`
        called `sfunload` + `sfload` under the render lock — a Salamander
        reload is hundreds of ms of cold disk read and dropped audio mid-song.
        With every supported preset's bank resident in memory, program changes
        do not allocate or unload samples. The remaining program_select call is
        still native work and must be qualified on the target.

        The full profile may retain Salamander (~1.2 GB) plus FluidR3_GM
        (~150 MB). LOW_RAM_MODE excludes Salamander and retains FluidR3_GM;
        the target's actual resident-memory cost must be verified before release.
        """
        self._closing = False
        self._preload_stop.clear()
        self.fs = fluidsynth.Synth(samplerate=float(self.sample_rate))

        self._configure_native_settings()

        # Configure pitch range after the core synth settings. Gain is 1.0
        # because our pipeline handles volume.
        # FluidSynth's own 1990s Schroeder reverb is DISABLED — piano-room
        # colour now comes from our dedicated Faust Dattorro (self._piano_room)
        # which runs in our Python pipeline and mixes properly with the rest
        # of the chain. Chorus stays off.
        # Pin pitch bend range to ±2 semitones via RPN so it matches the pad.
        # Default range varies by soundfont; without this the piano can bend
        # a different amount than the pad → user hears "out of tune" between
        # them. Sequence: select RPN 0 (Pitch Bend Sensitivity) via CC101/CC100,
        # then write data via CC6/CC38, then "null" the RPN selector with
        # CC101=127 / CC100=127 so a stray data-entry CC later doesn't drift it.
        try:
            self.fs.cc(0, 101, 0)    # RPN MSB
            self.fs.cc(0, 100, 0)    # RPN LSB → Pitch Bend Sensitivity
            self.fs.cc(0, 6, 2)      # Data MSB → 2 semitones
            self.fs.cc(0, 38, 0)     # Data LSB → 0 cents
            self.fs.cc(0, 101, 127)  # RPN null
            self.fs.cc(0, 100, 127)  # RPN null
        except Exception as e:
            logger.debug("FluidSynth pitch-bend range RPN failed: %s", e)

        # Load piano-room reverb .so. Lazy import so a broken build still
        # starts (piano just won't have room reverb).
        try:
            from .faust_piano_room import FaustPianoRoom
            self._piano_room = FaustPianoRoom(self.sample_rate)
        except Exception as e:
            logger.warning("Piano-room reverb unavailable: %s", e)
            self._piano_room = None

        # ─── Load the STARTUP bank first; preload the rest before READY ───
        # Salamander is 1.2GB and dominates cold-boot time (~3-6s of disk
        # read). Loading only the startup preset up-front and lazy-loading
        # the others on a background thread cuts boot to first-note from
        # ~25s → ~17-20s for typical worship use. Startup waits for the bounded
        # background preload before JACK starts, so no supported bank remains
        # pending once the instrument advertises readiness.
        self._sfid_by_file = {}         # preset["file"] → sfid
        _sfid_by_path = {}              # resolved abs path → sfid (dedup)
        # Resolve the startup file_key first so we know which one to preload
        # synchronously. (Rest of the resolve logic happens below for it too.)
        startup_preset = SOUNDFONT_PRESETS.get(soundfont_name)
        startup_file = startup_preset["file"] if startup_preset else soundfont_name

        def _load_one(file_key, allow_fallback=True):
            """Load one soundfont without racing native render or shutdown."""
            if self._preload_stop.is_set():
                return
            sf_path = (self._find_soundfont(file_key) if allow_fallback
                       else self._find_soundfont_exact(file_key))
            if sf_path is None:
                logger.warning("Soundfont not found for file='%s' — "
                               "preset(s) using it will be unavailable", file_key)
                return
            path_str = str(sf_path)
            with self._lock:
                diagnostic_enter = getattr(self, "_diagnostic_owner_enter", None)
                diagnostic_owner = (
                    diagnostic_enter("sfload") if diagnostic_enter else None
                )
                try:
                    if (self._closing or self._preload_stop.is_set()
                            or self.fs is None):
                        return
                    if file_key in self._sfid_by_file:
                        return
                    if path_str in _sfid_by_path:
                        self._sfid_by_file[file_key] = _sfid_by_path[path_str]
                        return
                    try:
                        sfid = self.fs.sfload(path_str)
                    except Exception as e:
                        logger.error("sfload crashed for %s: %s", path_str, e)
                        return
                    if sfid < 0:
                        logger.warning("sfload returned %d for %s", sfid, path_str)
                        return
                    self._sfid_by_file[file_key] = sfid
                    _sfid_by_path[path_str] = sfid
                finally:
                    diagnostic_exit = getattr(self, "_diagnostic_owner_exit", None)
                    if diagnostic_exit:
                        diagnostic_exit(diagnostic_owner)
            logger.info("Preloaded soundfont file='%s' path='%s' sfid=%d",
                         file_key, path_str, sfid)

        # Sync: just the startup font. Anything that happens to share its
        # file (e.g. Rhodes+Suitcase both FluidR3_GM) gets a free ride.
        _load_one(startup_file)

        # Background: everything else. Daemon thread so it doesn't block
        # shutdown. Errors logged but never crash the audio engine.
        def _bg_preload_rest():
            try:
                for preset_name, preset in SOUNDFONT_PRESETS.items():
                    if self._preload_stop.is_set():
                        return
                    # A missing secondary preset must stay unavailable rather
                    # than caching another font under its key and selecting a
                    # program number that belongs to the missing bank.
                    _load_one(preset["file"], allow_fallback=False)
                if not self._preload_stop.is_set():
                    logger.info("Background soundfont preload complete")
            except Exception as e:
                logger.warning("Background soundfont preload error: %s", e)
        self._preload_thread = threading.Thread(
            target=_bg_preload_rest, daemon=True, name="sf-preload"
        )

        # Resolve the startup name: if it's a preset key, grab the preset's
        # file (and tremolo config). Otherwise treat as a direct file stem
        # (legacy + fallback chain).
        preset_name = soundfont_name if soundfont_name in SOUNDFONT_PRESETS else None
        if preset_name is not None:
            preset = SOUNDFONT_PRESETS[preset_name]
            file_stem = preset["file"]
            startup_program = int(preset.get("program", 0))
            self.tremolo_hz = float(preset.get("tremolo_hz", 0.0))
            self.tremolo_depth = float(preset.get("tremolo_depth", 0.0))
            self.velocity_curve = float(preset.get("velocity_curve", 1.0))
        else:
            file_stem = soundfont_name
            startup_program = 0

        self.current_soundfont = preset_name or soundfont_name

        # Prefer the pre-loaded sfid for the requested startup preset.
        startup_sfid = self._sfid_by_file.get(file_stem)
        if startup_sfid is not None:
            self.sfid = startup_sfid
            self._loaded_file = file_stem
            self._program_select_checked(
                self.sfid, startup_program, f"startup preset '{self.current_soundfont}'"
            )
            logger.info("Startup soundfont: preset=%s file=%s prog=%d id=%d",
                         self.current_soundfont, file_stem, startup_program, self.sfid)
        elif file_stem and preset_name is None:
            # Legacy path: caller asked for a direct file stem that isn't in
            # SOUNDFONT_PRESETS. Fall through to a one-off sfload so existing
            # installs with ad-hoc soundfont names still boot.
            sf_path = self._find_soundfont(file_stem)
            if sf_path:
                self.sfid = self.fs.sfload(str(sf_path))
                if self.sfid >= 0:
                    self._loaded_file = Path(sf_path).stem
                    self.current_soundfont = self._loaded_file
                    self._program_select_checked(
                        self.sfid, startup_program,
                        f"legacy startup soundfont '{self.current_soundfont}'",
                    )
                    # Register in cache so subsequent switches are instant too.
                    self._sfid_by_file[file_stem] = self.sfid
                    logger.info("Loaded soundfont (legacy path): %s id=%d",
                                 sf_path, self.sfid)
                else:
                    logger.error("Failed to load soundfont: %s — piano disabled", sf_path)
                    self.sfid = None
                    self.enabled = False
            else:
                logger.error("No soundfont found (tried %s + fallbacks) — piano disabled",
                              file_stem)
                self.enabled = False
        else:
            logger.error("Startup preset '%s' not pre-loaded — piano disabled",
                          self.current_soundfont)
            self.sfid = None
            self.enabled = False

        if self.sfid is not None:
            logger.info("FluidSynth started (rendered in Python pipeline) — %d soundfont(s) resident",
                         len(self._sfid_by_file))
        else:
            logger.warning("FluidSynth started but no soundfont loaded — piano will be silent")

        # Start asynchronous loads only after the startup program has been
        # selected. All native calls share _lock with render and shutdown.
        self._preload_thread.start()

    def _configure_native_settings(self) -> None:
        """Set allocation-sensitive FluidSynth policy before the first sfload."""
        try:
            # FluidSynth documents dynamic sample loading as unsuitable for
            # real-time program changes: selecting a program can allocate and
            # unload samples. Keep the supported bank resident on every profile.
            # Zero is also FluidSynth's normal default, made explicit here so a
            # system/user configuration cannot silently change the live policy.
            self.fs.setting("synth.dynamic-sample-loading", 0)
            dynamic_loading = self.fs.get_setting(
                "synth.dynamic-sample-loading"
            )
            if type(dynamic_loading) is not int or dynamic_loading != 0:
                raise RuntimeError(
                    "FluidSynth dynamic-sample-loading readback is not disabled"
                )
        except Exception as exc:
            raise RuntimeError(
                "FluidSynth full-bank residency could not be configured"
            ) from exc
        self.fs.setting("synth.polyphony", 32 if LOW_RAM_MODE else 64)
        self.fs.setting("synth.gain", 1.0)
        self.fs.setting("synth.reverb.active", 0)
        self.fs.setting("synth.chorus.active", 0)

    def wait_for_preload(self, timeout: float = 30.0) -> bool:
        """Wait a bounded time for all background soundfont loads to finish.

        A False result leaves the loader and FluidSynth object owned and live;
        the caller must abort startup and use stop() for coordinated teardown.
        """
        preload = self._preload_thread
        if preload is None or not preload.is_alive():
            return True
        if preload is threading.current_thread():
            logger.error("Soundfont preload cannot wait for itself")
            return False
        preload.join(timeout=max(0.0, float(timeout)))
        if preload.is_alive():
            logger.error("Soundfont preload did not finish within %.1f seconds", timeout)
            return False
        return True

    def _find_soundfont_exact(self, name: str):
        """Search one name without substituting a different bank."""
        import os
        system_dirs = [
            "/usr/share/sounds/sf2",
            "/usr/share/soundfonts",
            "/usr/local/share/soundfonts",
        ]
        for ext in (".sf2", ".sf3", ".SF2", ".SF3"):
            path = SOUNDFONT_DIR / f"{name}{ext}"
            if path.exists():
                return path
        for directory in system_dirs:
            for ext in (".sf2", ".sf3"):
                path = os.path.join(directory, f"{name}{ext}")
                if os.path.exists(path):
                    return path
        return None

    def _program_select_checked(self, sfid: int, program: int, context: str) -> None:
        """Select one native program or raise without acknowledging new state."""
        try:
            status = self.fs.program_select(0, int(sfid), 0, int(program))
        except Exception as exc:
            raise RuntimeError(f"FluidSynth program selection failed for {context}") from exc
        # pyfluidsynth versions normally return FluidSynth's integer status;
        # tolerate None for bindings that expose this C call as void.
        if status not in (None, 0):
            raise RuntimeError(
                f"FluidSynth program selection failed for {context} (status {status})"
            )

    def _find_soundfont(self, name: str):
        """Search the requested name then a finite startup fallback list."""
        candidates = [name]
        candidates.extend(fb for fb in ("Salamander", "FluidR3_GM", "default-GM")
                          if fb != name)
        for index, candidate in enumerate(candidates):
            if index:
                logger.info("Trying fallback soundfont: %s", candidate)
            result = self._find_soundfont_exact(candidate)
            if result is not None:
                return result
        return None

    def note_on(self, note: int, velocity: float):
        """Play a note."""
        if self._closing or not self.enabled or self.fs is None:
            logger.debug("note_on skipped (enabled=%s, fs=%s)", self.enabled, self.fs is not None)
            return
        # Per-preset velocity curve — exponential bias that pushes mid-
        # velocity notes into a soundfont's hard/top layer without clipping
        # at 127. At curve=1.0 this is identity; curve=1.5 maps vel 0.5 → 0.63,
        # and max velocity still maps to 1.0 (no clipping, no "always slam").
        # Shape on the owner, so notes after a queued program boundary use
        # that program's curve even while its UI acknowledgement is pending.
        self._queue_midi_event("note_on", int(note), 0, float(velocity))

    def note_off(self, note: int):
        """Release a note."""
        if self._closing or self.fs is None:
            return
        self._queue_midi_event("note_off", int(note), 0, 0.0)

    def _queue_midi_event(self, kind: str, note: int, value: int,
                          velocity: float) -> bool:
        """Append one bounded musical command for the render owner.

        Once overflow makes the batch ambiguous, all later commands are
        rejected until render applies a fail-safe release.  This prevents a
        stale note-on from surviving after its matching note-off was lost.
        """
        if self._closing or self.fs is None:
            return False
        with self._midi_event_lock:
            if self._closing or self.fs is None:
                return False
            if kind != "note_off" and not self.enabled:
                return False
            if (self._midi_recovery_pending
                    or self._midi_recovery_failed_latched):
                self._midi_events_discarded += 1
                return False
            if len(self._midi_events) >= _MIDI_EVENT_QUEUE_CAPACITY:
                self._finish_program_requests(self._midi_events, "MIDI overflow cancelled program switch")
                self._midi_events_discarded += len(self._midi_events) + 1
                self._midi_events.clear()
                self._midi_recovery_pending = True
                self._midi_event_overflows += 1
                return False
            self._midi_events.append((kind, note, value, velocity))
            self._midi_events_enqueued += 1
            return True

    def _take_midi_batch_locked(self, *, blocking: bool = False,
                                limit: int | None = _MIDI_EVENT_RENDER_BATCH_MAX):
        """Take pending commands while the caller owns the native lock."""
        if not self._midi_event_lock.acquire(blocking=blocking):
            self._midi_queue_lock_deferrals += 1
            return None, False
        try:
            recovery = (self._midi_recovery_pending
                        or self._midi_recovery_failed_latched)
            self._midi_recovery_pending = False
            if recovery:
                self._finish_program_requests(self._midi_events, "MIDI recovery cancelled program switch")
                self._midi_events_discarded += len(self._midi_events)
                self._midi_events.clear()
                return (), True
            take = len(self._midi_events) if limit is None else min(
                len(self._midi_events), limit
            )
            batch = tuple(self._midi_events.popleft() for _ in range(take))
            # Claim while holding the same admission/cancellation lock. Once
            # detached, a native operation cannot be cancelled by a timeout.
            for kind, request, _value, _velocity in batch:
                if kind == "program" and request["state"] == "pending":
                    request["state"] = "claimed"
            return batch, False
        finally:
            self._midi_event_lock.release()

    def _native_call_checked(self, label: str, function, *args,
                             unmatched_noteoff_ok: bool = False) -> bool:
        """Call one FluidSynth API and account for exceptions/status errors."""
        try:
            status = function(*args)
            if status not in (None, 0):
                # FluidSynth reports FLUID_FAILED when noteoff finds no
                # matching voice. Duplicate releases and a 128-note panic
                # legitimately hit that case; it is not native corruption.
                if unmatched_noteoff_ok:
                    self._midi_noteoff_unmatched += 1
                    return True
                raise RuntimeError(f"status {status}")
            return True
        except Exception as exc:
            self._midi_native_errors += 1
            errors = self._midi_native_errors
            # A persistent native fault retries fail-silent every render.
            # Log the first few and powers of two; counters retain every
            # failure without turning diagnostics into an audio-thread flood.
            if errors <= 4 or (errors & (errors - 1)) == 0:
                logger.error("FluidSynth %s failed (#%d): %s",
                             label, errors, exc)
            return False

    def _release_all_native_locked(self) -> bool:
        """Best-effort complete native release; never stop at the first fault."""
        ok = True
        ok = self._native_call_checked("sustain release", self.fs.cc,
                                       0, 64, 0) and ok
        ok = self._native_call_checked("sostenuto release", self.fs.cc,
                                       0, 66, 0) and ok
        for note in range(128):
            ok = self._native_call_checked(
                f"note-off {note}", self.fs.noteoff, 0, note,
                unmatched_noteoff_ok=True,
            ) and ok
        ok = self._native_call_checked("pitch-bend reset", self.fs.pitch_bend,
                                       0, 0) and ok
        return ok

    def _recover_midi_native_locked(self) -> bool:
        """Apply one compact fail-safe release, retrying on later renders."""
        ok = True
        for label, controller in (("sustain release", 64),
                                  ("sostenuto release", 66),
                                  ("all-notes-off", 123)):
            ok = self._native_call_checked(
                label, self.fs.cc, 0, controller, 0
            ) and ok
        ok = self._native_call_checked(
            "pitch-bend reset", self.fs.pitch_bend, 0, 0
        ) and ok
        if ok:
            self._active_notes = 0
            self._midi_event_recoveries += 1
            self._midi_recovery_failed_latched = False
            return True

        self._midi_recovery_failures += 1
        # This render-owned latch closes admission and keeps output fail-silent
        # without ever waiting for a producer that holds the event lock.
        self._midi_recovery_failed_latched = True
        if self._midi_event_lock.acquire(blocking=False):
            try:
                self._finish_program_requests(self._midi_events, "Native MIDI recovery failed")
                self._midi_events_discarded += len(self._midi_events)
                self._midi_events.clear()
                self._midi_recovery_pending = True
            finally:
                self._midi_event_lock.release()
        else:
            self._midi_queue_lock_deferrals += 1
        return False

    def _apply_midi_batch_locked(self, batch, recovery: bool) -> bool:
        """Apply commands on the render owner immediately before sampling."""
        try:
            return self._apply_midi_batch_impl_locked(batch, recovery)
        finally:
            # Even an unexpected Python failure before a detached marker must
            # resolve its waiter; a still-ticking render watchdog cannot do so.
            self._finish_program_requests(batch, "MIDI batch interrupted before program completion")

    def _apply_midi_batch_impl_locked(self, batch, recovery: bool) -> bool:
        if recovery:
            return self._recover_midi_native_locked()

        for index, (kind, note, value, velocity) in enumerate(batch):
            if kind == "note_on":
                shaped = velocity ** (1.0 / max(1.0, self.velocity_curve))
                value = max(1, min(127, int(shaped * 127)))
                ok = self._native_call_checked(
                    "note-on", self.fs.noteon, 0, note, value
                )
                if ok:
                    self._note_on_count += 1
                    self._active_notes += 1
                    self._silent_blocks = 0
                    self._vel_tracker = (
                        0.6 * self._vel_tracker + 0.4 * float(velocity)
                    )
                    logger.debug("PIANO note_on: note=%d vel=%d (count=%d)",
                                 note, value, self._note_on_count)
            elif kind == "note_off":
                ok = self._native_call_checked(
                    "note-off", self.fs.noteoff, 0, note,
                    unmatched_noteoff_ok=True,
                )
                if ok:
                    self._active_notes = max(0, self._active_notes - 1)
            elif kind == "pitch_bend":
                ok = self._native_call_checked(
                    "pitch bend", self.fs.pitch_bend, 0, value
                )
            elif kind == "program":
                ok = self._apply_program_request_locked(note)
            else:
                self._midi_native_errors += 1
                logger.error("Unknown FluidSynth MIDI event: %s", kind)
                ok = False
            if ok:
                self._midi_events_applied += 1
                continue

            # A failed note-off makes native ownership ambiguous.  Do not
            # continue playing the rest of this detached batch; release every
            # possible voice and make the loss visible in cumulative health.
            self._midi_events_discarded += len(batch) - index
            self._finish_program_requests(batch[index:], "MIDI batch failed before program switch")
            return self._recover_midi_native_locked()
        return True

    @staticmethod
    def _finish_program_requests(events, message):
        """Resolve discarded markers; caller owns their queue or detached batch."""
        for kind, request, _value, _velocity in events:
            if kind == "program" and request["state"] not in {"done", "cancelled"}:
                request["error"] = RuntimeError(message)
                request["state"] = "cancelled"
                request["done"].set()

    def set_render_owner_attached(self, attached):
        """Called only before producer start or after proven producer join."""
        with self._lock:
            self._render_owner_attached = bool(attached)
            if not attached:
                with self._midi_event_lock:
                    self._finish_program_requests(self._midi_events, "Audio owner stopped")
                    remaining = deque(event for event in self._midi_events if event[0] != "program")
                    self._midi_events_discarded += len(self._midi_events) - len(remaining)
                    self._midi_events = remaining

    def pump_render_controls(self):
        """Service piano program boundaries when organ/off owns routed audio.

        Only JACK's render thread calls this. Never wait for another native
        owner or queue producer; retry at the next render cycle instead.
        """
        request = getattr(self, "_program_request", None)
        if request is None or request["done"].is_set() or self._closing:
            return
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self.fs is not None:
                batch, recovery = self._take_midi_batch_locked()
                if batch is not None:
                    self._apply_midi_batch_locked(batch, recovery)
        finally:
            self._lock.release()

    def _apply_program_metadata(self, name, preset, sfid):
        self.tremolo_hz = float(preset.get("tremolo_hz", 0.0))
        self.tremolo_depth = float(preset.get("tremolo_depth", 0.0))
        if self.tremolo_depth <= 1e-4:
            self._tremolo_phase = 0.0
        self.velocity_curve = float(preset.get("velocity_curve", 1.0))
        self.sfid = sfid
        self.current_soundfont = name
        self._loaded_file = preset["file"]

    def _apply_program_request_locked(self, request):
        request["error"] = RuntimeError("Program owner stopped before completion")
        try:
            if request["state"] != "claimed" or self._closing or self.fs is None:
                raise RuntimeError("Program switch has no active audio owner")
            self._program_select_checked(request["sfid"], request["program"],
                                         f"preset '{request['name']}'")
            self._apply_program_metadata(request["name"], request["preset"], request["sfid"])
            request["error"] = None
            return True
        except Exception as exc:
            request["error"] = exc
            self._midi_native_errors += 1
            return False
        finally:
            request["state"] = "done"
            request["done"].set()

    def _request_program_switch(self, name, preset):
        """Synchronous acknowledgement of a FIFO, render-owned program change.

        A pending request has a one-second admission deadline. A claimed C
        call cannot be cancelled: await its definite result, leaving a wedged
        render to the existing service watchdog. Never return a rollback-able
        timeout while that native call could still change the sound later.
        """
        request = {"name": name, "preset": dict(preset), "program": int(preset.get("program", 0)),
                   "sfid": getattr(self, "_sfid_by_file", {}).get(preset["file"]),
                   "state": "pending", "done": threading.Event(), "error": None}
        if request["sfid"] is None:
            raise RuntimeError(f"Soundfont preset '{name}' is unavailable or still preloading")
        marker = ("program", request, 0, 0.0)
        with self._midi_event_lock:
            prior = getattr(self, "_program_request", None)
            if (self._closing or not self._render_owner_attached or self.fs is None
                    or self._midi_recovery_pending or self._midi_recovery_failed_latched
                    or len(self._midi_events) >= _MIDI_EVENT_QUEUE_CAPACITY
                    or (prior is not None and not prior["done"].is_set())):
                raise RuntimeError("Program switch rejected: audio owner is unavailable or busy")
            self._program_request = request
            self._midi_events.append(marker)
            self._midi_events_enqueued += 1
        if not request["done"].wait(1.0):
            with self._midi_event_lock:
                if request["state"] == "pending":
                    self._midi_events.remove(marker)
                    self._midi_events_discarded += 1
                    self._finish_program_requests((marker,), "Audio owner did not accept program switch")
            request["done"].wait()
        if request["error"] is not None:
            raise RuntimeError(f"Program switch to '{name}' failed: {request['error']}") from request["error"]

    def midi_render_status(self) -> dict:
        """Return cumulative bounded-queue telemetry for health reporting."""
        status_lock_contended = not self._midi_event_lock.acquire(blocking=False)
        if status_lock_contended:
            # CPython's deque length and object-reference reads occur under the
            # GIL.  They are a diagnostic approximation for this one snapshot;
            # never wedge a health request behind a failed producer.
            self._midi_status_lock_deferrals += 1
            queued = len(self._midi_events)
            recovery_pending = (self._midi_recovery_pending
                                or self._midi_recovery_failed_latched)
        else:
            try:
                queued = len(self._midi_events)
                recovery_pending = (self._midi_recovery_pending
                                    or self._midi_recovery_failed_latched)
            finally:
                self._midi_event_lock.release()
        request = getattr(self, "_program_request", None)
        program_state = request["state"] if request is not None else None
        return {
            "queue_capacity": _MIDI_EVENT_QUEUE_CAPACITY,
            "render_batch_max": _MIDI_EVENT_RENDER_BATCH_MAX,
            "queued": queued,
            "pending": queued + int(recovery_pending) + int(program_state == "claimed"),
            "program_state": program_state,
            "render_owner_attached": getattr(self, "_render_owner_attached", False),
            "recovery_pending": recovery_pending,
            "enqueued": self._midi_events_enqueued,
            "applied": self._midi_events_applied,
            "overflows": self._midi_event_overflows,
            "recoveries": self._midi_event_recoveries,
            "discarded": self._midi_events_discarded,
            "queue_lock_deferrals": self._midi_queue_lock_deferrals,
            "status_lock_deferrals": self._midi_status_lock_deferrals,
            "status_lock_contended": status_lock_contended,
            "native_render_lock_misses": self._native_render_lock_misses,
            "native_errors": self._midi_native_errors,
            "recovery_failures": self._midi_recovery_failures,
            "noteoff_unmatched": self._midi_noteoff_unmatched,
            "diagnostics": self._diagnostic_snapshot(),
        }

    def all_notes_off(self):
        """Silence all notes."""
        if self.fs is None:
            return
        with self._lock:
            diagnostic_enter = getattr(self, "_diagnostic_owner_enter", None)
            diagnostic_owner = (
                diagnostic_enter("all_notes_off") if diagnostic_enter else None
            )
            try:
                # Panic/instrument switching remains synchronous.  Cancel queued
                # events under the same native ownership so a pre-panic batch can
                # never be resurrected after the release and JACK ring clear.
                with self._midi_event_lock:
                    self._finish_program_requests(self._midi_events, "All-notes-off cancelled program switch")
                    self._midi_events_discarded += len(self._midi_events)
                    self._midi_events.clear()
                    self._midi_recovery_pending = False
                    self._midi_recovery_failed_latched = False
                release_ok = self._release_all_native_locked()
                if release_ok:
                    self._active_notes = 0
                else:
                    # Publish the unsafe latch before native ownership is released;
                    # the next render must not observe a falsely clean boundary.
                    self._midi_recovery_failures += 1
                    self._midi_recovery_failed_latched = True
                    logger.error("FluidSynth all-notes-off was incomplete")
            finally:
                diagnostic_exit = getattr(self, "_diagnostic_owner_exit", None)
                if diagnostic_exit:
                    diagnostic_exit(diagnostic_owner)
        # Reset comp state so the first hard chord after silence doesn't
        # ramp from a stale gain (LA-2A linear-interp between blocks would
        # otherwise pop). Cheap; only fires on panic / instrument-cycle.
        self._comp_envelope = 0.0
        if hasattr(self, "_prev_comp_gain"):
            self._prev_comp_gain = 1.0
        # Flush piano-room tank so panic truly silences (otherwise the tail
        # keeps ringing while piano voices are killed).
        if self._piano_room is not None:
            with self._piano_room_lock:
                self._piano_room.clear()
        if not release_ok:
            raise RuntimeError("FluidSynth all-notes-off was incomplete")
        return True

    def midi_callback(self, event_type: str, note: int, velocity: float):
        """Callback to be registered with JackEngine for MIDI forwarding."""
        if event_type == "note_on":
            self.note_on(note, velocity)
        elif event_type == "note_off":
            self.note_off(note)
        elif event_type == "all_notes_off":
            self.all_notes_off()
        elif event_type == "pitch_bend":
            # `note` carries the 14-bit pitch bend value (0..16383, center=8192).
            # pyFluidSynth's pitch_bend expects a SIGNED OFFSET from center
            # (range -8192..+8192) and adds 8192 internally before passing to
            # libfluidsynth. So we must subtract 8192 here. Earlier code passed
            # the raw 14-bit number → at "center" the API saw +8192 = max bend
            # up (+2 semis with our default range), which is the "piano plays D
            # when I hit C" bug.
            if not self._closing and self.fs is not None:
                self._queue_midi_event(
                    "pitch_bend", 0, int(note) - 8192, 0.0
                )

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
        if self._closing or self.fs is None:
            return np.zeros((2, n_samples), dtype=np.float64)
        if not self.enabled:
            if getattr(self, "_render_owner_attached", False):
                self.pump_render_controls()
            return np.zeros((2, n_samples), dtype=np.float64)

        # A cold background sfload can own FluidSynth for hundreds of ms.
        # Never make the render producer wait that long: emit a complete
        # silent block while startup preload owns the native instance.
        diagnostic_owner_before = None
        diagnostic_block_index = 0
        if getattr(self, "_diagnostics_enabled", False):
            self._diagnostic_render_block_index += 1
            diagnostic_block_index = self._diagnostic_render_block_index
            # This read belongs immediately before the real try-acquire.  A
            # stable identical token after failure is required for attribution.
            diagnostic_owner_before = self._diagnostic_native_owner
        if not self._lock.acquire(blocking=False):
            self._native_render_lock_misses += 1
            self._record_native_lock_miss(
                diagnostic_block_index, diagnostic_owner_before
            )
            return np.zeros((2, n_samples), dtype=np.float64)
        try:
            if self.fs is None:
                return np.zeros((2, n_samples), dtype=np.float64)
            batch, recovery = self._take_midi_batch_locked()
            events_deferred = batch is None
            if events_deferred and self._midi_recovery_failed_latched:
                return np.zeros((2, n_samples), dtype=np.float64)
            if not events_deferred:
                recovery_safe = self._apply_midi_batch_locked(batch, recovery)
                if not recovery_safe:
                    # Recovery failed and is latched for retry.  Do not emit
                    # potentially stuck native voices as apparently valid audio.
                    return np.zeros((2, n_samples), dtype=np.float64)

            # Drain commands before considering the idle optimization.  If a
            # producer temporarily owns the event lock, sample normally so a
            # newly queued note after long silence cannot lose a whole block.
            # At 48k/512, 400 blocks retain about 4.27s of release tail.
            if self._active_notes == 0 and not events_deferred:
                self._silent_blocks += 1
                if self._silent_blocks > 400:
                    return np.zeros((2, n_samples), dtype=np.float64)
            # get_samples returns interleaved stereo int16, length = 2 * n_samples
            raw = self.fs.get_samples(n_samples)
        finally:
            self._lock.release()

        self._render_count += 1

        # Smooth volume changes (~10ms time constant at 48kHz/128 block).
        # Scalar state shared by both DSP paths below (Faust chain / Python
        # chain) — hoisted above the branch, order vs the int16 conversion
        # is immaterial (independent scalar math).
        smooth_alpha = 1.0 - np.exp(-n_samples / (0.01 * self.sample_rate))
        self._volume_cur += smooth_alpha * (self.volume - self._volume_cur)

        if self._faust_chain is not None:
            # ── Phase 3 Faust path (STAVE_FAUST_PIANO_CHAIN) ──
            # Volume gain + low/high-cut + EQ + sidechain mono run in
            # faust/piano_chain.dsp; the LA-2A gain ramp stays here.
            left, right = self._render_chain_faust(raw, n_samples)
        else:
            # ── Python DSP path (default — byte-identical to pre-Phase-3) ──
            # Convert interleaved int16 stereo to separate L/R float64
            inv_scale = 1.0 / 32768.0
            left = raw[0::2].astype(np.float64) * inv_scale
            right = raw[1::2].astype(np.float64) * inv_scale

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

            # 4-band parametric EQ — applied pre-compressor so the comp reacts
            # to the tone-shaped signal (post-EQ hotness hits the threshold the
            # way the user hears it). Each band skipped when disabled so you
            # only pay biquad cost for what's actually engaged.
            for i, band in enumerate(self.eq_bands):
                if band["enabled"]:
                    left = self.eq_filters_l[i].process(left)
                    right = self.eq_filters_r[i].process(right)

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

        # ── Velocity-aware brightness ──
        # Dynamic lowpass whose cutoff tracks a smoothed recent-velocity
        # value. Soft chord → cutoff drops (~3kHz floor at amount=1), hard
        # chord → cutoff opens (18kHz = effectively bypass). Sits post-comp
        # so compression stays predictable; the filter just shapes final tone.
        if self.vel_bright_enabled and self.vel_bright_amount > 0.001:
            # Smooth tracker toward its target value (~50ms TC)
            smooth_a = 1.0 - np.exp(-n_samples / (0.05 * self.sample_rate))
            self._vel_tracker_cur += smooth_a * (self._vel_tracker - self._vel_tracker_cur)
            # Map tracker → cutoff. At amount=0 floor=18kHz (no effect).
            # At amount=1 floor=1.5kHz — soft playing now actually sounds
            # soft (the old 3kHz floor was still "pretty bright").
            floor = 18000.0 - self.vel_bright_amount * 16500.0
            cutoff = floor + (18000.0 - floor) * max(0.0, min(1.0, self._vel_tracker_cur))
            # Avoid cheap-coefficient-recalc thrash: only push new params
            # when the cutoff has moved enough to hear.
            if abs(cutoff - self._vel_bright_last_cutoff) > 10.0:
                self._vel_bright_filter_l.set_params(cutoff, 0.707)
                self._vel_bright_filter_r.set_params(cutoff, 0.707)
                self._vel_bright_last_cutoff = cutoff
            left = self._vel_bright_filter_l.process(left)
            right = self._vel_bright_filter_r.process(right)

        # Stereo tremolo — Rhodes Suitcase "vibrato" was actually amplitude
        # tremolo on L/R 180° out of phase (auto-pan feel). At depth=0 this
        # block is a no-op, so other presets pay nothing.
        if self.tremolo_depth > 1e-4 and self.tremolo_hz > 0.0:
            n = len(left)
            step = self.tremolo_hz / self.sample_rate
            # Phase ramp across the block, starting at last block's end phase
            t = self._tremolo_phase + np.arange(n, dtype=np.float64) * step
            self._tremolo_phase = (t[-1] + step) % 1.0
            d = self.tremolo_depth
            amp_l = (1.0 - d) + d * (0.5 + 0.5 * np.sin(2.0 * np.pi * t))
            amp_r = (1.0 - d) + d * (0.5 + 0.5 * np.sin(2.0 * np.pi * (t + 0.5)))
            left *= amp_l
            right *= amp_r

        # ── Piano-room reverb (replaces FluidSynth's internal Schroeder) ──
        # 100% wet output from the .so; Python blends dry/wet here. When
        # enable toggles OFF we clear the tank so re-enable starts quiet.
        if self._piano_room is not None:
            with self._piano_room_lock:
                if self.piano_room_enabled and self._piano_room_was_enabled is False:
                    self._piano_room_was_enabled = True
                elif not self.piano_room_enabled and self._piano_room_was_enabled:
                    self._piano_room.clear()
                    self._piano_room_was_enabled = False

                if self.piano_room_enabled and self.reverb_dry_wet > 0.001:
                    smooth_a = 1.0 - np.exp(-n_samples / (0.03 * self.sample_rate))
                    self._piano_room_wet_cur += smooth_a * (
                        float(self.reverb_dry_wet) - self._piano_room_wet_cur
                    )
                    wet = self._piano_room.process(np.stack([left, right]))
                    w = self._piano_room_wet_cur
                    left = left * (1.0 - w) + wet[0] * w
                    right = right * (1.0 - w) + wet[1] * w

        return np.array([left, right])

    def _render_chain_faust(self, raw, n_samples: int):
        """Phase 3 flag path (see faust/piano_chain.dsp + FAUST_PORT_PLAN.md):
        the int16 conversion lands directly in the module's persistent input
        rows, volume gain + 24dB low/high-cut + 4-band EQ run natively, and
        the LA-2A comp consumes the module's post-EQ mono output.

        The comp gain ramp is applied DIRECTLY here. The legacy path
        recovers the very same ramp as compressed/safe_mono — a per-sample
        divide whose epsilon guard (|mono| <= 1e-10 → that sample's wet
        path silently drops to dry) is a hazard, not a feature: (mono·g)/
        mono == g to ~1 ulp wherever the guard doesn't trip, and at
        anticorrelated zero crossings (L ≈ −R, channels loud) the direct
        ramp is strictly more correct. Equivalence measured on real piano
        material in tools/compare_piano_chain.py."""
        chain = self._faust_chain
        buf = chain.in_buffer(n_samples)
        inv_scale = 1.0 / 32768.0
        # int16 → float64 straight into the persistent rows. Same values as
        # the Python path's astype(float64) * inv_scale: the int16→double
        # widening is exact and 2^-15 is a power of two.
        np.multiply(raw[0::2], inv_scale, out=buf[0])
        np.multiply(raw[1::2], inv_scale, out=buf[1])

        # Volume gain zone — the exact dB-curve branch from the Python
        # path. gain == 0.0 mirrors np.zeros_like: the module's filters
        # keep processing zeros, so their state decays identically.
        if self._volume_cur <= 0.001:
            gain = 0.0
        else:
            gain = 10.0 ** ((self._volume_cur - 1.0) * 40.0 / 20.0)  # -40dB to 0dB

        chain.set_block_params(gain=gain,
                               lowcut_hz=self.lowcut_hz,
                               highcut_hz=self.highcut_hz,
                               eq_bands=self.eq_bands)
        out = chain.process_in_place(n_samples)
        left = out[0]
        right = out[1]

        # LA-2A comp — same gate as the Python path; sidechain mono is the
        # module's output 2 (post-EQ (L+R)/2). Block-rate envelope + ramp
        # stay in Python by design (the RMS applies to the block it was
        # measured from — Faust can't see the block boundary).
        if self.comp_enabled and self.comp_wet > 0.001:
            gain_ramp = self._comp_gain_ramp(out[2])
            w = self.comp_wet
            blend = (1.0 - w) + gain_ramp * w
            left = left * blend
            right = right * blend
        return left, right

    def _compress(self, samples: np.ndarray) -> np.ndarray:
        """LA-2A-flavoured feed-forward compressor. Soft-knee + per-sample
        gain interpolation. The interpolation across the block is critical
        (without it, per-block gain jumps clicked at sample 255→256) and
        the soft knee is what gives the musical optical character —
        compression engages smoothly ~knee/2 dB below threshold instead of
        hard-switching.

        Envelope math + gain ramp live in _comp_gain_ramp (shared with the
        Faust-chain path, which applies the ramp to L/R directly)."""
        return samples * self._comp_gain_ramp(samples)

    def _comp_gain_ramp(self, samples: np.ndarray) -> np.ndarray:
        """Compute the LA-2A per-sample gain ramp for one block from the
        (post-EQ mono) sidechain. Updates envelope + prev-gain state.

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
        return gain_ramp

    def set_eq_band(self, index: int, *, freq_hz: float = None, gain_db: float = None,
                    q: float = None, enabled: bool = None):
        """Update one parametric EQ band. Only args explicitly passed are
        updated (rest preserved). Safe to call per-slider-tick from the UI
        because BiquadPeakingEQ.set_params recomputes coefficients in place
        without resetting state (no clicks on sweep)."""
        if not (0 <= index < len(self.eq_bands)):
            return
        b = self.eq_bands[index]
        if freq_hz is not None:
            b["freq_hz"] = max(20.0, min(20000.0, float(freq_hz)))
        if gain_db is not None:
            b["gain_db"] = max(-18.0, min(18.0, float(gain_db)))
        if q is not None:
            b["q"] = max(0.1, min(10.0, float(q)))
        if enabled is not None:
            b["enabled"] = bool(enabled)
        self.eq_filters_l[index].set_params(b["freq_hz"], b["gain_db"], b["q"])
        self.eq_filters_r[index].set_params(b["freq_hz"], b["gain_db"], b["q"])

    def set_voicing(self, name: str):
        """Apply a piano voicing preset: lowcut + highcut + 4-band EQ. Voicings
        are purely tone-shaping and never touch the GM program — that's what
        the Sound dropdown is for."""
        preset = PIANO_VOICINGS.get(name)
        if preset is None:
            logger.warning("Unknown voicing: %s", name)
            return
        self.current_voicing = name
        self.set_lowcut(float(preset["lowcut_hz"]))
        self.set_highcut(float(preset["highcut_hz"]))
        for i, (f, g, q) in enumerate(preset["bands"]):
            self.set_eq_band(i, freq_hz=f, gain_db=g, q=q, enabled=True)
        logger.info("Voicing applied: %s", name)

    def set_soundfont(self, name: str):
        """Swap to a named soundfont preset (SOUNDFONT_PRESETS). Handles the
        underlying sf2/sf3 load when the preset's file differs from what's
        currently loaded, plus any preset-specific effects (tremolo)."""
        preset = SOUNDFONT_PRESETS.get(name)
        if preset is None:
            raise ValueError(f"Unknown soundfont preset: {name}")

        target_file = preset["file"]
        target_program = int(preset.get("program", 0))

        # Every preset's .sf2 is pre-loaded at start(); switching is just a
        # program_select on the already-resident sfid. No sfload/sfunload or
        # dynamic sample allocation occurs on the live path; the remaining
        # native program-select duration is still measured during qualification.
        if self.fs is None:
            # Pre-start state update (set_soundfont called before start())
            self.current_soundfont = name
            self._loaded_file = target_file
            return

        if getattr(self, "_render_owner_attached", False):
            self._request_program_switch(name, preset)
            logger.info("Soundfont switched on audio owner: %s", name)
            return

        # Smooth handoff: `program_select` swaps the channel's program; existing
        # voices ring out naturally through their ADSR while new noteOns use the
        # target patch. `system_reset` is intentionally NOT called — it flushed
        # voices + reverb tail in one block, popping the preset crossfade.
        try:
            with self._lock:
                diagnostic_enter = getattr(self, "_diagnostic_owner_enter", None)
                diagnostic_owner = (
                    diagnostic_enter("program_change") if diagnostic_enter else None
                )
                try:
                    if self._closing or self.fs is None:
                        raise RuntimeError("FluidSynth is closing; preset switch rejected")
                    new_sfid = getattr(self, "_sfid_by_file", {}).get(target_file)
                    if new_sfid is None:
                        # Background preload owns all slow sfload calls. Never put
                        # cold disk I/O on a UI/control operation competing with
                        # the audio renderer.
                        logger.warning("Preset '%s' file='%s' is not preloaded yet; keeping %s",
                                       name, target_file, self.current_soundfont)
                        raise RuntimeError(
                            f"Soundfont preset '{name}' is unavailable or still preloading"
                        )
                    # Preserve the old lock-defined boundary: musical events that
                    # arrived before this switch sound with the old program.
                    batch, recovery = self._take_midi_batch_locked(
                        blocking=True, limit=None
                    )
                    if not self._apply_midi_batch_locked(batch, recovery):
                        raise RuntimeError(
                            "FluidSynth MIDI recovery failed before preset switch"
                        )
                    # (No set_reverb_level here — FluidSynth's reverb is
                    # permanently disabled at start(); piano room colour comes
                    # from the Faust Dattorro in our pipeline.)
                    self._program_select_checked(
                        new_sfid, target_program, f"preset '{name}'"
                    )
                finally:
                    diagnostic_exit = getattr(self, "_diagnostic_owner_exit", None)
                    if diagnostic_exit:
                        diagnostic_exit(diagnostic_owner)
        except Exception as e:
            logger.warning("program_select %d failed on preset switch: %s",
                           target_program, e)
            if isinstance(e, RuntimeError):
                raise
            raise RuntimeError(
                f"FluidSynth program switch to '{name}' failed"
            ) from e

        # Apply preset-specific post-processing only after the native program
        # switch succeeds, so a preload miss cannot leave a hybrid preset.
        self._apply_program_metadata(name, preset, new_sfid)
        logger.info("Soundfont switched: %s (file=%s prog=%d id=%d, trem=%.1fHz/%.2f vel^(1/%.2f))",
                    name, target_file, target_program, new_sfid,
                    self.tremolo_hz, self.tremolo_depth, self.velocity_curve)

    @staticmethod
    def list_available_soundfonts():
        """Return preset names whose underlying sf2/sf3 file exists on disk.
        Presets are the user-facing dropdown entries (Salamander, Fluid,
        Rhodes, Suitcase) — same file can appear under multiple preset names
        (Suitcase = Rhodes + tremolo)."""
        if not SOUNDFONT_DIR.exists():
            return []
        installed = set()
        for p in SOUNDFONT_DIR.iterdir():
            if p.suffix.lower() in (".sf2", ".sf3"):
                installed.add(p.stem)
        return [name for name, preset in SOUNDFONT_PRESETS.items()
                if preset["file"] in installed]

    def update_params(self, params: dict):
        """Update piano parameters from dict."""
        if "volume" in params:
            self.set_volume(float(params["volume"]))
        if "enabled" in params:
            self.enabled = bool(params["enabled"])
        if "soundfont" in params:
            target = str(params["soundfont"])
            if target != self.current_soundfont:
                self.set_soundfont(target)
        if "voicing" in params:
            target = str(params["voicing"])
            if target != self.current_voicing:
                self.set_voicing(target)
        # "sound" state key is vestigial — SOUNDFONT_PRESETS own the GM
        # program now (FluidR3_GM EP1 at prog 4 for Rhodes/Suitcase,
        # Salamander acoustic grand at prog 0, etc.). Ignoring it prevents
        # stale saved state from selecting a program the active preset's
        # SF2 doesn't have.
        if "eq_bands" in params and isinstance(params["eq_bands"], list):
            for i, band in enumerate(params["eq_bands"]):
                if i >= len(self.eq_bands) or not isinstance(band, dict):
                    continue
                self.set_eq_band(
                    i,
                    freq_hz=band.get("freq_hz"),
                    gain_db=band.get("gain_db"),
                    q=band.get("q"),
                    enabled=band.get("enabled"),
                )
        # Individual band param keys from the UI: "eq_band0_freq", "eq_band2_gain", etc.
        for i in range(len(self.eq_bands)):
            for suffix, arg in (("freq", "freq_hz"), ("gain", "gain_db"),
                                ("q", "q"), ("enabled", "enabled")):
                key = f"eq_band{i}_{suffix}"
                if key in params:
                    self.set_eq_band(i, **{arg: params[key]})
        if "filter_highcut_hz" in params:
            self.set_highcut(float(params["filter_highcut_hz"]))
        if "filter_lowcut_hz" in params:
            self.set_lowcut(float(params["filter_lowcut_hz"]))
        if "reverb_dry_wet" in params:
            # Wet level for the dedicated piano-room Faust reverb. State key
            # is kept for backward-compat with saved presets — the old
            # FluidSynth-reverb algorithm was replaced 2026-04-21 but the
            # slider semantic (0 = dry, 1 = fully wet) is identical.
            self.reverb_dry_wet = max(0.0, min(1.0, float(params["reverb_dry_wet"])))
        if "piano_room_enabled" in params:
            self.piano_room_enabled = bool(params["piano_room_enabled"])
        if "piano_room_size" in params and self._piano_room is not None:
            v = max(0.0, min(1.0, float(params["piano_room_size"])))
            with self._piano_room_lock:
                self._piano_room.set_zone("size", v)
        if "piano_room_damp" in params and self._piano_room is not None:
            v = max(0.0, min(0.99, float(params["piano_room_damp"])))
            with self._piano_room_lock:
                self._piano_room.set_zone("damp", v)
        if "vel_bright_enabled" in params:
            self.vel_bright_enabled = bool(params["vel_bright_enabled"])
        if "vel_bright_amount" in params:
            self.vel_bright_amount = max(0.0, min(1.0, float(params["vel_bright_amount"])))
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

    def stop(self, timeout: float = 5.0) -> bool:
        """Stop preload activity, then dispose native state if quiescent.

        A False result means a native preload may still be executing. The
        FluidSynth object deliberately remains owned and must not be deleted.
        """
        self._closing = True
        self.enabled = False
        self._preload_stop.set()
        preload = self._preload_thread
        if preload is not None and preload is not threading.current_thread():
            # An exception during startup can leave the Thread constructed but
            # never started; Thread.join() raises RuntimeError in that state.
            if preload.is_alive():
                preload.join(timeout=max(0.0, float(timeout)))
            if preload.is_alive():
                logger.error("FluidSynth preload did not quiesce; native object retained")
                return False

        self.all_notes_off()
        with self._lock:
            diagnostic_enter = getattr(self, "_diagnostic_owner_enter", None)
            diagnostic_owner = (
                diagnostic_enter("shutdown") if diagnostic_enter else None
            )
            try:
                if self.fs:
                    try:
                        self.fs.delete()
                    except Exception as e:
                        logger.warning("Error stopping FluidSynth: %s", e)
                    self.fs = None
            finally:
                diagnostic_exit = getattr(self, "_diagnostic_owner_exit", None)
                if diagnostic_exit:
                    diagnostic_exit(diagnostic_owner)
        logger.info("FluidSynth stopped")
        return True
