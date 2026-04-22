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

# General MIDI program numbers — independent of the voicing (EQ) system.
# Switching Sound changes the GM program on the loaded soundfont; on
# Salamander only program 0 is populated, so everything collapses to the
# acoustic grand. On FluidR3_GM all of these respond with distinct patches.
SOUNDS = {
    "acoustic_grand_piano":  0,
    "bright_acoustic_piano": 1,
    "electric_grand_piano":  2,
    "honky_tonk_piano":      3,
    "electric_piano_1":      4,   # Rhodes
    "electric_piano_2":      5,   # DX7 / Suitcase-style
    "harpsichord":           6,
    "clavinet":              7,
}

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
        self.current_sound = "acoustic_grand_piano"
        self.current_soundfont = "Arachno"
        self.reverb_dry_wet = 0.4
        self._lock = threading.Lock()

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

    def start(self, soundfont_name: str = "Salamander"):
        """Initialize FluidSynth and PRE-LOAD every available soundfont so
        mid-set preset switching never blocks the audio render thread.

        Audio is rendered via render_block() — no JACK driver needed.

        Pre-loading all soundfonts at startup trades a few seconds of
        boot time for instant live switching. Previously `set_soundfont`
        called `sfunload` + `sfload` under the render lock — a Salamander
        reload is hundreds of ms of cold disk read and dropped audio mid-song.
        With every preset's .sf2 resident in memory, a preset change is a
        3-call `program_select` that takes microseconds.

        Memory cost on the Pi 5 (8 GB): Salamander ~1.2 GB + FluidR3_GM
        ~150 MB = ~1.35 GB resident. Fine.
        """
        self.fs = fluidsynth.Synth(samplerate=float(self.sample_rate))

        # Configure — gain at 1.0 since our pipeline handles volume.
        # FluidSynth's own 1990s Schroeder reverb is DISABLED — piano-room
        # colour now comes from our dedicated Faust Dattorro (self._piano_room)
        # which runs in our Python pipeline and mixes properly with the rest
        # of the chain. Chorus stays off.
        self.fs.setting("synth.polyphony", 64)
        self.fs.setting("synth.gain", 1.0)
        self.fs.setting("synth.reverb.active", 0)
        self.fs.setting("synth.chorus.active", 0)

        # Load piano-room reverb .so. Lazy import so a broken build still
        # starts (piano just won't have room reverb).
        try:
            from .faust_piano_room import FaustPianoRoom
            self._piano_room = FaustPianoRoom(self.sample_rate)
        except Exception as e:
            logger.warning("Piano-room reverb unavailable: %s", e)
            self._piano_room = None

        # ─── Pre-load every preset's underlying .sf2 ─────────────────
        # Dedupe by resolved file path so Rhodes + Suitcase (both FluidR3_GM)
        # share a single sfload. Map by preset["file"] (the canonical key
        # used at switch time).
        self._sfid_by_file = {}         # preset["file"] → sfid
        _sfid_by_path = {}              # resolved abs path → sfid (dedup)
        for preset_name, preset in SOUNDFONT_PRESETS.items():
            file_key = preset["file"]
            if file_key in self._sfid_by_file:
                continue
            sf_path = self._find_soundfont(file_key)
            if sf_path is None:
                logger.warning("Soundfont not found for preset '%s' (file=%s) — "
                               "preset will be unavailable", preset_name, file_key)
                continue
            path_str = str(sf_path)
            if path_str in _sfid_by_path:
                self._sfid_by_file[file_key] = _sfid_by_path[path_str]
                logger.info("Preset '%s' (file=%s) reuses sfid for %s",
                             preset_name, file_key, path_str)
            else:
                try:
                    sfid = self.fs.sfload(path_str)
                except Exception as e:
                    logger.error("sfload crashed for %s: %s", path_str, e)
                    continue
                if sfid < 0:
                    logger.warning("sfload returned %d for %s", sfid, path_str)
                    continue
                self._sfid_by_file[file_key] = sfid
                _sfid_by_path[path_str] = sfid
                logger.info("Preloaded soundfont file='%s' path='%s' sfid=%d",
                             file_key, path_str, sfid)

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
            startup_program = SOUNDS.get(self.current_sound, 0)

        self.current_soundfont = preset_name or soundfont_name

        # Prefer the pre-loaded sfid for the requested startup preset.
        startup_sfid = self._sfid_by_file.get(file_stem)
        if startup_sfid is not None:
            self.sfid = startup_sfid
            self._loaded_file = file_stem
            self.fs.program_select(0, self.sfid, 0, startup_program)
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
                    self.fs.program_select(0, self.sfid, 0, startup_program)
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

    def _find_soundfont(self, name: str):
        """Search for a soundfont file by name."""
        for ext in (".sf2", ".sf3", ".SF2", ".SF3"):
            path = SOUNDFONT_DIR / f"{name}{ext}"
            if path.exists():
                return path

        # Try common system locations (Linux: /usr/share/*; macOS: Homebrew prefixes).
        import os
        from .paths import soundfont_search_dirs
        for d in soundfont_search_dirs():
            for ext in (".sf2", ".sf3"):
                path = os.path.join(d, f"{name}{ext}")
                if os.path.exists(path):
                    return path

        # Fallback chain — Salamander is the default, FluidR3_GM the backup.
        # TimGM6mb was dropped 2026-04-20 (too thin).
        fallbacks = ["Salamander", "FluidR3_GM", "default-GM"]
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
        # Per-preset velocity curve — exponential bias that pushes mid-
        # velocity notes into a soundfont's hard/top layer without clipping
        # at 127. At curve=1.0 this is identity; curve=1.5 maps vel 0.5 → 0.63,
        # and max velocity still maps to 1.0 (no clipping, no "always slam").
        vel_shaped = velocity ** (1.0 / max(1.0, self.velocity_curve))
        vel_midi = max(1, min(127, int(vel_shaped * 127)))
        self._note_on_count += 1
        self._active_notes += 1
        self._silent_blocks = 0
        # Velocity-brightness tracker: fast attack, slow-ish blend so a run
        # of soft notes truly reads as soft even if one loud accent sneaks in.
        self._vel_tracker = 0.6 * self._vel_tracker + 0.4 * float(velocity)
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
        # Reset comp state so the first hard chord after silence doesn't
        # ramp from a stale gain (LA-2A linear-interp between blocks would
        # otherwise pop). Cheap; only fires on panic / instrument-cycle.
        self._comp_envelope = 0.0
        if hasattr(self, "_prev_comp_gain"):
            self._prev_comp_gain = 1.0
        # Flush piano-room tank so panic truly silences (otherwise the tail
        # keeps ringing while piano voices are killed).
        if self._piano_room is not None:
            self._piano_room.clear()

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
            logger.warning("Unknown soundfont preset: %s", name)
            return

        # Always apply tremolo config — cheap, and lets Rhodes→Suitcase toggle
        # without a soundfont reload since they share the same .sf2 file.
        self.tremolo_hz = float(preset.get("tremolo_hz", 0.0))
        self.tremolo_depth = float(preset.get("tremolo_depth", 0.0))
        if self.tremolo_depth <= 1e-4:
            self._tremolo_phase = 0.0  # reset so cycle starts clean on next engage
        self.velocity_curve = float(preset.get("velocity_curve", 1.0))

        target_file = preset["file"]
        target_program = int(preset.get("program", 0))

        # Every preset's .sf2 is pre-loaded at start(); switching is just a
        # program_select on the already-resident sfid. No sfload/sfunload on
        # the audio path → no dropped audio mid-song.
        new_sfid = getattr(self, "_sfid_by_file", {}).get(target_file)

        if self.fs is None:
            # Pre-start state update (set_soundfont called before start())
            self.current_soundfont = name
            self._loaded_file = target_file
            return

        if new_sfid is None:
            # Pre-load miss — could happen if the preset's file wasn't found
            # on disk at startup. One-off sfload on the lock as a last resort
            # (rare; shouldn't hit during normal live use).
            logger.warning("Preset '%s' file='%s' not preloaded — attempting one-off sfload",
                           name, target_file)
            sf_path = self._find_soundfont(target_file)
            if sf_path is None:
                logger.warning("Soundfont file not found: %s — keeping %s",
                               target_file, self.current_soundfont)
                return
            with self._lock:
                loaded_id = self.fs.sfload(str(sf_path))
                if loaded_id < 0:
                    logger.error("one-off sfload failed for %s", sf_path)
                    return
                self._sfid_by_file[target_file] = loaded_id
                new_sfid = loaded_id

        # Instant switch: flush voices + internal reverb tail so the previous
        # patch's decay doesn't bleed in, then program_select to the target.
        try:
            with self._lock:
                self.fs.system_reset()
                self.fs.set_reverb_level(self.reverb_dry_wet)
                self.fs.program_select(0, new_sfid, 0, target_program)
        except Exception as e:
            logger.warning("program_select %d failed on preset switch: %s",
                           target_program, e)
            return

        self.sfid = new_sfid
        self.current_soundfont = name
        self._loaded_file = target_file
        logger.info("Soundfont switched: %s (file=%s prog=%d id=%d, trem=%.1fHz/%.2f vel^(1/%.2f))",
                    name, target_file, target_program, new_sfid,
                    self.tremolo_hz, self.tremolo_depth, self.velocity_curve)

        self._active_notes = 0
        self._silent_blocks = 0
        self._comp_envelope = 0.0
        if hasattr(self, "_prev_comp_gain"):
            self._prev_comp_gain = 1.0

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
            self._piano_room.set_zone("size", v)
        if "piano_room_damp" in params and self._piano_room is not None:
            v = max(0.0, min(0.99, float(params["piano_room_damp"])))
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
