"""Stave Synth — main entry point. Wires all components together."""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import (
    DEFAULT_STATE, AUTOSAVE_INTERVAL, HTTP_PORT,
    ensure_dirs, load_state, save_state,
)
from .synth_engine import SynthEngine
from .jack_engine import JackEngine
from .midi_handler import MidiHandler
from .fluidsynth_player import FluidSynthPlayer
from .organ_engine import OrganEngine
from .preset_manager import PresetManager
from .websocket_server import WebSocketServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def ensure_jack_running():
    """Start JACK server if not already running, targeting USB audio."""
    # Check if JACK is already running
    try:
        result = subprocess.run(
            ["jack_lsp"], capture_output=True, timeout=3
        )
        if result.returncode == 0:
            logger.info("JACK server already running")
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    logger.info("Starting JACK server...")
    script = Path(__file__).parent.parent / "start_jack.sh"
    if script.exists():
        subprocess.Popen(
            [str(script)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)  # Give JACK time to initialize
        return True

    # Fallback: start JACK directly
    # Find USB audio card
    try:
        result = subprocess.run(
            ["aplay", "-l"], capture_output=True, text=True, timeout=5
        )
        card = "0"
        for line in result.stdout.splitlines():
            if "USB" in line and "Audio" in line:
                card = line.split("card ")[1].split(":")[0]
                break
        subprocess.Popen(
            ["jackd", "-R", "-d", "alsa", "-d", f"hw:{card}",
             "-r", "48000", "-p", "128", "-n", "2", "-S"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
        return True
    except Exception as e:
        logger.error("Failed to start JACK: %s", e)
        return False


_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _midi_to_note_label(midi: int) -> str:
    """60 → 'C', 70 → 'A#', etc. Only used for UI labels in the pad slot list."""
    idx = int(midi) % 12
    return _NOTE_NAMES[idx]


class StaveSynth:
    """Main application class — orchestrates all components."""

    def __init__(self):
        ensure_dirs()

        # Load persisted state
        self.state = load_state()

        # Initialize components
        self.synth = SynthEngine()
        self.midi = MidiHandler()
        self.presets = PresetManager()
        self.piano = None
        self.organ = None
        self.instrument_mode = "piano"  # "piano", "organ", "off"
        self.jack = None
        self.ws_server = None

        self._running = False
        self._autosave_thread = None

        # Preset crossfade (ramps numeric params; discrete params snap)
        self._crossfade_thread = None
        self._crossfade_cancel = threading.Event()

        # MIDI CC learn mode (accessed from MIDI thread + WS thread, needs lock)
        self._midi_learn_lock = threading.Lock()
        self._midi_learn_active = False
        self._midi_learn_target = None  # {"id": fader_id, "alt": alt_state}
        # CC mappings: { "cc_number": {"id": fader_id, "alt": alt_state} }
        self._cc_map = {}
        self._load_cc_map()

        # Apply loaded state to synth engine
        self.synth.update_params(self.state.get("synth_pad", {}))
        self.midi.set_transpose(
            self.state.get("master", {}).get("transpose_semitones", 0)
        )

    def _handle_ws_message(self, msg: dict) -> dict | None:
        """Handle an incoming WebSocket message from the UI."""
        msg_type = msg.get("type")

        if msg_type == "fader":
            return self._handle_fader(msg)
        elif msg_type == "alt_toggle":
            return self._handle_alt_toggle(msg)
        elif msg_type == "preset_load":
            return self._handle_preset_load(msg)
        elif msg_type == "preset_save":
            return self._handle_preset_save(msg)
        elif msg_type == "preset_delete":
            return self._handle_preset_delete(msg)
        elif msg_type == "preset_label":
            return self._handle_preset_label(msg)
        elif msg_type == "preset_swap":
            return self._handle_preset_swap(msg)
        elif msg_type == "transpose":
            return self._handle_transpose(msg)
        elif msg_type == "panic":
            return self._handle_panic()
        elif msg_type == "shimmer_toggle":
            return self._handle_shimmer_toggle(msg)
        elif msg_type == "shimmer_high_toggle":
            return self._handle_shimmer_high_toggle(msg)
        elif msg_type == "freeze_toggle":
            return self._handle_freeze_toggle(msg)
        elif msg_type == "octave":
            return self._handle_octave(msg)
        elif msg_type == "drone_toggle":
            return self._handle_drone_toggle(msg)
        elif msg_type == "fade_toggle":
            return self._handle_fade_toggle(msg)
        elif msg_type == "bus_comp_preset":
            return self._handle_bus_comp_preset(msg)
        elif msg_type == "macro_value":
            return self._handle_macro_value(msg)
        elif msg_type == "macro_assign":
            return self._handle_macro_assign(msg)
        elif msg_type == "drone_key":
            return self._handle_drone_key(msg)
        elif msg_type == "drone_off":
            return self._handle_drone_off(msg)
        elif msg_type == "drone_fade":
            return self._handle_drone_fade(msg)
        elif msg_type == "record_toggle":
            return self._handle_record_toggle(msg)
        elif msg_type == "list_recordings":
            return self._handle_list_recordings(msg)
        elif msg_type == "delete_recording":
            return self._handle_delete_recording(msg)
        elif msg_type == "recall_recording_params":
            return self._handle_recall_recording_params(msg)
        elif msg_type == "save_to_pad_slot":
            return self._handle_save_to_pad_slot(msg)
        elif msg_type == "list_pad_slots":
            return self._handle_list_pad_slots(msg)
        elif msg_type == "clear_pad_slot":
            return self._handle_clear_pad_slot(msg)
        elif msg_type == "get_state":
            # Piggy-back the reverb-types availability map so the UI can
            # grey out PLATE / DRONE if their .so didn't build.
            try:
                avail = self.synth.reverb.available_types() if hasattr(self.synth.reverb, "available_types") else None
                if avail and self.ws_server:
                    self.ws_server.broadcast_sync({"type": "reverb_types_available", "available": avail})
            except Exception as e:
                logger.debug("reverb_types_available probe failed: %s", e)
            return {"type": "state", "state": self.state}
        elif msg_type == "debug":
            # Piano diagnostics
            piano_info = {}
            if self.piano:
                import numpy as np
                piano_info["exists"] = True
                piano_info["enabled"] = self.piano.enabled
                piano_info["volume"] = self.piano.volume
                piano_info["fs_alive"] = self.piano.fs is not None
                piano_info["sfid"] = self.piano.sfid
                # Quick render test — does FluidSynth produce any audio?
                try:
                    test_block = self.piano.fs.get_samples(64) if self.piano.fs else None
                    if test_block is not None:
                        piano_info["test_peak"] = float(np.abs(test_block.astype(np.float64)).max())
                    else:
                        piano_info["test_peak"] = -1
                    piano_info["note_on_count"] = self.piano._note_on_count
                    piano_info["render_count"] = self.piano._render_count
                    piano_info["last_raw_peak"] = self.piano._last_raw_peak
                except Exception as e:
                    piano_info["test_error"] = str(e)
            else:
                piano_info["exists"] = False
            return {
                "type": "debug",
                "jack_error": getattr(self.jack, '_last_error', None),
                "jack_traceback": getattr(self.jack, '_last_traceback', None),
                "jack_callbacks": getattr(self.jack, '_callback_count', 0),
                "midi_events": getattr(self.jack, '_midi_events_seen', 0),
                "midi_notes": getattr(self.jack, '_midi_notes_triggered', 0),
                "synth_voices": len(self.synth.voices),
                "synth_osc1": self.synth.osc1_blend,
                "synth_osc2": self.synth.osc2_blend,
                "synth_vol": self.synth.volume,
                "peak_output": getattr(self.jack, '_peak_output', 0),
                "piano_render_peak": getattr(self.jack, '_piano_peak', 0),
                "piano_renders": getattr(self.jack, '_piano_renders', 0),
                "bridge_callbacks": self.jack._bridge.bridge_get_callback_count() if self.jack else 0,
                "bridge_peak": float(self.jack._bridge.bridge_get_peak_output()) if self.jack else 0,
                "bridge_underruns": self.jack._bridge.bridge_get_underrun_count() if self.jack else 0,
                "bridge_xruns": self.jack._bridge.bridge_get_xrun_count() if self.jack else 0,
                "bridge_ring_fill": self.jack._bridge.bridge_get_ring_fill() if self.jack else 0,
                "piano": piano_info,
            }
        elif msg_type == "test_piano":
            return self._handle_test_piano()
        elif msg_type == "instrument_cycle":
            return self._handle_instrument_cycle()
        elif msg_type == "setting":
            return self._handle_setting(msg)
        elif msg_type == "piano_comp_preset":
            return self._handle_piano_comp_preset(msg)
        elif msg_type == "midi_learn_start":
            return self._handle_midi_learn_start()
        elif msg_type == "midi_learn_cancel":
            return self._handle_midi_learn_cancel()
        elif msg_type == "midi_learn_select":
            return self._handle_midi_learn_select(msg)
        elif msg_type == "midi_learn_clear":
            return self._handle_midi_learn_clear(msg)
        elif msg_type == "get_cc_map":
            return {"type": "cc_map", "map": self._cc_map}
        elif msg_type == "get_audio_outputs":
            return self._handle_get_audio_outputs()
        elif msg_type == "set_audio_output":
            return self._handle_set_audio_output(msg)
        else:
            logger.warning("Unknown message type: %s", msg_type)
            return None

    def _handle_fader(self, msg: dict) -> dict:
        fader_id = msg.get("id", 0)
        value = max(0.0, min(1.0, float(msg.get("value", 0))))
        alt = msg.get("alt", False)

        if fader_id == 0:  # OSC 1 volume / OSC 2 volume (alt)
            linked = bool(self.state["synth_pad"].get("osc_levels_linked", False))
            if not alt:
                osc_max = self.state["synth_pad"].get("osc1_max", 1.0)
                scaled = value * osc_max
                self.state["synth_pad"]["osc1_blend"] = scaled
                self.synth.osc1_blend = scaled
                if linked:
                    osc2_max = self.state["synth_pad"].get("osc2_max", 1.0)
                    scaled2 = value * osc2_max
                    self.state["synth_pad"]["osc2_blend"] = scaled2
                    self.synth.osc2_blend = scaled2
            else:
                osc_max = self.state["synth_pad"].get("osc2_max", 1.0)
                scaled = value * osc_max
                self.state["synth_pad"]["osc2_blend"] = scaled
                self.synth.osc2_blend = scaled
                if linked:
                    osc1_max = self.state["synth_pad"].get("osc1_max", 1.0)
                    scaled1 = value * osc1_max
                    self.state["synth_pad"]["osc1_blend"] = scaled1
                    self.synth.osc1_blend = scaled1

        elif fader_id == 1:  # Piano/Organ: Volume(0) / Tone(1) / Comp or Leslie(2)
            alt_state = int(alt) if isinstance(alt, (int, float)) else (1 if alt else 0)
            if self.instrument_mode == "organ" and self.organ:
                if alt_state == 0:
                    self.state["organ"]["volume"] = value
                    self.organ.set_volume(value)
                elif alt_state == 1:
                    # Organ tone: balanced tilt EQ (volume-neutral).
                    # 0 = warm (low boost, high cut), 0.5 = flat, 1 = bright.
                    self.state["organ"]["tone_tilt"] = value
                    if hasattr(self.organ, "set_tone_tilt"):
                        self.organ.set_tone_tilt(value)
                elif alt_state == 2:
                    # Leslie depth: 0=none, 1=full
                    self.state["organ"]["leslie_depth"] = value
                    self.organ.leslie_depth = value
            else:
                # Piano mode (original behavior)
                if alt_state == 0:
                    self.state["piano"]["volume"] = value
                    if self.piano:
                        self.piano.set_volume(value)
                elif alt_state == 1:
                    # Map 0-1 within the configured tone range
                    t_min = self.state["piano"].get("tone_range_min", 200)
                    t_max = self.state["piano"].get("tone_range_max", 20000)
                    freq = t_min * ((t_max / t_min) ** value)
                    self.state["piano"]["filter_highcut_hz"] = freq
                    if self.piano:
                        self.piano.set_highcut(freq)
                elif alt_state == 2:
                    # Compressor amount = parallel wet/dry blend. Fader
                    # maps DIRECTLY to `comp_wet` (0 = dry bypass, 1 = fully
                    # compressed), leaving threshold/ratio/attack/release/
                    # knee at whatever the user dialled in the settings menu
                    # (or the LA-2A-ish defaults). This mirrors the LA-2A
                    # workflow where the musical "character" is fixed and
                    # the user just rides the drive/amount.
                    wet = float(value)
                    self.state["piano"]["comp_wet"] = wet
                    self.state["piano"]["comp_enabled"] = wet > 0.01
                    if self.piano:
                        self.piano.comp_wet = wet
                        self.piano.comp_enabled = wet > 0.01

        elif fader_id == 2:  # Filter: normal = highcut, ALT = lowcut
            alt_state = int(alt) if isinstance(alt, (int, float)) else (1 if alt else 0)
            if alt_state == 0:
                f_min = self.state["synth_pad"].get("filter_range_min", 150)
                f_max = self.state["synth_pad"].get("filter_range_max", 20000)
                freq = f_min * ((f_max / f_min) ** value)
                self.state["synth_pad"]["filter_cutoff_hz"] = freq
                self.synth.filter_cutoff = freq
            else:
                hp_min, hp_max = 20.0, 2000.0
                freq = hp_min * ((hp_max / hp_min) ** value)
                self.state["synth_pad"]["filter_highpass_hz"] = freq
                self.synth.filter_highpass_hz = freq

        elif fader_id == 3:  # FX: Reverb Mix(0) / Shimmer Vol(1) / Motion Bus(2)
            alt_state = int(alt) if isinstance(alt, (int, float)) else (1 if alt else 0)
            if alt_state == 0:
                self.state["synth_pad"]["reverb_dry_wet"] = value
                self.synth.reverb.dry_wet = value
            elif alt_state == 1:
                self.state["synth_pad"]["shimmer_mix"] = value
                self.synth.shimmer_mix = value
            elif alt_state == 2:
                self.state["synth_pad"]["motion_mix"] = value
                self.synth.motion_mix = value

        elif fader_id == 4:  # Master Volume
            if not alt:
                self.state["master"]["volume"] = value
                if self.jack:
                    self.jack.master_volume = value
            else:
                # ALT: drive into pre-limiter. Fader 0..1 → trim 0.5..3.0.
                trim = 0.5 + value * 2.5
                self.state["master"]["pre_limiter_trim"] = trim
                if self.jack:
                    self.jack.pre_gain = max(0.5, min(3.0, trim))

        return {"type": "fader_ack", "id": fader_id, "value": value, "alt": alt}

    def _handle_alt_toggle(self, msg: dict) -> dict:
        fader_id = msg.get("id", 0)
        alt = msg.get("alt", False)
        return {"type": "alt_ack", "id": fader_id, "alt": alt}

    def _rebuild_preset_saved(self):
        """Rebuild ui.preset_saved from which files actually exist on disk."""
        saved = []
        for i in range(self.presets.num_slots):
            saved.append(self.presets._slot_path(i).exists())
        if "ui" not in self.state:
            self.state["ui"] = {}
        self.state["ui"]["preset_saved"] = saved

    def _handle_preset_load(self, msg: dict) -> dict:
        slot = msg.get("slot", 0)
        state = self.presets.load(slot)
        if state:
            # Merge with defaults so new params added since preset was saved exist
            from .config import _deep_merge
            import json
            defaults = json.loads(json.dumps(DEFAULT_STATE))
            old_state = json.loads(json.dumps(self.state))  # snapshot current
            self.state = _deep_merge(defaults, state)
            # Rebuild preset_saved from disk — never trust the snapshot
            self._rebuild_preset_saved()

            # Snap discrete params (instrument mode, waveforms, on/off toggles snap
            # at the start of the crossfade; numeric params ramp over ~400ms).
            master = self.state.get("master", {})
            self.instrument_mode = master.get("instrument_mode", "piano")
            self._apply_instrument_mode()
            new_transpose = int(master.get("transpose_semitones", 0))
            self.midi.set_transpose(new_transpose)
            if self.jack:
                self.jack.transpose = new_transpose

            # Suppress sympathetic for the crossfade — held keys won't pump tone
            # at changing levels into the reverb. Re-armed at the end of the ramp.
            self.synth.sympathetic_set_suppress(True)

            self._start_preset_crossfade(old_state, self.state, duration_ms=800)
            logger.info("Loaded preset slot %d (800ms crossfade)", slot)

            if self.ws_server:
                self.ws_server.broadcast_sync({"type": "state", "state": self.state})
            return {"type": "preset_loaded", "slot": slot}
        return {"type": "error", "message": f"Failed to load preset {slot}"}

    def _start_preset_crossfade(self, old_state: dict, new_state: dict, duration_ms: int = 400):
        """Kick off a linear ramp of numeric params from old_state to new_state.
        Cancels any in-flight crossfade. Non-numeric params are applied at step 0."""
        self._crossfade_cancel.set()
        if self._crossfade_thread and self._crossfade_thread.is_alive():
            self._crossfade_thread.join(timeout=0.05)
        self._crossfade_cancel = threading.Event()
        self._crossfade_thread = threading.Thread(
            target=self._run_preset_crossfade,
            args=(old_state, new_state, duration_ms, self._crossfade_cancel),
            daemon=True,
        )
        self._crossfade_thread.start()

    # Discrete/integer params that must NOT interpolate — mid-ramp fractional
    # values would be truncated (int()) causing weird half-transitions or, worse,
    # brief invalid states (e.g. filter_slope=18 is undefined).
    _SNAP_KEYS = {
        "osc1_octave", "osc2_octave", "piano_octave", "transpose_semitones",
        "unison_voices", "filter_slope", "eq_lowcut_slope",
    }

    @staticmethod
    def _interp_section(old: dict, new: dict, t: float) -> dict:
        """Return a dict with numerics interpolated old->new at fraction t, and
        non-numerics / discrete integer params pulled from new (snap at step 0)."""
        out = {}
        for k, nv in new.items():
            ov = old.get(k, nv)
            if k in StaveSynth._SNAP_KEYS:
                out[k] = nv
            elif isinstance(nv, bool) or isinstance(ov, bool):
                out[k] = nv
            elif isinstance(nv, (int, float)) and isinstance(ov, (int, float)):
                out[k] = ov + (nv - ov) * t
            elif isinstance(nv, dict) and isinstance(ov, dict):
                out[k] = StaveSynth._interp_section(ov, nv, t)
            else:
                out[k] = nv
        return out

    def _run_preset_crossfade(self, old_state: dict, new_state: dict,
                              duration_ms: int, cancel: threading.Event):
        """Linear ramp in ~20ms steps. Cheap — just re-pushes smoothed params."""
        steps = max(1, duration_ms // 20)
        step_sec = (duration_ms / 1000.0) / steps
        old_sp = old_state.get("synth_pad", {})
        new_sp = new_state.get("synth_pad", {})
        old_pi = old_state.get("piano", {})
        new_pi = new_state.get("piano", {})
        old_or = old_state.get("organ", {})
        new_or = new_state.get("organ", {})
        old_m = old_state.get("master", {})
        new_m = new_state.get("master", {})

        for i in range(1, steps + 1):
            if cancel.is_set():
                return
            t = i / steps
            try:
                self.synth.update_params(self._interp_section(old_sp, new_sp, t))
                if self.piano:
                    self.piano.update_params(self._interp_section(old_pi, new_pi, t))
                if self.organ:
                    self.organ.update_params(self._interp_section(old_or, new_or, t))
                if self.jack:
                    ov = old_m.get("volume", 0.85)
                    nv = new_m.get("volume", 0.85)
                    self.jack.master_volume = ov + (nv - ov) * t
                    otrim = float(old_m.get("pre_limiter_trim", 2.0))
                    ntrim = float(new_m.get("pre_limiter_trim", 2.0))
                    self.jack.pre_gain = max(0.5, min(3.0, otrim + (ntrim - otrim) * t))
            except Exception as e:
                logger.error("Preset crossfade error: %s", e)
                self.synth.sympathetic_set_suppress(False)
                return
            time.sleep(step_sec)
        self.synth.sympathetic_set_suppress(False)

    def _handle_preset_save(self, msg: dict) -> dict:
        slot = msg.get("slot", 0)
        if self.presets.save(slot, self.state):
            # Rebuild from disk so it's always accurate
            self._rebuild_preset_saved()
            save_state(self.state)
            return {"type": "preset_saved", "slot": slot}
        return {"type": "error", "message": f"Failed to save preset {slot}"}

    def _handle_preset_delete(self, msg: dict) -> dict:
        slot = msg.get("slot", 0)
        if self.presets.delete(slot):
            self._rebuild_preset_saved()
            # Clear label for the deleted slot
            labels = self.state.setdefault("ui", {}).setdefault(
                "preset_labels", [""] * self.presets.num_slots
            )
            if 0 <= slot < len(labels):
                labels[slot] = ""
            save_state(self.state)
            return {"type": "preset_deleted", "slot": slot}
        return {"type": "error", "message": f"Failed to delete preset {slot}"}

    def _handle_preset_label(self, msg: dict) -> dict:
        slot = msg.get("slot", 0)
        label = str(msg.get("label", ""))[:16]  # cap at 16 chars
        labels = self.state.setdefault("ui", {}).setdefault(
            "preset_labels", [""] * self.presets.num_slots
        )
        if 0 <= slot < len(labels):
            labels[slot] = label
            save_state(self.state)
            return {"type": "preset_labeled", "slot": slot, "label": label}
        return {"type": "error", "message": f"Invalid preset slot {slot}"}

    def _handle_preset_swap(self, msg: dict) -> dict:
        """Swap preset file contents (and labels) between two slots."""
        src = int(msg.get("source", -1))
        dst = int(msg.get("target", -1))
        n = self.presets.num_slots
        if src == dst or src < 0 or src >= n or dst < 0 or dst >= n:
            return {"type": "error", "message": f"Invalid swap {src}→{dst}"}
        src_state = self.presets.load(src)  # None if empty
        dst_state = self.presets.load(dst)
        # Write swapped: source gets dst's content (or delete if dst was empty),
        # dst gets src's content (or delete if src was empty).
        if dst_state is None:
            self.presets.delete(src)
        else:
            self.presets.save(src, dst_state)
        if src_state is None:
            self.presets.delete(dst)
        else:
            self.presets.save(dst, src_state)
        # Swap labels too
        labels = self.state.setdefault("ui", {}).setdefault(
            "preset_labels", [""] * n
        )
        # Pad if needed
        while len(labels) < n:
            labels.append("")
        labels[src], labels[dst] = labels[dst], labels[src]
        self._rebuild_preset_saved()
        save_state(self.state)
        # Broadcast updated state so the UI's visible 5 buttons refresh
        if self.ws_server:
            self.ws_server.broadcast_sync({"type": "state", "state": self.state})
        return {"type": "preset_swapped", "source": src, "target": dst}

    def _handle_transpose(self, msg: dict) -> dict:
        semitones = msg.get("semitones", 0)
        new_val = self.midi.set_transpose(semitones)
        self.state["master"]["transpose_semitones"] = new_val
        if self.jack:
            self.jack.transpose = new_val
        return {"type": "transpose_ack", "semitones": new_val}

    def _handle_test_piano(self) -> dict:
        """Diagnostic: trigger a piano note and measure render output."""
        import numpy as np
        if not self.piano:
            return {"type": "test_piano", "error": "no piano object"}
        if not self.piano.fs:
            return {"type": "test_piano", "error": "FluidSynth is None"}

        # Trigger a test note
        self.piano.note_on(60, 0.8)
        # Render a few blocks to let FluidSynth produce audio
        peaks = []
        for i in range(5):
            block = self.piano.render_block(256)
            peaks.append(float(np.abs(block).max()))
        self.piano.note_off(60)
        return {
            "type": "test_piano",
            "peaks": peaks,
            "max_peak": max(peaks),
            "has_audio": max(peaks) > 0.001,
        }

    def _handle_shimmer_toggle(self, msg: dict) -> dict:
        enabled = msg.get("enabled")
        if enabled is None:
            enabled = not self.synth.shimmer_enabled
        self.synth.shimmer_enabled = enabled
        self.state["synth_pad"]["shimmer_enabled"] = enabled
        return {"type": "shimmer_ack", "enabled": enabled}

    def _handle_shimmer_high_toggle(self, msg: dict) -> dict:
        enabled = msg.get("enabled")
        if enabled is None:
            enabled = not self.synth.shimmer_high
        self.synth.shimmer_high = enabled
        self.state["synth_pad"]["shimmer_high"] = enabled
        return {"type": "shimmer_high_ack", "enabled": enabled}

    def _handle_freeze_toggle(self, msg: dict) -> dict:
        enabled = msg.get("enabled")
        if enabled is None:
            enabled = not self.synth.freeze_enabled
        self.synth.freeze_enabled = enabled
        self.synth.reverb.set_freeze(enabled)
        self.state["synth_pad"]["freeze_enabled"] = enabled
        return {"type": "freeze_ack", "enabled": enabled}

    def _handle_drone_toggle(self, msg: dict) -> dict:
        enabled = msg.get("enabled")
        if enabled is None:
            enabled = not self.synth.drone_enabled
        self.synth.drone_enabled = enabled
        if not enabled:
            self.synth.drone_off()
        self.state["synth_pad"]["drone_enabled"] = enabled
        return {"type": "drone_ack", "enabled": enabled}

    _BUS_COMP_PRESETS = {
        "glue": {
            "bus_comp_enabled": True,
            "bus_comp_threshold_db": -4.0, "bus_comp_ratio": 2.0,
            "bus_comp_attack_ms": 10.0, "bus_comp_release_ms": 300.0,
            "bus_comp_release_auto": True, "bus_comp_makeup_db": 0.0,
            "bus_comp_mix": 0.30, "bus_comp_source": "self",
            "bus_comp_fx_bypass": False,
        },
        "punch": {
            "bus_comp_enabled": True,
            "bus_comp_threshold_db": -10.0, "bus_comp_ratio": 4.0,
            "bus_comp_attack_ms": 3.0, "bus_comp_release_ms": 300.0,
            "bus_comp_release_auto": False, "bus_comp_makeup_db": 2.0,
            "bus_comp_mix": 0.70, "bus_comp_source": "self",
            "bus_comp_fx_bypass": False,
        },
        "pump": {
            "bus_comp_enabled": True,
            "bus_comp_threshold_db": -18.0, "bus_comp_ratio": 10.0,
            "bus_comp_attack_ms": 0.3, "bus_comp_release_ms": 300.0,
            "bus_comp_release_auto": True, "bus_comp_makeup_db": 0.0,
            "bus_comp_mix": 1.0, "bus_comp_source": "bpm",
            "bus_comp_fx_bypass": True,
        },
    }

    # ═══ Recorder (master-output capture) ═══

    def _handle_record_toggle(self, msg: dict) -> dict:
        """Toggle the master-output recorder. Starts a new take when idle,
        stops + flushes when active. On start, snapshots the current state
        to a sidecar JSON for later 'recall params'."""
        if not self.jack or not self.jack.recorder:
            return {"type": "record_ack", "recording": False, "error": "no recorder"}
        rec = self.jack.recorder
        if rec.is_recording():
            meta = rec.stop()
            return {"type": "record_ack", "recording": False, "take": meta}
        # Start — deep-copy state so later edits don't mutate the snapshot
        snapshot = json.loads(json.dumps(self.state, default=str))
        meta = rec.start(state_snapshot=snapshot)
        return {"type": "record_ack", "recording": True, "take": meta}

    def _handle_list_recordings(self, msg: dict) -> dict:
        from .recorder import Recorder as _R
        return {"type": "recordings_list", "takes": _R.list_takes()}

    def _handle_delete_recording(self, msg: dict) -> dict:
        from .recorder import Recorder as _R
        filename = str(msg.get("filename", ""))
        ok = _R.delete_take(filename)
        return {"type": "recording_deleted", "filename": filename, "ok": ok,
                "takes": _R.list_takes()}

    def _handle_recall_recording_params(self, msg: dict) -> dict:
        """Load a take's sidecar state JSON and re-apply every setting so the
        synth returns to the sound captured at record-start."""
        from .recorder import Recorder as _R
        filename = str(msg.get("filename", ""))
        snap = _R.load_state_snapshot(filename)
        if snap is None:
            return {"type": "recall_params_ack", "filename": filename,
                    "ok": False, "error": "no state snapshot"}
        # Re-apply each section.param via _handle_setting (robust to schema drift)
        count = 0
        for section in ("synth_pad", "piano", "organ", "master"):
            src = snap.get(section, {})
            if not isinstance(src, dict):
                continue
            for param, value in src.items():
                if isinstance(value, (dict, list)) and param not in ("eq_bands", "adsr_osc1", "adsr_osc2"):
                    continue  # skip nested / complex fields we can't easily re-apply
                try:
                    self._handle_setting({"section": section, "param": param, "value": value})
                    count += 1
                except Exception as e:
                    logger.debug("recall_params skip %s.%s: %s", section, param, e)
        # Broadcast the refreshed state so all UI clients re-sync
        if self.ws_server:
            self.ws_server.broadcast_sync({"type": "state", "state": self.state})
        return {"type": "recall_params_ack", "filename": filename, "ok": True, "params_applied": count}

    # ═══ Pad sample library (per-slot WAVs) ═══

    _PAD_NOTE_FILENAMES = {
        60: "pad_C.wav", 61: "pad_Cs.wav", 62: "pad_D.wav", 63: "pad_Ds.wav",
        64: "pad_E.wav", 65: "pad_F.wav", 66: "pad_Fs.wav", 67: "pad_G.wav",
        68: "pad_Gs.wav", 69: "pad_A.wav", 70: "pad_As.wav", 71: "pad_B.wav",
    }

    def _pad_dir(self):
        from pathlib import Path
        p = Path.home() / ".local" / "share" / "stave-synth" / "pad_samples"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _handle_list_pad_slots(self, msg: dict) -> dict:
        """List all 12 pad slots — which have a file loaded, filename, duration."""
        import wave as _wave
        pad_dir = self._pad_dir()
        slots = []
        for note in sorted(self._PAD_NOTE_FILENAMES.keys()):
            fname = self._PAD_NOTE_FILENAMES[note]
            path = pad_dir / fname
            info = {"note": note, "filename": fname, "loaded": False,
                    "duration_seconds": 0.0, "label": _midi_to_note_label(note)}
            if path.exists():
                info["loaded"] = True
                try:
                    with _wave.open(str(path), "rb") as w:
                        info["duration_seconds"] = round(w.getnframes() / float(w.getframerate()), 2)
                except Exception:
                    pass
            slots.append(info)
        return {"type": "pad_slots", "slots": slots}

    def _handle_save_to_pad_slot(self, msg: dict) -> dict:
        """Copy a take from the recordings dir into a pad slot. Reloads that slot."""
        import shutil
        from pathlib import Path
        source_filename = str(msg.get("source", ""))
        note = int(msg.get("note", -1))
        if note not in self._PAD_NOTE_FILENAMES:
            return {"type": "error", "message": f"invalid pad slot {note}"}
        # Path safety
        if "/" in source_filename or "\\" in source_filename or ".." in source_filename:
            return {"type": "error", "message": "bad source filename"}
        src = Path.home() / ".local" / "share" / "stave-synth" / "recordings" / source_filename
        if not src.exists():
            return {"type": "error", "message": f"source not found: {source_filename}"}
        dst = self._pad_dir() / self._PAD_NOTE_FILENAMES[note]
        try:
            shutil.copyfile(src, dst)
        except Exception as e:
            return {"type": "error", "message": f"copy failed: {e}"}
        # Reload just this slot
        if self.synth:
            try:
                self.synth.load_pad_samples(self._pad_dir())
            except Exception as e:
                logger.warning("pad reload failed: %s", e)
        return {"type": "pad_slot_saved", "note": note,
                "label": _midi_to_note_label(note),
                "slots": self._handle_list_pad_slots({}).get("slots", [])}

    def _handle_clear_pad_slot(self, msg: dict) -> dict:
        """Delete the WAV for a pad slot → that key reverts to live synth."""
        note = int(msg.get("note", -1))
        if note not in self._PAD_NOTE_FILENAMES:
            return {"type": "error", "message": f"invalid pad slot {note}"}
        path = self._pad_dir() / self._PAD_NOTE_FILENAMES[note]
        try:
            if path.exists():
                path.unlink()
        except Exception as e:
            logger.warning("clear_pad_slot: %s", e)
        if self.synth:
            self.synth.load_pad_samples(self._pad_dir())
        return {"type": "pad_slot_cleared", "note": note,
                "slots": self._handle_list_pad_slots({}).get("slots", [])}

    # ═══ Pad player (drone key triggers from touchscreen) ═══

    def _handle_drone_key(self, msg: dict) -> dict:
        """Force drone to a specific root note from the pad-player UI.
        Tapping the same key while active toggles drone off.

        Dispatch: sampler first — if a per-note pad WAV exists, play it and
        silence the live drone. Else fall back to the live root+fifth synth
        and silence any active sampler voice."""
        note = int(msg.get("note", 60))
        current_key = self.state["synth_pad"].get("drone_key")
        current_on = bool(self.state["synth_pad"].get("drone_enabled", False))
        if current_on and current_key == note:
            self.state["synth_pad"]["drone_enabled"] = False
            self.state["synth_pad"]["drone_key"] = None
            if self.synth:
                self.synth.drone_off()
                self.synth.release_pad_samples()
            return {"type": "drone_key_ack", "note": None, "enabled": False,
                    "source": "off"}
        self.state["synth_pad"]["drone_enabled"] = True
        self.state["synth_pad"]["drone_key"] = note
        source = "live"
        if self.synth:
            used_sample = self.synth.trigger_pad_sample(note)
            if used_sample:
                # Sample took over — silence the live drone so we don't double up
                self.synth.drone_off()
                source = "sample"
            else:
                # No slot WAV for this key — fall through to live synth
                self.synth.release_pad_samples()
                self.synth.set_drone_key(note)
        return {"type": "drone_key_ack", "note": note, "enabled": True,
                "source": source}

    def _handle_drone_off(self, msg: dict) -> dict:
        self.state["synth_pad"]["drone_enabled"] = False
        self.state["synth_pad"]["drone_key"] = None
        if self.synth:
            self.synth.drone_off()
            self.synth.release_pad_samples()
        return {"type": "drone_key_ack", "note": None, "enabled": False}

    def _handle_drone_fade(self, msg: dict) -> dict:
        """Toggle-swell the drone between full and silent. Mirrors master
        FADE: 5s S-curve ramp, cancel+restart safe. Ramps synth._drone_fade_scale."""
        if not self.synth:
            return {"type": "drone_fade_ack", "faded_out": False}
        duration = float(msg.get("duration_s", 5.0))
        # Decide target: if currently faded (scale < 0.5), fade back in; else fade out
        current = getattr(self.synth, "_drone_fade_scale", 1.0)
        target = 1.0 if current < 0.5 else 0.0
        self._start_drone_fade_ramp(target, duration)
        return {"type": "drone_fade_ack", "faded_out": target < 0.5}

    def _start_drone_fade_ramp(self, target: float, duration_s: float):
        """Cancel any prior drone-fade ramp, then S-curve from current → target."""
        import math, threading, time
        if hasattr(self, "_drone_fade_cancel") and self._drone_fade_cancel:
            self._drone_fade_cancel.set()
        cancel = threading.Event()
        self._drone_fade_cancel = cancel
        start = getattr(self.synth, "_drone_fade_scale", 1.0)

        def run():
            steps = max(1, int(duration_s * 50))  # 20 ms steps
            step_sec = duration_s / steps
            span = target - start
            for i in range(1, steps + 1):
                if cancel.is_set():
                    return
                t = i / steps
                prog = (1.0 - math.cos(t * math.pi)) * 0.5
                self.synth._drone_fade_scale = max(0.0, min(1.0, start + span * prog))
                time.sleep(step_sec)
            if not cancel.is_set():
                self.synth._drone_fade_scale = target

        threading.Thread(target=run, daemon=True).start()

    # ═══ Macros (performance morph knobs) ═══

    def _handle_macro_value(self, msg: dict) -> dict:
        """Set a macro's value and apply all of its assigned parameters. Each
        assignment lerps its target param from min → max across the macro
        value 0 → 1."""
        idx = int(msg.get("idx", 0))
        value = float(msg.get("value", 0.0))
        value = max(0.0, min(1.0, value))
        macros = self.state.get("macros", [])
        if idx < 0 or idx >= len(macros):
            return {"type": "error", "message": f"Invalid macro idx {idx}"}
        macros[idx]["value"] = value
        for a in macros[idx].get("assignments", []):
            section = a.get("section")
            param = a.get("param")
            mn = float(a.get("min", 0.0))
            mx = float(a.get("max", 1.0))
            if a.get("is_bool"):
                target_val = value >= 0.5
            else:
                target_val = mn + value * (mx - mn)
            try:
                self._handle_setting({"section": section, "param": param, "value": target_val})
            except Exception as e:
                logger.warning("Macro %d failed to apply %s.%s: %s", idx, section, param, e)
        return {"type": "macro_value_ack", "idx": idx, "value": value}

    def _handle_macro_assign(self, msg: dict) -> dict:
        """Add, remove, toggle, or clear a macro's assignments.
        action in {"toggle", "add", "remove", "clear"}."""
        idx = int(msg.get("idx", 0))
        action = str(msg.get("action", "toggle"))
        macros = self.state.get("macros", [])
        if idx < 0 or idx >= len(macros):
            return {"type": "error", "message": f"Invalid macro idx {idx}"}
        if action == "clear":
            macros[idx]["assignments"] = []
        else:
            section = str(msg.get("section", ""))
            param = str(msg.get("param", ""))
            if not section or not param:
                return {"type": "error", "message": "assign needs section + param"}
            mn = float(msg.get("min", 0.0))
            mx = float(msg.get("max", 1.0))
            is_bool = bool(msg.get("is_bool", False))
            assigns = macros[idx]["assignments"]
            existing = next(
                (a for a in assigns if a.get("section") == section and a.get("param") == param),
                None,
            )
            if action == "remove" or (action == "toggle" and existing):
                if existing:
                    assigns.remove(existing)
            else:  # add or toggle-add
                if not existing:
                    assigns.append({
                        "section": section, "param": param,
                        "min": mn, "max": mx, "is_bool": is_bool,
                    })
        return {
            "type": "macro_assign_ack",
            "idx": idx,
            "assignments": macros[idx]["assignments"],
        }

    def _handle_bus_comp_preset(self, msg: dict) -> dict:
        name = str(msg.get("name", "")).lower()
        preset = self._BUS_COMP_PRESETS.get(name)
        if not preset:
            return {"type": "error", "message": f"Unknown bus_comp preset: {name}"}
        for p, v in preset.items():
            self.state["master"][p] = v
            self._handle_setting({"section": "master", "param": p, "value": v})
        # Push the full state so a second open client (phone next to tablet)
        # picks up the new knob positions immediately, not on next reconnect.
        if self.ws_server:
            self.ws_server.broadcast_sync({"type": "state", "state": self.state})
        return {"type": "bus_comp_preset_ack", "name": name, "values": preset}

    def _handle_fade_toggle(self, msg: dict) -> dict:
        """Toggle master fade: down to 0 then back up to 1 (current master fader
        position still governs the full-gain endpoint via fader_to_amplitude).
        Duration defaults to 5s; user may override via msg['duration_s']."""
        if not self.jack:
            return {"type": "fade_ack", "faded_out": False}
        duration = float(msg.get("duration_s", 5.0))
        faded_out_requested = msg.get("faded_out")
        if faded_out_requested is None:
            faded_out_requested = self.jack._fade_target >= 0.5  # flip
        target = 0.0 if faded_out_requested else 1.0
        self.jack.start_fade(target, duration)
        return {"type": "fade_ack", "faded_out": bool(faded_out_requested)}

    def _handle_instrument_cycle(self) -> dict:
        """Cycle keyboard instrument: piano → organ → off → piano."""
        modes = ["piano", "organ", "off"]
        idx = modes.index(self.instrument_mode) if self.instrument_mode in modes else 0
        self.instrument_mode = modes[(idx + 1) % len(modes)]
        self._apply_instrument_mode()
        logger.info("Instrument mode: %s", self.instrument_mode)
        return {"type": "instrument_mode", "mode": self.instrument_mode}

    def _apply_instrument_mode(self):
        """Enable/disable piano and organ based on current instrument mode."""
        if self.instrument_mode == "piano":
            if self.piano:
                self.piano.enabled = True
            if self.organ:
                self.organ.enabled = False
                self.organ.all_notes_off()
            if self.jack:
                self.jack.piano_player = self.piano
                self.jack.piano_callback = self.piano.midi_callback if self.piano else None
        elif self.instrument_mode == "organ":
            if self.piano:
                self.piano.enabled = False
                self.piano.all_notes_off()
            if self.organ:
                self.organ.enabled = True
            if self.jack:
                self.jack.piano_player = self.organ
                self.jack.piano_callback = self.organ.midi_callback
        else:  # "off"
            if self.piano:
                self.piano.enabled = False
                self.piano.all_notes_off()
            if self.organ:
                self.organ.enabled = False
                self.organ.all_notes_off()
            if self.jack:
                self.jack.piano_player = None
                self.jack.piano_callback = None
        self.state["master"]["instrument_mode"] = self.instrument_mode
        # Sync organ shared filter setting
        if self.jack:
            self.jack.organ_filter_enabled = self.state.get("organ", {}).get("shared_filter_enabled", False)
            # Clear piano-note state so sympathetic/drone don't keep pumping
            # ghost notes into the new instrument after an instrument switch
            # with keys still held.
            self.jack._piano_notes_active.clear()

    def _handle_panic(self) -> dict:
        """Hard-silence everything: voices, piano, organ, freeze, drone, sympathetic, sustain."""
        self.synth.panic()
        if self.piano:
            self.piano.all_notes_off()
        if self.organ:
            self.organ.all_notes_off()
        if self.jack:
            self.jack.panic()
            self.jack.fade_reset()
        self.midi.all_notes_off()
        self.state["synth_pad"]["freeze_enabled"] = False
        self.state["synth_pad"]["drone_enabled"] = False
        logger.info("PANIC — all notes off, freeze/drone cleared, buffers flushed")
        return {"type": "panic_ack", "fade_reset": True}

    def _handle_octave(self, msg: dict) -> dict:
        instrument = msg.get("instrument", "")
        octave = max(-3, min(3, int(msg.get("octave", 0))))
        if instrument == "osc1":
            self.state["synth_pad"]["osc1_octave"] = octave
            if self.jack:
                self.jack.synth.update_params({"osc1_octave": octave})
        elif instrument == "osc2":
            self.state["synth_pad"]["osc2_octave"] = octave
            if self.jack:
                self.jack.synth.update_params({"osc2_octave": octave})
        elif instrument == "pad":
            # Legacy: shift both oscs together
            self.state["synth_pad"]["osc1_octave"] = octave
            self.state["synth_pad"]["osc2_octave"] = octave
            if self.jack:
                self.jack.synth.update_params({"osc1_octave": octave, "osc2_octave": octave})
        elif instrument == "piano":
            self.state["master"]["piano_octave"] = octave
            if self.jack:
                self.jack.piano_octave = octave
        return {"type": "octave_ack", "instrument": instrument, "octave": octave}

    def _handle_get_audio_outputs(self) -> dict:
        """List available audio sinks via pw-jack."""
        outputs = []
        current = None
        try:
            # Get all JACK playback ports (sinks)
            result = subprocess.run(
                ["pw-jack", "jack_lsp", "-t"],
                capture_output=True, text=True, timeout=3
            )
            lines = result.stdout.strip().split("\n")
            sinks = set()
            for line in lines:
                line = line.strip()
                if ":playback_FL" in line:
                    name = line.split(":playback_FL")[0]
                    if name != "StaveSynth":
                        sinks.add(name)
            # Check current connections
            result2 = subprocess.run(
                ["pw-jack", "jack_lsp", "-c"],
                capture_output=True, text=True, timeout=3
            )
            conn_lines = result2.stdout.strip().split("\n")
            for i, line in enumerate(conn_lines):
                if line.strip() == "StaveSynth:out_L" and i + 1 < len(conn_lines):
                    connected_to = conn_lines[i + 1].strip()
                    current = connected_to.split(":playback_FL")[0]
                    break
            outputs = [{"name": s, "active": s == current} for s in sorted(sinks)]
        except Exception as e:
            logger.error("Failed to list audio outputs: %s", e)
        return {"type": "audio_outputs", "outputs": outputs}

    def _handle_set_audio_output(self, msg: dict) -> dict:
        """Route StaveSynth JACK output to the selected sink."""
        target = msg.get("name", "")
        try:
            # Disconnect all current output connections
            result = subprocess.run(
                ["pw-jack", "jack_lsp", "-c"],
                capture_output=True, text=True, timeout=3
            )
            lines = result.stdout.strip().split("\n")
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped in ("StaveSynth:out_L", "StaveSynth:out_R"):
                    # Walk every consecutive indented line — a port can have
                    # multiple connections and each shows as its own indented row.
                    j = i + 1
                    while j < len(lines) and lines[j].startswith("   "):
                        dest = lines[j].strip()
                        subprocess.run(
                            ["pw-jack", "jack_disconnect", stripped, dest],
                            capture_output=True, timeout=3
                        )
                        j += 1
            # Connect to new target
            subprocess.run(
                ["pw-jack", "jack_connect", "StaveSynth:out_L", f"{target}:playback_FL"],
                capture_output=True, timeout=3
            )
            subprocess.run(
                ["pw-jack", "jack_connect", "StaveSynth:out_R", f"{target}:playback_FR"],
                capture_output=True, timeout=3
            )
            logger.info("Audio output routed to: %s", target)
            return {"type": "audio_output_set", "name": target, "success": True}
        except Exception as e:
            logger.error("Failed to set audio output: %s", e)
            return {"type": "audio_output_set", "name": target, "success": False}

    # Canonical "perfect" piano compressor preset — the optical-tube-style
    # settings that give the piano that smooth, musical, slow-onset glue.
    # Kept in one place so the UI button and the defaults file stay in sync.
    # Ratio 3:1 matches the traditional "Compress" mode measurement on real
    # optical-tube units — 4:1 is the often-cited "spec sheet" number but
    # the T4 cell's program-dependent response usually lands closer to 3:1
    # at nominal input levels. Threshold sits low because the optical ratio
    # naturally scales with signal, so the wide soft knee makes the whole
    # thing feel gentle.
    PERFECT_PIANO_COMP = {
        "comp_enabled": True,
        "comp_threshold_db": -20.0,
        "comp_ratio": 3.0,
        "comp_attack_ms": 10.0,
        "comp_release_ms": 80.0,
        "comp_knee_db": 18.0,
        "comp_makeup_db": 0.0,
        "comp_drive_db": 0.0,
        "comp_wet": 1.0,
    }

    def _handle_piano_comp_preset(self, msg: dict) -> dict:
        """Apply a named piano-compressor preset. Currently only 'perfect'
        (optical-style glue) is defined; adding more is just a dict entry."""
        preset_name = str(msg.get("preset", "perfect")).lower()
        preset = self.PERFECT_PIANO_COMP if preset_name == "perfect" else None
        if not preset:
            return {"type": "piano_comp_preset_ack", "preset": preset_name, "applied": False}
        # Merge preset into state and push to the live piano object.
        for k, v in preset.items():
            self.state["piano"][k] = v
        if self.piano:
            self.piano.update_params(preset)
        # Broadcast the new state so open settings tabs reflect the preset
        # values in every knob simultaneously.
        if self.ws_server:
            self.ws_server.broadcast_sync({"type": "state", "state": self.state})
        return {"type": "piano_comp_preset_ack", "preset": preset_name, "applied": True}

    def _handle_setting(self, msg: dict) -> dict:
        """Handle deep settings changes from the settings menu."""
        section = msg.get("section", "")
        param = msg.get("param", "")
        value = msg.get("value")

        if section == "synth_pad":
            if param == "sympathetic_level":
                value = max(0.0, min(0.15, float(value)))
            if param == "osc_levels_linked":
                new_val = bool(value)
                self.state["synth_pad"]["osc_levels_linked"] = new_val
                if new_val:
                    # Snap osc2 to osc1 so they're equal at toggle time
                    osc1_b = float(self.state["synth_pad"].get("osc1_blend", 0.6))
                    self.state["synth_pad"]["osc2_blend"] = osc1_b
                    self.synth.osc2_blend = osc1_b
                    # Broadcast new state so UI fader visuals catch up
                    if self.ws_server:
                        self.ws_server.broadcast_sync({"type": "state", "state": self.state})
            elif param in ("osc1_max", "osc2_max"):
                self.state["synth_pad"][param] = value
                # No synth param to update — max is applied when fader moves
            elif param in self.state["synth_pad"]:
                self.state["synth_pad"][param] = value
                self.synth.update_params({param: value})
                # Reverb type change applies a full preset — mirror the preset's
                # decay/predelay/cuts into state so the UI and save file stay in
                # sync, and a subsequent state reload doesn't clobber the preset.
                if param == "reverb_type":
                    try:
                        from .faust_reverb import REVERB_PRESETS
                        preset = REVERB_PRESETS.get(str(value))
                    except ImportError:
                        preset = None
                    if preset:
                        _sync = {
                            "decay_seconds": "reverb_decay_seconds",
                            "predelay_ms":   "reverb_predelay_ms",
                            "low_cut_hz":    "reverb_low_cut",
                            "high_cut_hz":   "reverb_high_cut",
                            "damp":          "reverb_damp",
                            "shimmer_fb":    "reverb_shimmer_fb",
                            "noise_mod":     "reverb_noise_mod",
                        }
                        for src, dst in _sync.items():
                            if dst in self.state["synth_pad"]:
                                # Default non-FDN / plate-ignored params to 0 so
                                # stale slider values from the previous type
                                # don't carry over visually.
                                self.state["synth_pad"][dst] = preset.get(src, 0.0)
                        # Broadcast so the open reverb tab's sliders update live.
                        if self.ws_server:
                            self.ws_server.broadcast_sync({"type": "state", "state": self.state})
            elif param.startswith("adsr_osc1.") or param.startswith("adsr_osc2."):
                # Per-OSC ADSR namespace: "adsr_osc1.attack_ms" / "adsr_osc2.*"
                adsr_ns, adsr_key = param.split(".", 1)
                self.state["synth_pad"][adsr_ns][adsr_key] = value
                self.synth.update_params({adsr_ns: {adsr_key: value}})
            elif param.startswith("adsr."):
                # Legacy single-ADSR: splat to both OSCs for back-compat.
                adsr_key = param.split(".", 1)[1]
                self.state["synth_pad"]["adsr_osc1"][adsr_key] = value
                self.state["synth_pad"]["adsr_osc2"][adsr_key] = value
                self.synth.update_params({"adsr_osc1": {adsr_key: value}, "adsr_osc2": {adsr_key: value}})
        elif section == "piano":
            if param in self.state["piano"]:
                self.state["piano"][param] = value
                if self.piano:
                    self.piano.update_params({param: value})
                    if param == "enabled" and not value:
                        self.piano.all_notes_off()
        elif section == "organ":
            if param in self.state["organ"]:
                self.state["organ"][param] = value
                if param == "shared_filter_enabled":
                    if self.jack:
                        self.jack.organ_filter_enabled = bool(value)
                elif self.organ:
                    self.organ.update_params({param: value})
        elif section == "master":
            _EQ_MAP = {
                "eq_low_gain": (0, "gain_db"), "eq_mid_gain": (1, "gain_db"), "eq_high_gain": (2, "gain_db"),
                "eq_low_freq": (0, "freq_hz"), "eq_mid_freq": (1, "freq_hz"), "eq_high_freq": (2, "freq_hz"),
            }
            if param in ("eq_lowcut_enabled", "eq_lowcut_hz", "eq_lowcut_slope"):
                self.state["master"][param] = value
                if self.jack:
                    self.jack.set_master_hp(
                        float(self.state["master"].get("eq_lowcut_hz", 80)),
                        int(self.state["master"].get("eq_lowcut_slope", 12)),
                        bool(self.state["master"].get("eq_lowcut_enabled", False)),
                    )
            elif param == "pre_limiter_trim":
                self.state["master"][param] = value
                if self.jack:
                    self.jack.pre_gain = max(0.5, min(3.0, float(value)))
            elif param == "piano_reverb_send":
                v = max(0.0, min(1.0, float(value)))
                self.state["master"][param] = v
                if self.jack:
                    self.jack.piano_reverb_send = v
            elif param == "saturation_enabled":
                self.state["master"][param] = bool(value)
                if self.jack:
                    self.jack.saturation_enabled = bool(value)
            elif param == "bpm":
                self.state["master"]["bpm"] = max(40, min(300, int(float(value))))
                # Delay engine will query this when present
                if self.jack and hasattr(self.jack, "set_bpm"):
                    self.jack.set_bpm(self.state["master"]["bpm"])
            elif param.startswith("bus_comp_"):
                self.state["master"][param] = value
                if self.jack and hasattr(self.jack, "bus_comp"):
                    bc = self.jack.bus_comp
                    if param == "bus_comp_enabled":
                        bc.enabled = bool(value)
                    elif param == "bus_comp_source":
                        src = str(value)
                        if src in ("self", "piano", "lfo", "bpm"):
                            self.jack.bus_comp_source = src
                    elif param == "bus_comp_threshold_db":
                        bc.threshold_db = max(-40.0, min(0.0, float(value)))
                    elif param == "bus_comp_ratio":
                        # Cap at 1000 for effective brick-wall (infinity) limiting
                        bc.ratio = max(1.0, min(1000.0, float(value)))
                    elif param == "bus_comp_attack_ms":
                        bc.attack_ms = max(0.1, min(30.0, float(value)))
                    elif param == "bus_comp_release_ms":
                        bc.release_ms = max(50.0, min(1200.0, float(value)))
                    elif param == "bus_comp_release_auto":
                        bc.release_auto = bool(value)
                    elif param == "bus_comp_makeup_db":
                        bc.makeup_db = max(0.0, min(20.0, float(value)))
                    elif param == "bus_comp_mix":
                        bc.mix = max(0.0, min(1.0, float(value)))
                    elif param == "bus_comp_fx_bypass":
                        self.jack.bus_comp_fx_bypass = bool(value)
                    elif param == "bus_comp_retrigger":
                        self.jack.bus_comp_retrigger = bool(value)
                    elif param == "bus_comp_sc_hpf_hz":
                        hz = max(20.0, min(500.0, float(value)))
                        bc.sidechain_hpf_hz = hz
                        bc._hpf_l.set_params(hz, 0.707)
                        bc._hpf_r.set_params(hz, 0.707)
            elif param in _EQ_MAP:
                idx, field = _EQ_MAP[param]
                bands = self.state["master"].get("eq_bands", [])
                if idx < len(bands):
                    bands[idx][field] = float(value)
                    if self.jack:
                        self.jack.set_master_eq(bands)
            elif param == "eq_bands":
                self.state["master"]["eq_bands"] = value
                if self.jack:
                    self.jack.set_master_eq(value)
            elif param.startswith("eq_band_"):
                idx = int(param.split("_")[-1])
                bands = self.state["master"].get("eq_bands", [])
                if idx < len(bands):
                    bands[idx] = value
                    if self.jack:
                        self.jack.set_master_eq(bands)

        return {"type": "setting_ack", "section": section, "param": param, "value": value}

    # ═══ MIDI Learn & CC Mapping ═══

    def _load_cc_map(self):
        """Load CC mappings from state."""
        saved = self.state.get("midi_cc_map", {})
        self._cc_map = {str(k): v for k, v in saved.items()}

    def _save_cc_map(self):
        """Persist CC mappings to state."""
        self.state["midi_cc_map"] = self._cc_map
        save_state(self.state)

    def _handle_midi_learn_start(self) -> dict:
        """Enter MIDI learn mode — UI goes grey, waiting for fader selection."""
        with self._midi_learn_lock:
            self._midi_learn_active = True
            self._midi_learn_target = None
        logger.info("MIDI learn mode: ON")
        return {"type": "midi_learn_active", "active": True}

    def _handle_midi_learn_cancel(self) -> dict:
        """Exit MIDI learn mode without mapping."""
        with self._midi_learn_lock:
            self._midi_learn_active = False
            self._midi_learn_target = None
        logger.info("MIDI learn mode: OFF")
        return {"type": "midi_learn_active", "active": False}

    def _handle_midi_learn_select(self, msg: dict) -> dict:
        """User tapped a fader in learn mode — now waiting for CC input."""
        fader_id = msg.get("id", 0)
        alt = msg.get("alt", 0)
        with self._midi_learn_lock:
            self._midi_learn_target = {"id": fader_id, "alt": alt}
        logger.info("MIDI learn: waiting for CC → fader %d (alt=%s)", fader_id, alt)
        return {"type": "midi_learn_waiting", "id": fader_id, "alt": alt}

    def _handle_midi_learn_clear(self, msg: dict) -> dict:
        """Clear a CC mapping by CC number."""
        cc_key = str(msg.get("cc", ""))
        logger.info("midi_learn_clear: cc_key=%r, map_keys=%r", cc_key, list(self._cc_map.keys()))
        with self._midi_learn_lock:
            if cc_key in self._cc_map:
                del self._cc_map[cc_key]
                self._save_cc_map()
                logger.info("Cleared CC %s mapping", cc_key)
            return {"type": "cc_map", "map": dict(self._cc_map)}

    def _cc_callback(self, cc_num: int, cc_val: int):
        """Called from JACK engine MIDI thread on CC messages."""
        with self._midi_learn_lock:
            if self._midi_learn_active and self._midi_learn_target:
                # Learn mode: map this CC to the selected fader
                cc_key = str(cc_num)
                self._cc_map[cc_key] = self._midi_learn_target.copy()
                self._save_cc_map()
                logger.info("Mapped CC %d → fader %d alt=%s",
                            cc_num, self._midi_learn_target["id"],
                            self._midi_learn_target["alt"])
                self._midi_learn_active = False
                self._midi_learn_target = None
                if self.ws_server:
                    self.ws_server.broadcast_sync({
                        "type": "midi_learn_mapped",
                        "cc": cc_num,
                        "map": dict(self._cc_map),
                    })
                return

            # Normal mode: apply CC value to mapped fader
            cc_key = str(cc_num)
            target = self._cc_map.get(cc_key)

        if target:
            value = cc_val / 127.0
            fader_msg = {
                "type": "fader",
                "id": target["id"],
                "value": value,
                "alt": target["alt"],
            }
            self._handle_fader(fader_msg)
            if self.ws_server:
                self.ws_server.broadcast_sync({
                    "type": "fader_ack",
                    "id": target["id"],
                    "value": value,
                    "alt": target["alt"],
                    "from_cc": True,
                })

    def _midi_callback(self, event_type: str, note: int, velocity: float):
        """Called from JACK engine on MIDI events."""
        if event_type == "note_on":
            self.midi.on_note_on(note, velocity)
            # Throttle the visual activity ping to ~10 Hz so dense passages
            # (32nd notes on a chord) don't flood the WebSocket queue.
            if self.ws_server:
                now = time.monotonic()
                last = getattr(self, "_midi_activity_last_ts", 0.0)
                if now - last >= 0.1:
                    self._midi_activity_last_ts = now
                    self.ws_server.broadcast_sync({"type": "midi_activity", "event": "on"})
        elif event_type == "note_off":
            self.midi.on_note_off(note)
        elif event_type == "all_notes_off":
            self.midi.all_notes_off()

    def _autosave_loop(self):
        """Periodically save state and broadcast peak levels.
        Runs at 20 Hz so the bus compressor GR LED can track beat-rate pumping.
        Heavier broadcasts (peak_level, system_stats with CPU) are throttled to 5 Hz
        and 1 Hz respectively."""
        import psutil
        self._proc = psutil.Process()
        tick_counter = 0
        stats_counter = 0
        silence_ticks = 0
        TICK_HZ = 20
        TICK_SEC = 1.0 / TICK_HZ
        # Subrate dividers
        PEAK_EVERY = 4     # 5 Hz peak_level broadcasts
        STATS_EVERY = 20   # 1 Hz CPU/RAM
        AUTOSAVE_TICKS = max(1, int(AUTOSAVE_INTERVAL * TICK_HZ))

        while self._running:
            time.sleep(TICK_SEC)
            tick_counter += 1
            stats_counter += 1

            if self.jack and self.ws_server:
                # GR reading — broadcast every tick (20 Hz) for beat-accurate LED
                try:
                    gr_db = float(self.jack.bus_comp.current_gr_db) if hasattr(self.jack, "bus_comp") else 0.0
                    self.ws_server.broadcast_sync({
                        "type": "bus_comp_gr",
                        "gr_db": round(gr_db, 2),
                    })
                except Exception:
                    pass

                # Peak level for output meter — 5 Hz is plenty
                if tick_counter % PEAK_EVERY == 0:
                    peak = self.jack.get_and_reset_peak()
                    bus = self.jack.get_and_reset_bus_peaks()
                    pad = bus.get("pad", 0.0)
                    piano = bus.get("piano", 0.0)
                    if peak > 0.01 or pad > 0.01 or piano > 0.01:
                        self.ws_server.broadcast_sync({
                            "type": "peak_level",
                            "peak": peak,
                            "pad": pad,
                            "piano": piano,
                        })
                        silence_ticks = 0
                    elif silence_ticks < 5:
                        self.ws_server.broadcast_sync({
                            "type": "peak_level", "peak": 0.0, "pad": 0.0, "piano": 0.0,
                        })
                        silence_ticks += 1

            # Broadcast CPU/RAM stats every ~1 second
            if stats_counter >= STATS_EVERY and self.ws_server:
                stats_counter = 0
                try:
                    cpu = self._proc.cpu_percent(interval=None)
                    mem = self._proc.memory_info()
                    self.ws_server.broadcast_sync({
                        "type": "system_stats",
                        "cpu_percent": round(cpu, 1),
                        "ram_mb": round(mem.rss / 1048576, 1),
                    })
                except Exception:
                    pass

            # Autosave every AUTOSAVE_INTERVAL
            if tick_counter >= AUTOSAVE_TICKS:
                tick_counter = 0
                if self._running:
                    try:
                        save_state(self.state)
                    except Exception as e:
                        logger.warning("Auto-save failed: %s", e)

    def start(self):
        """Start all components."""
        logger.info("Starting Stave Synth...")
        self._running = True

        # Ensure JACK is running
        ensure_jack_running()

        # Initialize presets
        self.presets.init_defaults()

        # Start FluidSynth
        try:
            self.piano = FluidSynthPlayer()
            self.piano.start(
                self.state.get("piano", {}).get("soundfont", "Arachno")
            )
            self.piano.update_params(self.state.get("piano", {}))
        except Exception as e:
            logger.warning("FluidSynth not available: %s (piano disabled)", e)
            self.piano = None

        # Start organ engine (lightweight, no external deps).
        # STAVE_FAUST_ORGAN=1 routes through libstave_organ.so (native tonewheel
        # + Leslie); fallback is the numpy OrganEngine.
        from . import config as _cfg
        if _cfg.USE_FAUST_ORGAN:
            try:
                from .faust_organ import FaustOrganEngine
                self.organ = FaustOrganEngine()
                logger.info("Organ: Faust backend (libstave_organ.so)")
            except Exception as e:
                logger.warning("Faust organ load failed (%s) — falling back to Python", e)
                self.organ = OrganEngine()
        else:
            self.organ = OrganEngine()
        self.organ.update_params(self.state.get("organ", {}))
        self.instrument_mode = self.state.get("master", {}).get("instrument_mode", "piano")

        # Start JACK engine (piano/organ audio rendered through our pipeline)
        try:
            piano_cb = self.piano.midi_callback if self.piano else None
            self.jack = JackEngine(
                self.synth,
                midi_callback=self._midi_callback,
                piano_callback=piano_cb,
                piano_player=self.piano,
                cc_callback=self._cc_callback,
            )
            self.jack.master_volume = self.state.get("master", {}).get("volume", 0.85)
            self.jack.transpose = self.state.get("master", {}).get("transpose_semitones", 0)
            self.jack.piano_octave = self.state.get("master", {}).get("piano_octave", 0)
            self.jack.pre_gain = float(self.state.get("master", {}).get("pre_limiter_trim", 2.0))
            self.jack.piano_reverb_send = float(self.state.get("master", {}).get("piano_reverb_send", 0.0))
            self.jack.saturation_enabled = bool(self.state.get("master", {}).get("saturation_enabled", False))
            self.jack.set_bpm(float(self.state.get("master", {}).get("bpm", 120)))
            # Push all bus_comp_* keys at startup so the compressor matches saved state
            m = self.state.get("master", {})
            for p in ("bus_comp_enabled", "bus_comp_source", "bus_comp_threshold_db",
                      "bus_comp_ratio", "bus_comp_attack_ms", "bus_comp_release_ms",
                      "bus_comp_release_auto", "bus_comp_makeup_db", "bus_comp_mix",
                      "bus_comp_fx_bypass", "bus_comp_retrigger", "bus_comp_sc_hpf_hz"):
                if p in m:
                    self._handle_setting({"section": "master", "param": p, "value": m[p]})
            eq_bands = self.state.get("master", {}).get("eq_bands", [])
            if eq_bands:
                self.jack.set_master_eq(eq_bands)
            master = self.state.get("master", {})
            if master.get("eq_lowcut_enabled"):
                self.jack.set_master_hp(
                    float(master.get("eq_lowcut_hz", 80)),
                    int(master.get("eq_lowcut_slope", 12)),
                    True,
                )
            self.jack.start()
        except Exception as e:
            logger.error("Failed to start JACK engine: %s", e)
            logger.info("Make sure JACK is running: jackd -d alsa -r 48000 -p 256")
            sys.exit(1)

        # Apply instrument mode (swaps piano_player/callback if organ is active)
        self._apply_instrument_mode()

        # Start WebSocket + HTTP server
        self.ws_server = WebSocketServer(message_handler=self._handle_ws_message)
        self.ws_server.start()

        # Load pad samples (per-note WAVs) — any slot without a file falls
        # back to the live synth drone automatically.
        try:
            self.synth.load_pad_samples()
        except Exception as e:
            logger.warning("pad sample load failed: %s", e)

        # Start autosave
        self._autosave_thread = threading.Thread(target=self._autosave_loop, daemon=True)
        self._autosave_thread.start()

        # Start ALSA-to-JACK MIDI bridge and auto-connect
        self._setup_midi_bridge()

        # FluidSynth audio now rendered through Python pipeline — no JACK connection needed

        logger.info("Stave Synth is running!")
        logger.info("  UI: http://localhost:%d", HTTP_PORT)

    def _setup_midi_bridge(self):
        """Start a2jmidid and kick off the MIDI auto-connect watcher."""
        try:
            subprocess.Popen(
                ["a2jmidid", "-e"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1)
        except Exception as e:
            logger.warning("Failed to start a2jmidid: %s", e)

        # Do one immediate connect attempt, then start the watcher
        self._connect_midi_ports()
        self._midi_watch_thread = threading.Thread(
            target=self._midi_watch_loop, daemon=True
        )
        self._midi_watch_thread.start()

    def _get_midi_capture_ports(self) -> list[str]:
        """List MIDI capture ports (a2jmidid or PipeWire Midi-Bridge), real devices only.
        PipeWire's built-in bridge exposes 'Midi-Bridge:<device> (capture)'; legacy
        a2jmidid setups use 'a2j:<device>' style names."""
        try:
            result = subprocess.run(
                ["jack_lsp", "-t"], capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.splitlines()
            ports = []
            for i, raw in enumerate(lines):
                line = raw.strip()
                # -t output alternates: port name, then type line starting with whitespace
                if raw.startswith((" ", "\t")):
                    continue
                if "Midi Through" in line:
                    continue
                # Only keep midi capture ports
                type_line = lines[i + 1] if i + 1 < len(lines) else ""
                if "midi" not in type_line.lower():
                    continue
                is_a2j = line.startswith("a2j:") and "capture" in line
                is_pw_bridge = line.startswith("Midi-Bridge:") and line.endswith("(capture)")
                if is_a2j or is_pw_bridge:
                    ports.append(line)
            # If a2j is active, skip Midi-Bridge duplicates to avoid double MIDI events.
            has_a2j = any(p.startswith("a2j:") for p in ports)
            if has_a2j:
                ports = [p for p in ports if not p.startswith("Midi-Bridge:")]
            return ports
        except Exception:
            return []

    def _get_port_connections(self, port: str) -> list[str]:
        """Get list of ports connected to the given port."""
        try:
            result = subprocess.run(
                ["jack_lsp", "-c"], capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.splitlines()
            connections = []
            found = False
            for line in lines:
                if not line.startswith(" ") and not line.startswith("\t"):
                    found = (line.strip() == port)
                elif found:
                    connections.append(line.strip())
            return connections
        except Exception:
            return []

    def _connect_midi_ports(self):
        """Find unconnected MIDI capture ports and wire them to StaveSynth."""
        ports = self._get_midi_capture_ports()
        for port in ports:
            connections = self._get_port_connections(port)
            if "StaveSynth:midi_in" not in connections:
                try:
                    subprocess.run(
                        ["jack_connect", port, "StaveSynth:midi_in"],
                        capture_output=True, timeout=5,
                    )
                    logger.info("Auto-connected MIDI: %s", port)
                except Exception as e:
                    logger.warning("Failed to connect MIDI port %s: %s", port, e)

    def _midi_watch_loop(self):
        """Poll for new MIDI devices every 2 seconds and auto-connect them."""
        while self._running:
            time.sleep(2)
            try:
                self._connect_midi_ports()
            except Exception:
                pass

    def stop(self):
        """Gracefully shut down all components."""
        logger.info("Shutting down Stave Synth...")
        self._running = False
        self._crossfade_cancel.set()

        # Save final state
        try:
            save_state(self.state)
        except Exception:
            pass

        if self.jack:
            self.jack.stop()
        if self.piano:
            self.piano.stop()
        if self.organ:
            self.organ.all_notes_off()
        if self.ws_server:
            self.ws_server.stop()

        logger.info("Stave Synth stopped.")


def main():
    """Entry point."""
    app = StaveSynth()

    def signal_handler(sig, frame):
        app.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    app.start()

    # Check if we have a display for native window
    has_display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    use_gui = has_display and "--no-gui" not in sys.argv

    if use_gui:
        try:
            import webview

            window = webview.create_window(
                "Stave Synth",
                f"http://localhost:{HTTP_PORT}",
                fullscreen=True,
                frameless=True,
            )
            webview.start()
        except Exception as e:
            logger.warning("Could not start native window: %s", e)
            logger.info("Running headless — open http://localhost:%d", HTTP_PORT)
            use_gui = False

    if not use_gui:
        logger.info("Running in headless mode — UI at http://0.0.0.0:%d", HTTP_PORT)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    app.stop()


if __name__ == "__main__":
    main()
