"""Stave Synth — main entry point. Wires all components together."""

import copy
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import (
    DEFAULT_STATE, AUTOSAVE_INTERVAL, HTTP_PORT, LOW_RAM_MODE,
    ensure_dirs, load_state, save_state, _deep_merge,
    DATA_DIR, ISOLATED, JACK_CLIENT_NAME, RUNTIME,
)
from .runtime import InstanceLock
from .synth_engine import SynthEngine
from .jack_engine import JackEngine
from .midi_handler import MidiHandler
from .fluidsynth_player import FluidSynthPlayer
from .organ_engine import OrganEngine
from .preset_manager import PresetManager
from .websocket_server import WebSocketServer
from .state_schema import ValidationError, validate_message, validate_setting, normalize_state, GLOBAL_MASTER_KEYS
from .control_mapping import macro_commands
from .control_queue import ControlQueue
from .ui_recovery import UIRecovery

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

import re as _re
_EQ_BAND_RE = _re.compile(r"^eq_band(\d+)_(freq|gain|q|enabled)$")


def _run_with_hard_timeout(cmd, timeout_s: float = 3.0, hard_timeout_s: float = 5.0):
    """Bound caller wait and admit at most two tracked command workers.

    An uninterruptible child is NOT cancelled by the caller's timeout. Its
    pending worker prevents further admission until it finishes.
    """
    from .routing import run_bounded_command
    return run_bounded_command(cmd, timeout_s, hard_timeout_s)


def ensure_jack_running():
    """Confirm JACK/PipeWire-JACK is reachable.

    The app is always launched via the `pw-jack` prefix (systemd unit +
    stave-synth.sh both prepend it), so by the time this runs the JACK
    API is already shimmed onto PipeWire. The old direct-jackd fallback
    was never reachable on this hardware — removed.
    """
    try:
        result = subprocess.run(
            ["jack_lsp"], capture_output=True, timeout=3
        )
        if result.returncode == 0:
            logger.info("JACK server reachable")
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    logger.error("JACK not reachable via pw-jack — audio will be silent")
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
        self._control_lock = threading.RLock()
        self._control_error = None
        self._control_diagnostics = None
        self._control_diagnostics_unavailable = False
        if os.environ.get("STAVE_DIAGNOSTICS") == "1":
            try:
                from .control_diagnostics import ControlDiagnostics
                self._control_diagnostics = ControlDiagnostics()
            except Exception:
                self._control_diagnostics_unavailable = True
        self._controls = ControlQueue(self._apply_queued_control)
        self._stop_event = threading.Event()
        self._stopping = False
        self._ui_lifecycle_lock = threading.Lock()
        self._ui_recovery = UIRecovery()

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
        self._drone_fade_target = 1.0

        # MIDI CC learn mode (accessed from MIDI thread + WS thread, needs lock)
        self._midi_learn_lock = threading.Lock()
        self._midi_learn_active = False
        self._midi_learn_target = None  # {"id": fader_id, "alt": alt_state}
        # CC mappings: { "cc_number": {"id": fader_id, "alt": alt_state} }
        self._cc_map = {}
        # Last seen CC value per cc_key — preset targets fire on rising edge
        # so a footswitch (press 127, release 0) loads once instead of twice.
        self._cc_last_value = {}
        self._load_cc_map()

        # Apply loaded state to synth engine
        self.synth.update_params(self.state.get("synth_pad", {}))
        self.midi.set_transpose(
            self.state.get("master", {}).get("transpose_semitones", 0)
        )

    def _handle_ws_message(self, msg: dict) -> dict | None:
        """Validate before mutation and serialize all control-plane owners."""
        try:
            command = validate_message(msg)
            with self._control_lock:
                if self._stopping:
                    return {"type": "error", "message": "Synth is shutting down"}
                scene_control = command["type"] in {
                    "setting", "fader", "macro_value", "instrument_cycle", "transpose", "octave",
                    "shimmer_toggle", "shimmer_high_toggle", "freeze_toggle", "piano_comp_preset", "bus_comp_preset"}
                diagnostics = getattr(self, "_control_diagnostics", None)
                if diagnostics is not None and scene_control:
                    with diagnostics.span("control_snapshot"):
                        previous = copy.deepcopy({key: self.state[key] for key in
                                                  ("synth_pad", "piano", "organ", "master", "macros")})
                else:
                    previous = copy.deepcopy({key: self.state[key] for key in
                                              ("synth_pad", "piano", "organ", "master", "macros")}) if scene_control else None
                if scene_control or command["type"] == "recall_recording_params":
                    self._crossfade_cancel.set()
                    self.synth.sympathetic_set_suppress(False)
                try:
                    if diagnostics is None:
                        result = self._dispatch_ws_message(command)
                    else:
                        with diagnostics.span("control_dispatch"):
                            result = self._dispatch_ws_message(command)
                except Exception as exc:
                    if previous is not None:
                        self._restore_failed_scene(previous, exc)
                    raise
                if previous is not None and result and result.get("type") == "error":
                    self._restore_failed_scene(previous, result.get("message", "control rejected"))
                # Detach while owned, before the websocket serializes it.
                if diagnostics is None:
                    return copy.deepcopy(result)
                with diagnostics.span("control_response_detach"):
                    return copy.deepcopy(result)
        except (ValidationError, ValueError, TypeError) as exc:
            logger.warning("Rejected control message: %s", exc)
            return {"type": "error", "message": str(exc)}
        except Exception as exc:
            logger.exception("Control operation failed")
            self._control_error = str(exc)
            return {"type": "error", "message": f"Operation failed: {exc}"}

    def _dispatch_ws_message(self, msg: dict) -> dict | None:
        """Handle an incoming WebSocket message from the UI."""
        msg_type = msg.get("type")

        if msg_type == "fader":
            return self._handle_fader(msg)
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
        elif msg_type == "setlist_save":
            return self._handle_setlist_save(msg)
        elif msg_type == "setlist_load":
            return self._handle_setlist_load(msg)
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
            try:
                sf_list = FluidSynthPlayer.list_available_soundfonts()
            except Exception:
                sf_list = []
            # Push current MIDI connection state so the top-bar pip lights
            # up correctly without waiting for the 2-second watch tick.
            try:
                connected = bool(getattr(self, "_midi_connected", False))
                if self.ws_server:
                    self.ws_server.broadcast_sync({"type": "midi_status", "connected": connected})
            except Exception:
                pass
            # Include the ephemeral fade position — otherwise reloading the
            # page while faded out shows a silent synth with the FADE button
            # in its normal state and no clue why.
            faded_out = bool(self.jack and self.jack._fade_target < 0.5)
            recorder = getattr(self.jack, "recorder", None)
            return {"type": "state", "state": self.state,
                    "health": self._health_status(),
                    "record_status": (recorder.current_status() if recorder is not None
                                      else {"recording": False, "writer_pending": False,
                                            "status": "idle", "complete": None}),
                    "faded_out": faded_out,
                    "drone_faded_out": getattr(self, "_drone_fade_target", 1.0) < 0.5,
                    "soundfonts_available": sf_list,
                    # Small-Pi profile pins unison_voices to 3 (the Faust
                    # fast path); the UI greys out the slider when true.
                    "unison_pinned": LOW_RAM_MODE}
        elif msg_type == "debug":
            # Piano diagnostics
            piano_info = {}
            if self.piano:
                piano_info["exists"] = True
                piano_info["enabled"] = self.piano.enabled
                piano_info["volume"] = self.piano.volume
                piano_info["fs_alive"] = self.piano.fs is not None
                piano_info["sfid"] = self.piano.sfid
                # Diagnostics must never advance/discard the live audio stream.
                try:
                    piano_info["test_peak"] = None
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

    def _handle_setlist_save(self, msg: dict) -> dict:
        slot, name = msg["slot"], msg.get("name", "")
        bank = self.presets.snapshot()  # Invalid is not confused with empty.
        candidate = copy.deepcopy(self.state)
        candidate["setlists"][slot] = {
            "name": name, "presets": bank["presets"], "labels": bank["labels"],
        }
        save_state(candidate)
        self.state = candidate
        return {"type": "setlist_save_ack", "slot": slot, "name": name}

    def _handle_setlist_load(self, msg: dict) -> dict:
        slot = msg["slot"]
        sl = self.state["setlists"][slot]
        if sl.get("presets") is None:
            return {"type": "error", "message": f"Setlist slot {slot} is empty"}
        self.presets.replace_bank(sl["presets"], sl.get("labels", [""] * 10))
        self._rebuild_preset_saved()
        # The bank manifest owns scenes/labels; ui state is a rebuildable cache.
        if self.ws_server:
            self.ws_server.broadcast_sync({"type": "state", "state": self.state})
        return {"type": "setlist_load_ack", "slot": slot, "name": sl.get("name", "")}

    def _rebuild_preset_saved(self):
        bank = self.presets.snapshot()
        self.state["ui"]["preset_saved"] = [p is not None for p in bank["presets"]]
        self.state["ui"]["preset_labels"] = bank["labels"]

    def _handle_preset_load(self, msg: dict) -> dict:
        slot = msg.get("slot", 0)
        scene = self.presets.load_checked(slot)
        if scene is None:
            return {"type": "error", "message": f"Preset slot {slot} is empty"}
        self._begin_scene(scene, completion={"type": "preset_loaded", "slot": slot})
        return {"type": "preset_transition", "slot": slot, "pending": True}

    def _apply_scene_frame(self, frame, previous):
        """Apply one validated frame under control ownership."""
        self.state.update(copy.deepcopy(frame))
        mode = frame["master"]["instrument_mode"]
        mode_changed = mode != self.instrument_mode
        self.synth.update_params(frame["synth_pad"])
        if self.piano:
            self.piano.update_params(frame["piano"])
        if self.organ:
            self.organ.update_params(frame["organ"])
        if self.jack:
            # This switch belongs to the mixer, not either organ backend.
            # Same-mode recalls and rollback must update it too.
            self.jack.organ_filter_enabled = bool(frame["organ"].get("shared_filter_enabled", False))
        if mode_changed:
            self.instrument_mode = mode
            self._apply_instrument_mode()
        if self.piano:
            self.piano.enabled = mode == "piano" and frame["piano"].get("enabled", True)
        if self.organ:
            self.organ.enabled = mode == "organ" and frame["organ"].get("enabled", True)
        for param, value in frame["master"].items():
            if param in GLOBAL_MASTER_KEYS or previous["master"].get(param) == value:
                continue
            self._handle_setting({"section": "master", "param": param, "value": value})

    def _restore_failed_scene(self, previous, failure):
        """Best-effort rollback, never report success after a native failure."""
        current = copy.deepcopy({key: self.state[key] for key in previous})
        try:
            self._apply_scene_frame(previous, current)
        except Exception:
            logger.exception("Scene rollback failed; requesting panic")
            self._handle_panic()
        self._control_error = f"Scene change failed: {failure}"
        if self.ws_server:
            self.ws_server.broadcast_sync({"type": "error", "message": self._control_error})
            self.ws_server.broadcast_sync({"type": "state", "state": self.state})

    def _begin_scene(self, scene, *, completion=None):
        # Validation precedes every state or engine change.
        target = normalize_state(scene, scene=True)
        old = copy.deepcopy({key: self.state[key] for key in target})
        for key in GLOBAL_MASTER_KEYS:
            if key in self.state["master"]:
                target["master"][key] = self.state["master"][key]
        for key in ("drone_enabled", "drone_key"):
            target["synth_pad"][key] = self.state["synth_pad"].get(key)
        self._crossfade_cancel.set()
        initial = {key: self._interp_section(old[key], target[key], 0.0)
                   if isinstance(target[key], dict) else copy.deepcopy(target[key])
                   for key in target}
        try:
            # Snap discrete controls BEFORE accepting the transition. A
            # missing soundfont fails here, not after a false loaded reply.
            self._apply_scene_frame(initial, old)
        except Exception as exc:
            self._restore_failed_scene(old, exc)
            raise
        self.synth.sympathetic_set_suppress(True)
        self._start_preset_crossfade(old, target, duration_ms=800, completion=completion)
        if self.ws_server:
            self.ws_server.broadcast_sync({"type": "state", "state": self.state})

    def _start_preset_crossfade(self, old_state: dict, new_state: dict, duration_ms: int = 400, completion=None):
        """Kick off a linear ramp of numeric params from old_state to new_state.
        Cancels any in-flight crossfade. Non-numeric params are applied at step 0."""
        self._crossfade_cancel.set()
        # Never join a worker while holding the control lock it needs to exit.
        self._crossfade_cancel = threading.Event()
        self._crossfade_thread = threading.Thread(
            target=self._run_preset_crossfade,
            args=(old_state, new_state, duration_ms, self._crossfade_cancel, completion),
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
        if t >= 1.0:
            return copy.deepcopy(new)  # Exact endpoint, without rounding drift.
        out = {}
        for k, nv in new.items():
            ov = old.get(k, nv)
            if k in StaveSynth._SNAP_KEYS or "_split_" in k:
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
                              duration_ms: int, cancel: threading.Event, completion=None):
        """Cancellable ramp; state reflects the last successfully applied frame."""
        steps = max(1, duration_ms // 20)
        interval = duration_ms / 1000.0 / steps
        try:
            for i in range(1, steps + 1):
                with self._control_lock:
                    if cancel.is_set() or not self._running:
                        return
                    previous = copy.deepcopy({key: self.state[key] for key in new_state})
                    frame = {key: self._interp_section(old_state[key], new_state[key], i / steps)
                             if isinstance(new_state[key], dict) else copy.deepcopy(new_state[key])
                             for key in new_state}
                    self._apply_scene_frame(frame, previous)
                if i != steps and cancel.wait(interval):
                    return
            with self._control_lock:
                if not cancel.is_set() and self.ws_server:
                    self.ws_server.broadcast_sync({"type": "state", "state": self.state})
                    if completion:
                        self.ws_server.broadcast_sync(completion)
        except Exception as exc:
            with self._control_lock:
                if not cancel.is_set():
                    self._restore_failed_scene(old_state, exc)
        finally:
            with self._control_lock:
                if self._crossfade_cancel is cancel:
                    self.synth.sympathetic_set_suppress(False)

    def _handle_preset_save(self, msg: dict) -> dict:
        slot = msg.get("slot", 0)
        if self.presets.save(slot, self.state):
            # Rebuild from disk so it's always accurate
            self._rebuild_preset_saved()
            # The bank manifest owns scenes/labels; ui state is a rebuildable cache.
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
            # The bank manifest owns scenes/labels; ui state is a rebuildable cache.
            return {"type": "preset_deleted", "slot": slot}
        return {"type": "error", "message": f"Failed to delete preset {slot}"}

    def _handle_preset_label(self, msg: dict) -> dict:
        slot, label = msg["slot"], msg.get("label", "")
        self.presets.label(slot, label)
        self._rebuild_preset_saved()
        # The bank manifest owns scenes/labels; ui state is a rebuildable cache.
        if self.ws_server:
            self.ws_server.broadcast_sync({"type": "state", "state": self.state})
        return {"type": "preset_labeled", "slot": slot, "label": label}

    def _handle_preset_swap(self, msg: dict) -> dict:
        source, target = msg["source"], msg["target"]
        self.presets.swap(source, target)
        self._rebuild_preset_saved()
        # The bank manifest owns scenes/labels; ui state is a rebuildable cache.
        if self.ws_server:
            self.ws_server.broadcast_sync({"type": "state", "state": self.state})
        return {"type": "preset_swapped", "source": source, "target": target}

    def _handle_transpose(self, msg: dict) -> dict:
        semitones = msg.get("semitones", 0)
        new_val = self.midi.set_transpose(semitones)
        self.state["master"]["transpose_semitones"] = new_val
        if self.jack:
            self.jack.transpose = new_val
        return {"type": "transpose_ack", "semitones": new_val}

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
        try:
            if rec.is_recording():
                meta = rec.stop()
            else:
                meta = rec.start(state_snapshot=normalize_state(self.state, scene=True))
            return {"type": "record_ack", "recording": rec.is_recording(),
                    "take": meta, "status": rec.current_status()}
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            return {"type": "record_ack", "recording": rec.is_recording(),
                    "error": str(exc), "status": rec.current_status()}

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
        self._begin_scene(snap, completion={"type": "recall_params_ack", "filename": filename, "ok": True})
        return {"type": "recall_params_ack", "filename": filename, "ok": True,
                "transition_started": True, "pending": True}

    # ═══ Pad sample library (per-slot WAVs) ═══

    _PAD_NOTE_FILENAMES = {
        60: "pad_C.wav", 61: "pad_Cs.wav", 62: "pad_D.wav", 63: "pad_Ds.wav",
        64: "pad_E.wav", 65: "pad_F.wav", 66: "pad_Fs.wav", 67: "pad_G.wav",
        68: "pad_Gs.wav", 69: "pad_A.wav", 70: "pad_As.wav", 71: "pad_B.wav",
    }

    def _pad_dir(self):
        p = DATA_DIR / "pad_samples"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _handle_list_pad_slots(self, msg: dict) -> dict:
        """Report resident slots, separate from files that failed to load."""
        from .pad_store import MAX_SOURCE_BYTES
        pad_dir = self._pad_dir()
        status = self.synth.pad_sample_status() if self.synth else {}
        resident = status.get("slots", {})
        errors = dict(status.get("errors", {}))
        errors.update(getattr(self, "_pad_slot_errors", {}))
        slots = []
        for note in sorted(self._PAD_NOTE_FILENAMES.keys()):
            fname = self._PAD_NOTE_FILENAMES[note]
            path = pad_dir / fname
            current = resident.get(note, {})
            loaded = bool(current.get("loaded", False))
            file_present = path.is_file()
            error = errors.get(note)
            if file_present and not loaded and not error:
                error = "Synth engine is unavailable" if not self.synth else "Pad file is not loaded"
            info = {"note": note, "filename": fname, "loaded": loaded,
                    "active": bool(current.get("active", False)), "file_present": file_present,
                    "duration_seconds": round(current.get("duration_seconds", 0.0), 2),
                    "memory_bytes": current.get("memory_bytes", 0),
                    "preparing": status.get("preparing_note") == note,
                    "error": error, "label": _midi_to_note_label(note)}
            slots.append(info)
        return {"type": "pad_slots", "slots": slots,
                "memory_bytes": status.get("memory_bytes", 0),
                "max_bank_bytes": status.get("max_bank_bytes"),
                "max_slot_bytes": status.get("max_slot_bytes"),
                "max_source_bytes": MAX_SOURCE_BYTES}

    def _handle_save_to_pad_slot(self, msg: dict) -> dict:
        """Commit a validated take to one inactive pad slot with rollback."""
        from .recorder import Recorder as _R
        from .pad_store import save_pad_slot
        source_filename = str(msg.get("source", ""))
        note = int(msg.get("note", -1))
        if note not in self._PAD_NOTE_FILENAMES:
            return {"type": "error", "message": f"invalid pad slot {note}"}
        if not self.synth:
            return {"type": "error", "message": "Synth engine is unavailable"}
        dst = self._pad_dir() / self._PAD_NOTE_FILENAMES[note]
        try:
            # Claim spans both bounded copying and sample preparation, so
            # delete/prune cannot remove the source during this transaction.
            with _R.claim_take(source_filename) as src:
                warnings = save_pad_slot(self.synth, note, src, dst)
        except Exception as exc:
            if not hasattr(self, "_pad_slot_errors"):
                self._pad_slot_errors = {}
            self._pad_slot_errors[note] = str(exc)
            logger.warning("save_to_pad_slot %s: %s", note, exc)
            return {"type": "error", "message": str(exc), "note": note,
                    "slots": self._handle_list_pad_slots({})["slots"]}
        getattr(self, "_pad_slot_errors", {}).pop(note, None)
        result = self._handle_list_pad_slots({})
        result.update(type="pad_slot_saved", note=note,
                      label=_midi_to_note_label(note), warnings=warnings)
        return result

    def _handle_clear_pad_slot(self, msg: dict) -> dict:
        """Recoverably clear one inactive slot without restarting other beds."""
        from .pad_store import clear_pad_slot
        note = int(msg.get("note", -1))
        if note not in self._PAD_NOTE_FILENAMES:
            return {"type": "error", "message": f"invalid pad slot {note}"}
        if not self.synth:
            return {"type": "error", "message": "Synth engine is unavailable"}
        path = self._pad_dir() / self._PAD_NOTE_FILENAMES[note]
        try:
            warnings = clear_pad_slot(self.synth, note, path)
        except Exception as exc:
            if not hasattr(self, "_pad_slot_errors"):
                self._pad_slot_errors = {}
            self._pad_slot_errors[note] = str(exc)
            logger.warning("clear_pad_slot %s: %s", note, exc)
            return {"type": "error", "message": str(exc), "note": note,
                    "slots": self._handle_list_pad_slots({})["slots"]}
        getattr(self, "_pad_slot_errors", {}).pop(note, None)
        result = self._handle_list_pad_slots({})
        result.update(type="pad_slot_cleared", note=note, warnings=warnings)
        return result

    # ═══ Pad player (drone key triggers from touchscreen) ═══

    def _handle_drone_key(self, msg: dict) -> dict:
        """Force drone to a specific root note from the pad-player UI.
        Tapping the same key while active toggles drone off.

        Sampler-only — if a per-note pad WAV exists, play it; otherwise the
        key is silent. The legacy live-drone (root+fifth) fallback was
        removed since the user is using only sampled pads."""
        note = int(msg.get("note", 60))
        rise_seconds_msg = float(msg.get("rise_seconds", 0.0) or 0.0)
        current_key = self.state["synth_pad"].get("drone_key")
        current_on = bool(self.state["synth_pad"].get("drone_enabled", False))
        # Toggle-off only when not in rise mode — with rise armed, every tap
        # re-triggers (otherwise tapping same pad twice would just release it).
        if current_on and current_key == note and rise_seconds_msg <= 0.0:
            self.state["synth_pad"]["drone_enabled"] = False
            self.state["synth_pad"]["drone_key"] = None
            if self.synth:
                self.synth.release_pad_samples()
            return {"type": "drone_key_ack", "note": None, "enabled": False,
                    "source": "off"}
        rise_seconds = float(msg.get("rise_seconds", 0.0) or 0.0)
        rise_cutoff = self.state.get("synth_pad", {}).get("pad_rise_cutoff_hz", 3000.0)
        if self.synth:
            used_sample = self.synth.trigger_pad_sample(
                note, rise_seconds=rise_seconds, rise_cutoff_open=rise_cutoff,
            )
            if used_sample:
                self.state["synth_pad"]["drone_enabled"] = True
                self.state["synth_pad"]["drone_key"] = note
                return {"type": "drone_key_ack", "note": note, "enabled": True,
                        "source": "sample"}
            # No slot WAV for this key — silent. State stays "off".
            self.synth.release_pad_samples()
        self.state["synth_pad"]["drone_enabled"] = False
        self.state["synth_pad"]["drone_key"] = None
        return {"type": "drone_key_ack", "note": note, "enabled": False,
                "source": "no-sample"}

    def _handle_drone_fade(self, msg: dict) -> dict:
        """Toggle-swell the drone between full and silent. Mirrors master
        FADE: 5s S-curve ramp, cancel+restart safe. Ramps synth._drone_fade_scale."""
        if not self.synth:
            return {"type": "drone_fade_ack", "faded_out": False}
        duration = float(msg.get("duration_s", 5.0))
        # Toggle the intended endpoint, not the in-flight gain. Otherwise a
        # second press early in a fade repeats it instead of reversing it.
        # Absolute requests also keep two controllers' intended state explicit.
        if msg.get("faded_out") is not None:
            target = 0.0 if msg["faded_out"] else 1.0
        else:
            target = 1.0 if getattr(self, "_drone_fade_target", 1.0) < 0.5 else 0.0
        self._start_drone_fade_ramp(target, duration)
        self._drone_fade_target = target
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
                with self._control_lock:
                    if cancel.is_set() or self._stopping:
                        return
                    t = i / steps
                    prog = (1.0 - math.cos(t * math.pi)) * 0.5
                    self.synth._drone_fade_scale = max(0.0, min(1.0, start + span * prog))
                if cancel.wait(step_sec):
                    return
            with self._control_lock:
                if not cancel.is_set():
                    self.synth._drone_fade_scale = target

        self._drone_fade_thread = threading.Thread(target=run, daemon=True)
        self._drone_fade_thread.start()

    # ═══ Macros (performance morph knobs) ═══

    def _handle_macro_value(self, msg: dict) -> dict:
        idx, value = msg["idx"], msg["value"]
        macros = self.state["macros"]
        # The saved ranges are DOM units; the shared mapper converts to the
        # exact canonical fader/setting units, including linked slider twins.
        commands = [validate_message(command) for command in macro_commands(
            macros[idx], value, link_state=self.state["synth_pad"])]
        for command in commands:
            result = self._dispatch_ws_message(command)
            if result and result.get("type") == "error":
                return result
        macros[idx]["value"] = value
        if self.ws_server:
            self.ws_server.broadcast_sync({"type": "state", "state": self.state})
        return {"type": "macro_value_ack", "idx": idx, "value": value}

    def _handle_macro_assign(self, msg: dict) -> dict:
        """Toggle, clear, or set bipolar on a macro's assignments.
        UI sends action in {"toggle", "clear", "set_bipolar"}."""
        action = str(msg.get("action", "toggle"))
        macros = self.state.get("macros", [])
        idx = int(msg.get("idx", 0))
        if idx < 0 or idx >= len(macros):
            return {"type": "error", "message": f"Invalid macro idx {idx}"}
        if action == "set_bipolar":
            macros[idx]["bipolar"] = bool(msg.get("bipolar", False))
            return {
                "type": "macro_assign_ack",
                "idx": idx,
                "bipolar": macros[idx]["bipolar"],
                "assignments": macros[idx]["assignments"],
            }
        if action == "clear":
            macros[idx]["assignments"] = []
        else:
            kind = str(msg.get("kind", "param"))
            mn = float(msg.get("min", 0.0))
            mx = float(msg.get("max", 1.0))
            is_bool = bool(msg.get("is_bool", False))
            assigns = macros[idx]["assignments"]
            if kind == "fader":
                fid = int(msg.get("fader_id", -1))
                falt = int(msg.get("fader_alt", 0))
                if fid < 0:
                    return {"type": "error", "message": "fader assign needs fader_id"}
                existing = next(
                    (a for a in assigns
                     if a.get("kind") == "fader"
                     and a.get("fader_id") == fid
                     and a.get("fader_alt") == falt),
                    None,
                )
                new_entry = {
                    "kind": "fader", "fader_id": fid, "fader_alt": falt,
                    "min": mn, "max": mx, "is_bool": False,
                }
            else:
                section = str(msg.get("section", ""))
                param = str(msg.get("param", ""))
                if not section or not param:
                    return {"type": "error", "message": "assign needs section + param"}
                existing = next(
                    (a for a in assigns
                     if a.get("kind", "param") == "param"
                     and a.get("section") == section
                     and a.get("param") == param),
                    None,
                )
                new_entry = {
                    "kind": "param", "section": section, "param": param,
                    "min": mn, "max": mx, "is_bool": is_bool,
                }
            # Toggle: remove if an equivalent assignment exists, else add.
            if action == "toggle" and existing:
                assigns.remove(existing)
            elif not existing:
                if len(assigns) >= 32:
                    raise ValidationError("macro supports at most 32 assignments")
                if "center" in msg:
                    new_entry["center"] = msg["center"]
                assigns.append(new_entry)
        return {
            "type": "macro_assign_ack",
            "idx": idx,
            "bipolar": macros[idx].get("bipolar", False),
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
        self._crossfade_cancel.set()
        controls = getattr(self, "_controls", None)
        if controls is not None:
            controls.clear()
        drone_cancel = getattr(self, "_drone_fade_cancel", None)
        if drone_cancel is not None:
            drone_cancel.set()
        self.synth.panic()
        self._drone_fade_target = 1.0
        piano_error = None
        if self.piano:
            try:
                if self.piano.all_notes_off() is False:
                    raise RuntimeError("Piano release was incomplete")
            except Exception as exc:
                # A piano fault must not prevent the other layers, MIDI
                # ownership and queued JACK audio from being cleared. Finish
                # those releases, then report failure instead of panic_ack.
                piano_error = exc
        if self.organ:
            if hasattr(self.organ, "hard_panic"):
                self.organ.hard_panic()
            else:
                self.organ.all_notes_off()
        if self.jack:
            self.jack.panic()
            self.jack.fade_reset()
        self.midi.all_notes_off()
        self.state["synth_pad"]["freeze_enabled"] = False
        self.state["synth_pad"]["drone_enabled"] = False
        self.state["synth_pad"]["drone_key"] = None
        if piano_error is not None:
            raise RuntimeError("Piano panic failed; remaining cleanup completed") from piano_error
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
        """List complete, typed stereo sinks and verified own-client links."""
        from .routing import read_ports, read_connections, stereo_sinks
        try:
            with self._control_lock:
                sinks = stereo_sinks(read_ports(_run_with_hard_timeout), JACK_CLIENT_NAME)
                graph = read_connections(_run_with_hard_timeout)
                outputs = [{"name": name, "ports": list(pair),
                            "active": pair[0] in graph.get(f"{JACK_CLIENT_NAME}:out_L", set())
                            and pair[1] in graph.get(f"{JACK_CLIENT_NAME}:out_R", set())}
                           for name, pair in sorted(sinks.items())]
                return {"type": "audio_outputs", "outputs": outputs,
                        "preferred": self.state.get("master", {}).get("audio_output_pref")}
        except Exception as exc:
            return {"type": "audio_outputs", "outputs": [], "error": str(exc)}

    def _handle_set_audio_output(self, msg: dict) -> dict:
        """Verify/connect a new stereo pair before retiring this client's old pair."""
        from .routing import switch_stereo
        target = msg.get("name", "")
        if ISOLATED:
            return {"type": "audio_output_set", "name": target,
                    "success": False,
                    "error": "Automatic output routing is disabled for isolated instances"}
        try:
            # Lock order is always control -> routing, including the watcher.
            with self._control_lock:
                if self._stopping:
                    raise RuntimeError("Synth is shutting down")
                if not hasattr(self, "_routing_lock"):
                    self._routing_lock = threading.RLock()
                with self._routing_lock:
                    result = switch_stereo(_run_with_hard_timeout, JACK_CLIENT_NAME,
                                           target, should_stop=lambda: self._stopping)
                    result.update(type="audio_output_set", name=target)
                    if not result["success"]:
                        return result
                    # Only a verified route may become the runtime preference.
                    self.state.setdefault("master", {})["audio_output_pref"] = target
                    try:
                        save_state(copy.deepcopy(self.state))
                        result["persisted"] = True
                    except Exception as exc:
                        # Do not tear down working audio merely because storage
                        # failed; report the lost persistence explicitly.
                        self._control_error = str(exc)
                        result.update(persisted=False,
                                      warning=f"Audio routed, but output preference was not saved: {exc}")
                    return result
        except Exception as exc:
            return {"type": "audio_output_set", "name": target,
                    "success": False, "error": str(exc),
                    "uncertain": bool(getattr(exc, "uncertain", False))}

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
        section, param = msg.get("section"), msg.get("param")
        value = validate_setting(section, param, msg.get("value"))
        with self._control_lock:
            # Validate paired fader limits before accepting either endpoint.
            if param in ("filter_range_min", "filter_range_max", "tone_range_min", "tone_range_max"):
                prefix = param.rsplit("_", 1)[0]
                lo = value if param.endswith("min") else self.state[section][prefix + "_min"]
                hi = value if param.endswith("max") else self.state[section][prefix + "_max"]
                if lo > hi:
                    raise ValidationError("minimum control range exceeds maximum")
            return self._apply_setting({"section": section, "param": param, "value": value})

    def _apply_setting(self, msg: dict) -> dict:
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
            # Per-band EQ knobs ship as synthetic params (eq_band{N}_{freq|gain|q|enabled})
            # and aren't top-level state keys. Route them into the eq_bands list
            # and forward to the player, which knows how to unpack the key.
            _eq_match = _EQ_BAND_RE.match(param) if param else None
            if _eq_match:
                idx = int(_eq_match.group(1))
                if not 0 <= idx < 4:
                    return {"type": "error", "message": "piano EQ band must be between 0 and 3"}
                field_map = {"freq": "freq_hz", "gain": "gain_db",
                             "q": "q", "enabled": "enabled"}
                field = field_map[_eq_match.group(2)]
                bands = self.state["piano"].setdefault("eq_bands", [])
                while len(bands) <= idx:
                    bands.append({"freq_hz": 1000.0, "gain_db": 0.0,
                                  "q": 1.0, "enabled": True})
                bands[idx][field] = (bool(value) if field == "enabled"
                                     else float(value))
                if self.piano:
                    self.piano.update_params({param: value})
            else:  # The schema also recognizes optional piano-room controls.
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
            if param == "piano_octave":
                value = max(-3, min(3, int(value)))
                self.state["master"][param] = value
                if self.jack:
                    self.jack.piano_octave = value
            elif param == "volume":
                if self.jack:
                    self.jack.master_volume = value
                self.state["master"][param] = value
            elif param == "transpose_semitones":
                self.midi.set_transpose(value)
                if self.jack:
                    self.jack.transpose = value
                self.state["master"][param] = value
            elif param == "instrument_mode":
                if value != self.instrument_mode:
                    self.instrument_mode = value
                    self._apply_instrument_mode()
                self.state["master"][param] = value
            elif param == "split_octave_snapshot":
                self.state["master"][param] = copy.deepcopy(value)
            elif param == "audio_output_pref":
                raise ValidationError("select an audio output using set_audio_output")
            elif param in ("eq_lowcut_enabled", "eq_lowcut_hz", "eq_lowcut_slope"):
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
            elif param == "pitch_bend_enabled":
                v = bool(value)
                self.state["master"][param] = v
                if self.jack:
                    self.jack.pitch_bend_enabled = v
                    # Force-center pad + piano on disable so any current bend
                    # snaps cleanly. (Enable doesn't need a snap — next 0xE0
                    # tick will set the live value.)
                    if not v:
                        self.synth.set_pitch_bend(0.0)
                        if self.piano:
                            self.piano.midi_callback("pitch_bend", 8192, 0)
            elif param == "midi_clock_enabled":
                v = bool(value)
                self.state["master"][param] = v
                if self.jack:
                    self.jack.midi_clock_enabled = v
                    # Reset averaging state on toggle so a stale dt from a
                    # prior session doesn't bias the next BPM derivation.
                    self.jack._midi_clock_avg_dt = 0.0
                    self.jack._midi_clock_count = 0
            elif param == "piano_reverb_send":
                v = max(0.0, min(1.0, float(value)))
                self.state["master"][param] = v
                if self.jack:
                    self.jack.piano_reverb_send = v
            elif param == "piano_delay_send":
                v = max(0.0, min(1.0, float(value)))
                self.state["master"][param] = v
                if self.jack:
                    self.jack.piano_delay_send = v
            elif param == "saturation_enabled":
                self.state["master"][param] = bool(value)
                if self.jack:
                    self.jack.saturation_enabled = bool(value)
            elif param == "split_enabled":
                v = bool(value)
                was = bool(self.state["master"].get(param, False))
                # Octave SWAP between two saved sets — one for LAYER-off mode
                # ("normal play"), one for LAYER-on mode ("layered play"). On
                # every toggle, the current octaves get stashed (they belong
                # to the mode we're leaving), and the stash gets loaded (it
                # belongs to the mode we're entering). User's octave tweaks in
                # one mode are remembered when they flip back; LAYER off
                # always restores normal play, LAYER on always restores their
                # layered tuning. Stash persists in state so this survives
                # presets + reloads.
                if v != was:
                    sp = self.state["synth_pad"]
                    m  = self.state["master"]
                    current = {
                        "osc1_octave":  sp.get("osc1_octave",  0),
                        "osc2_octave":  sp.get("osc2_octave",  0),
                        "piano_octave": m.get("piano_octave", 0),
                        "shimmer_high": sp.get("shimmer_high", False),
                    }
                    stash = m.get("split_octave_snapshot")
                    # First toggle ever: seed stash with current so the swap
                    # is a no-op for this first transition. From then on the
                    # two modes accumulate independent octave histories.
                    if not isinstance(stash, dict):
                        stash = dict(current)
                    # Apply stash → current state
                    sp["osc1_octave"]  = int(stash.get("osc1_octave",  0))
                    sp["osc2_octave"]  = int(stash.get("osc2_octave",  0))
                    m["piano_octave"]  = int(stash.get("piano_octave", 0))
                    sp["shimmer_high"] = bool(stash.get("shimmer_high", False))
                    # Save what was current as the new stash (it belongs to
                    # the mode we're leaving).
                    m["split_octave_snapshot"] = current
                    if self.jack:
                        self.synth.update_params({
                            "osc1_octave":  sp["osc1_octave"],
                            "osc2_octave":  sp["osc2_octave"],
                            "shimmer_high": sp["shimmer_high"],
                        })
                        self.jack.piano_octave = m["piano_octave"]
                self.state["master"][param] = v
                if self.jack:
                    self.jack.split_enabled = v
                    self.synth.split_enabled = v
                # Broadcast on transition so UI octave readouts + front-panel
                # displays refresh without waiting for the user to touch.
                if self.ws_server and v != was:
                    self.ws_server.broadcast_sync({"type": "state", "state": self.state})
            elif param in ("instrument_split_low", "instrument_split_high",
                           "instrument_split_xfade"):
                lo, hi = (0, 24) if param.endswith("xfade") else (0, 127)
                v = max(lo, min(hi, int(value)))
                self.state["master"][param] = v
                if self.jack:
                    setattr(self.jack, param, v)
            elif param == "low_latency_mode":
                enabled = bool(value)
                if self.jack:
                    if self.jack.set_low_latency_mode(enabled) is False:
                        raise RuntimeError("Audio ring is busy; latency mode did not change")
                self.state["master"][param] = enabled
            elif param == "bpm":
                self.state["master"]["bpm"] = value
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
            elif param == "show_macros":
                # UI-only flag — toggles the macro row's visibility on the main
                # face. No engine effect; just persist + broadcast so the UI
                # picks it up without a reload.
                self.state["master"][param] = bool(value)
                if self.ws_server:
                    self.ws_server.broadcast_sync({"type": "state", "state": self.state})

        return {"type": "setting_ack", "section": section, "param": param, "value": value}

    # ═══ MIDI Learn & CC Mapping ═══

    def _load_cc_map(self):
        """Load CC mappings from state."""
        saved = self.state.get("midi_cc_map", {})
        self._cc_map = {str(k): v for k, v in saved.items()}
        # Factory defaults for the two universally-used MIDI CCs. Out of the
        # box every keyboard with a mod wheel + every expression-pedal owner
        # gets a musical assignment — no learn step required. If the user
        # remaps either CC, their map wins; clearing brings the default back
        # on next boot (easy enough to re-clear if not wanted).
        DEFAULT_CC_MAP = {
            "1":  {"kind": "fader", "id": 2, "alt": 0},  # mod wheel    → FILTER cutoff fader
            "11": {"kind": "fader", "id": 4, "alt": 0},  # expression   → MASTER volume fader
        }
        for cc, target in DEFAULT_CC_MAP.items():
            if cc not in self._cc_map:
                self._cc_map[cc] = target

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
        """User tapped a fader, macro slot, OR preset slot in learn mode — waiting for CC."""
        with self._midi_learn_lock:
            if "macro_idx" in msg:
                idx = int(msg.get("macro_idx", 0))
                self._midi_learn_target = {"kind": "macro", "macro_idx": idx}
                logger.info("MIDI learn: waiting for CC → macro %d", idx)
                return {"type": "midi_learn_waiting", "macro_idx": idx}
            if "preset_slot" in msg:
                slot = int(msg.get("preset_slot", 0))
                self._midi_learn_target = {"kind": "preset", "preset_slot": slot}
                logger.info("MIDI learn: waiting for CC → preset slot %d", slot)
                return {"type": "midi_learn_waiting", "preset_slot": slot}
            fader_id = msg.get("id", 0)
            alt = msg.get("alt", 0)
            self._midi_learn_target = {"kind": "fader", "id": fader_id, "alt": alt}
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

    def _program_change_callback(self, program: int):
        if 0 <= program < self.presets.num_slots:
            self._controls.submit(("program", program))

    def _apply_program_change(self, program: int):
        """MIDI 0xC0 program change → load preset slot. Used for hands-free
        footswitch advance via a class-compliant footswitch with PC output,
        or by an external sequencer cueing scenes. Programs 0..9 map directly
        to preset slots 0..9; programs >9 are ignored."""
        if 0 <= program < self.presets.num_slots:
            result = self._handle_ws_message({"type": "preset_load", "slot": program})
            if self.ws_server:
                self.ws_server.broadcast_sync(result)

    def _cc_callback(self, cc_num: int, cc_val: int):
        """Keep note dispatch independent of JSON writes and scene application."""
        target = self._cc_map.get(str(cc_num), {})
        ordered = self._midi_learn_active or target.get("kind") == "preset"
        self._controls.submit(("cc", cc_num, cc_val), key=None if ordered else ("cc", cc_num))

    def _apply_queued_control(self, event, generation):
        with self._control_lock:
            if not self._controls.is_current(generation):
                return
            if event[0] == "cc":
                self._apply_cc(event[1], event[2])
            else:
                self._apply_program_change(event[1])

    def _apply_cc(self, cc_num: int, cc_val: int):
        """Called from JACK engine MIDI thread on CC messages."""
        with self._midi_learn_lock:
            if self._midi_learn_active and self._midi_learn_target:
                # Learn mode: map this CC to the selected target (fader or macro)
                cc_key = str(cc_num)
                target = self._midi_learn_target.copy()
                self._cc_map[cc_key] = target
                self._save_cc_map()
                kind = target.get("kind")
                if kind == "macro":
                    logger.info("Mapped CC %d → macro %s", cc_num, target.get("macro_idx"))
                elif kind == "preset":
                    logger.info("Mapped CC %d → preset slot %s", cc_num, target.get("preset_slot"))
                else:
                    logger.info("Mapped CC %d → fader %s alt=%s",
                                cc_num, target.get("id"), target.get("alt"))
                # Stay in learn mode so user can map multiple CCs without
                # re-tapping MIDI. Only clear target so next tap selects fresh.
                self._midi_learn_target = None
                if self.ws_server:
                    self.ws_server.broadcast_sync({
                        "type": "midi_learn_mapped",
                        "cc": cc_num,
                        "map": dict(self._cc_map),
                    })
                return

            # Normal mode: apply CC value to mapped fader OR macro
            cc_key = str(cc_num)
            target = self._cc_map.get(cc_key)

        if target:
            value = cc_val / 127.0
            kind = target.get("kind", "fader")  # legacy maps lack kind → fader
            if kind == "macro":
                result = self._handle_ws_message({"type": "macro_value",
                                                  "idx": int(target["macro_idx"]), "value": value})
                if self.ws_server:
                    self.ws_server.broadcast_sync(result)
                return
            if kind == "preset":
                # Edge-trigger on rising edge so a momentary footswitch
                # (press 127 / release 0) loads exactly once. An absolute
                # pot held at 127 won't re-fire either since the callback
                # only runs on CC messages received.
                last = self._cc_last_value.get(cc_key, 0)
                self._cc_last_value[cc_key] = cc_val
                if last < 64 and cc_val >= 64:
                    slot = int(target.get("preset_slot", 0))
                    self._apply_program_change(slot)
                return
            fader_msg = {
                "type": "fader",
                "id": target["id"],
                "value": value,
                "alt": target["alt"],
            }
            result = self._handle_ws_message(fader_msg)
            if result.get("type") == "error":
                if self.ws_server:
                    self.ws_server.broadcast_sync(result)
                return
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
            # `note` is the transposed pad note; LAYER widget wants the
            # most recent key for its live indicator.
            if self.ws_server:
                now = time.monotonic()
                last = getattr(self, "_midi_activity_last_ts", 0.0)
                if now - last >= 0.1:
                    self._midi_activity_last_ts = now
                    self.ws_server.broadcast_sync({"type": "midi_activity", "event": "on", "note": int(note)})
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
            if self._stop_event.wait(TICK_SEC):
                break
            tick_counter += 1
            stats_counter += 1

            if self.jack and self.ws_server:
                # GR reading — broadcast every tick (20 Hz) for beat-accurate LED.
                # Prefer the Faust path's bargraph when it's active (self-sidechain
                # + STAVE_FAUST_BUS_COMP=1); fall back to Python otherwise.
                try:
                    gr_db = 0.0
                    faust_bc = getattr(self.jack, "_faust_bus_comp", None)
                    if faust_bc is not None and getattr(self.jack, "bus_comp_source", "self") == "self":
                        gr_db = float(faust_bc.current_gr_db)
                    elif hasattr(self.jack, "bus_comp"):
                        gr_db = float(self.jack.bus_comp.current_gr_db)
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
                    payload = {
                        "type": "system_stats",
                        "cpu_percent": round(cpu, 1),
                        "ram_mb": round(mem.rss / 1048576, 1),
                        "health": self._health_status(),
                    }
                    # Xrun count comes from the JACK C bridge on Linux; on Mac
                    # (MacPortAudioIO) the call won't exist. Isolate so a
                    # missing accessor doesn't drop the whole stats payload.
                    if self.jack:
                        try:
                            payload["xruns"] = (
                                self.jack._bridge.bridge_get_xrun_count()
                                + self.jack._bridge.bridge_get_underrun_count()
                            )
                        except Exception:
                            pass
                    self.ws_server.broadcast_sync(payload)
                    if self.jack and self.jack.recorder:
                        status = self.jack.recorder.current_status()
                        if status != getattr(self, "_last_record_status", None):
                            self._last_record_status = copy.deepcopy(status)
                            self.ws_server.broadcast_sync({"type": "record_status", **status})
                except Exception:
                    pass

            # Autosave every AUTOSAVE_INTERVAL
            if tick_counter >= AUTOSAVE_TICKS:
                tick_counter = 0
                if self._running:
                    try:
                        # Skip the disk write (and its fsync) when nothing
                        # changed since the last save — the state embeds 10
                        # setlists × 10 presets, so unconditional 30s writes
                        # were real SD-card write amplification for a synth
                        # that mostly sits at one setting all service.
                        with self._control_lock:
                            diagnostics = getattr(self, "_control_diagnostics", None)
                            if diagnostics is None:
                                snapshot = json.dumps(self.state, sort_keys=True, allow_nan=False)
                            else:
                                with diagnostics.span("autosave_snapshot"):
                                    snapshot = json.dumps(self.state, sort_keys=True, allow_nan=False)
                            if snapshot != getattr(self, "_last_saved_snapshot", None):
                                if diagnostics is None:
                                    save_state(self.state)
                                else:
                                    with diagnostics.span("autosave_save"):
                                        save_state(self.state)
                                self._last_saved_snapshot = snapshot
                    except Exception as e:
                        logger.warning("Auto-save failed: %s", e)

    def start(self):
        """Start all components."""
        logger.info("Starting Stave Synth...")
        self._running = True
        self._controls.start()

        # Ensure JACK is running
        if not ensure_jack_running():
            raise RuntimeError("JACK/PipeWire is not reachable; startup aborted")

        # Initialize presets
        try:
            self.presets.init_defaults(self.state["ui"]["preset_labels"])
            self._rebuild_preset_saved()
        except (ValueError, TypeError, OSError) as exc:
            # A broken preset library must not prevent the already-validated
            # current sound from booting. Keep the original bank for recovery.
            self._control_error = f"Preset library unavailable: {exc}"
            logger.error("%s", self._control_error)
            self.state["ui"]["preset_saved"] = [False] * self.presets.num_slots

        # Start FluidSynth
        try:
            self.piano = FluidSynthPlayer()
            self.piano.start(
                self.state.get("piano", {}).get("soundfont", "Salamander")
            )
            self.piano.update_params(self.state.get("piano", {}))
        except Exception as e:
            # Retain the partially initialized owner for coordinated finally
            # cleanup. A silent piano is not an acceptable stage-ready state.
            raise RuntimeError(f"FluidSynth piano startup failed: {e}") from e

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
                program_change_callback=self._program_change_callback,
            )
            self.jack.master_volume = self.state.get("master", {}).get("volume", 0.85)
            self.jack.transpose = self.state.get("master", {}).get("transpose_semitones", 0)
            self.jack.piano_octave = self.state.get("master", {}).get("piano_octave", 0)
            self.jack.pre_gain = float(self.state.get("master", {}).get("pre_limiter_trim", 1.5))
            self.jack.piano_reverb_send = float(self.state.get("master", {}).get("piano_reverb_send", 0.0))
            self.jack.piano_delay_send = float(self.state.get("master", {}).get("piano_delay_send", 0.0))
            self.jack.saturation_enabled = bool(self.state.get("master", {}).get("saturation_enabled", False))
            self.jack.pitch_bend_enabled = bool(self.state.get("master", {}).get("pitch_bend_enabled", True))
            self.jack.midi_clock_enabled = bool(self.state.get("master", {}).get("midi_clock_enabled", False))
            # LAYER (split) startup state — instrument range on JackEngine
            # mirrors what's saved; per-OSC + shimmer ranges live on synth.
            _m = self.state.get("master", {})
            self.jack.split_enabled = bool(_m.get("split_enabled", False))
            self.synth.split_enabled = self.jack.split_enabled
            self.jack.instrument_split_low = int(_m.get("instrument_split_low", 0))
            self.jack.instrument_split_high = int(_m.get("instrument_split_high", 127))
            self.jack.instrument_split_xfade = int(_m.get("instrument_split_xfade", 0))
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
            # Apply saved low-latency preference before start — the ring
            # modulo picks up g_active_slots on first callback.
            self.jack.set_low_latency_mode(bool(master.get(
                "low_latency_mode", not LOW_RAM_MODE)))  # matches config default
            # Finish heavy disk/native preparation before render readiness.
            # No deferred sfload or whole-bank decode should stall first notes.
            if self.piano:
                if self.piano.wait_for_preload(timeout=30.0) is False:
                    raise RuntimeError("Soundfont preload has not finished; startup aborted")
            self.synth.load_pad_samples()
            from .native_profile import require_native_profile
            native_issues = require_native_profile(synth=self.synth, jack=self.jack,
                                                  piano=self.piano, organ=self.organ,
                                                  instrument_mode=self.instrument_mode)
            self._native_profile = {"ready": not native_issues, "issues": list(native_issues)}
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
        self._ui_watch_thread = threading.Thread(target=self._ui_watch_loop,
                                                  name="stave-ui-watch", daemon=True)
        self._ui_watch_thread.start()

        # Start autosave
        self._autosave_thread = threading.Thread(target=self._autosave_loop, daemon=True)
        self._autosave_thread.start()

        # Start ALSA-to-JACK MIDI bridge and auto-connect
        self._setup_midi_bridge()

        # FluidSynth audio now rendered through Python pipeline — no JACK connection needed

        # systemd watchdog: keep us auto-recoverable from hangs (not just crashes).
        self._setup_systemd_watchdog()

        # Cap recordings-dir growth (appliance SD card) — prune oldest takes
        # at startup, never mid-set.
        try:
            from .recorder import prune_recordings
            pruned = prune_recordings()
            if pruned:
                logger.info("Pruned %d old recording(s) to stay under the size cap", pruned)
        except Exception as e:
            logger.debug("Recordings prune skipped: %s", e)

        logger.info("Stave Synth is running!")
        logger.info("  UI: http://localhost:%d", HTTP_PORT)

    def _health_status(self):
        from .state_persistence import last_load_warning
        status = {"control_error": self._control_error, "state_warning": last_load_warning,
                  "native_profile": getattr(self, "_native_profile", None),
                  "controls": self._controls.status(), "ui_recovery": self._ui_recovery.status(),
                  "ui": self.ws_server.health_status() if self.ws_server else {"healthy": False}}
        diagnostics = getattr(self, "_control_diagnostics", None)
        if diagnostics is not None:
            try:
                status["control_diagnostics"] = diagnostics.snapshot()
            except Exception:
                status["control_diagnostics"] = {"enabled": True, "available": False,
                                                  "diagnostic_errors": 1}
        elif getattr(self, "_control_diagnostics_unavailable", False):
            status["control_diagnostics"] = {"enabled": True, "available": False,
                                              "diagnostic_errors": 1}
        if self.jack:
            bridge = self.jack._bridge
            status["audio"] = {"error": getattr(self.jack, "_last_error", None),
                               "sample_rate": int(bridge.bridge_get_sample_rate()),
                               "block_frames": int(bridge.bridge_get_buffer_size()),
                               "graph_error": int(bridge.bridge_get_graph_error()),
                               "midi_dropped": int(bridge.bridge_get_midi_drop_count()),
                               "midi_recoveries": int(bridge.bridge_get_midi_recovery_count()),
                               "ring_slots": int(bridge.bridge_get_ring_slots())}
            metrics = getattr(self.jack, "render_metrics", None)
            snapshot = getattr(metrics, "snapshot", None)
            if callable(snapshot):
                try:
                    status["audio"]["render_metrics"] = snapshot()
                except Exception as exc:
                    logger.debug("Render metrics snapshot unavailable: %s", exc)
            diagnostics = getattr(self.jack, "render_diagnostics", None)
            if diagnostics is not None:
                try:
                    status["audio"]["render_diagnostics"] = diagnostics.snapshot()
                except Exception as exc:
                    status["audio"]["render_diagnostics"] = {"available": False}
                    logger.debug("Render diagnostics unavailable: %s", exc)
            synth_status = getattr(getattr(self, "synth", None), "synth_diagnostics_status", None)
            if callable(synth_status):
                try:
                    status["audio"]["synth_diagnostics"] = synth_status()
                except Exception as exc:
                    status["audio"]["synth_diagnostics"] = {"available": False}
                    logger.debug("Synth diagnostics unavailable: %s", exc)
            reverb = getattr(getattr(self, "synth", None), "reverb", None)
            reverb_status = getattr(reverb, "get_diagnostics_status", None)
            if callable(reverb_status):
                try:
                    status["audio"]["reverb_diagnostics"] = reverb_status()
                except Exception as exc:
                    status["audio"]["reverb_diagnostics"] = {"available": False}
                    logger.debug("Reverb diagnostics unavailable: %s", exc)
            piano_status = getattr(getattr(self, "piano", None),
                                   "midi_render_status", None)
            if callable(piano_status):
                try:
                    status["audio"]["piano_midi"] = piano_status()
                except Exception as exc:
                    status["audio"]["piano_midi"] = {"available": False}
                    logger.debug("Piano MIDI snapshot unavailable: %s", exc)
        return status

    def _ui_watch_loop(self):
        previous_callbacks = None
        while not self._stop_event.wait(5.0):
            try:
                callbacks = int(self.jack._bridge.bridge_get_callback_count()) if self.jack else 0
                last = getattr(self.jack, "last_iter_ts", 0.0)
                audio_healthy = bool(last and time.perf_counter() - last < 5.0
                                     and previous_callbacks is not None and callbacks > previous_callbacks
                                     and not getattr(self.jack, "_last_error", None))
                previous_callbacks = callbacks
                with self._ui_lifecycle_lock:
                    self._ui_recovery.tick(self, lambda: WebSocketServer(
                        message_handler=self._handle_ws_message), audio_healthy=audio_healthy)
                if self._ui_recovery.last_error:
                    logger.error("UI recovery: %s", self._ui_recovery.last_error)
            except Exception:
                logger.exception("UI health check failed")

    def _setup_systemd_watchdog(self):
        """Wire up a systemd Type=notify watchdog heartbeat.

        Active only when launched under a systemd unit configured with
        `WatchdogSec=`. systemd exports `NOTIFY_SOCKET` + `WATCHDOG_USEC` in
        that case; otherwise this is a silent no-op (manual launch, dev runs).

        The heartbeat is gated on `jack.last_iter_ts` — if the render loop
        wedges, the timestamp stops advancing and we stop pinging, so systemd
        kills + restarts us per `Restart=on-failure`.
        """
        if ISOLATED:
            return  # Never notify a parent/production unit from a test process.
        notify_socket = os.environ.get("NOTIFY_SOCKET")
        watchdog_usec = os.environ.get("WATCHDOG_USEC")
        if not notify_socket:
            return

        def _send(msg: str) -> None:
            try:
                addr = notify_socket
                if addr.startswith("@"):
                    addr = "\0" + addr[1:]
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                    s.sendto(msg.encode(), addr)
            except Exception as e:
                logger.debug("systemd notify failed: %s", e)

        # READY must go out unconditionally under Type=notify — the unit
        # isn't "started" until systemd hears it (even if WatchdogSec were
        # ever removed, startup must not hang).
        _send("READY=1")

        try:
            timeout_s = int(watchdog_usec) / 1_000_000.0 if watchdog_usec else 0.0
        except ValueError:
            timeout_s = 0.0
        if timeout_s <= 0.0:
            return  # READY sent; no watchdog configured

        # Ping at half the watchdog timeout per systemd's recommendation.
        interval_s = max(2.0, timeout_s / 2.0)
        # Stale threshold: if the render loop hasn't ticked in this many
        # seconds, stop pinging so systemd notices.
        stale_s = max(5.0, timeout_s * 0.8)

        def _heartbeat():
            prev_cb = -1
            while self._running:
                if self._stop_event.wait(interval_s):
                    break
                if not self.jack:
                    continue
                last = getattr(self.jack, "last_iter_ts", 0.0)
                if last <= 0.0:
                    continue  # render thread hasn't started yet
                if time.perf_counter() - last >= stale_s:
                    continue  # render loop stalled — withhold ping
                # A ticking render loop isn't proof of audio: if PipeWire
                # wedges WITHOUT firing the JACK shutdown callback, the
                # render loop fills the ring then idles with fresh
                # timestamps while nothing is consumed. Require the JACK
                # process callback to have advanced since the last ping too.
                try:
                    cb = int(self.jack._bridge.bridge_get_callback_count())
                except Exception:
                    cb = -1  # bridge unavailable — fall back to old behavior
                if cb != -1:
                    if cb == prev_cb:
                        logger.warning(
                            "JACK process callback stalled (count=%d) — "
                            "withholding watchdog ping", cb)
                        continue
                    prev_cb = cb
                _send("WATCHDOG=1")

        threading.Thread(target=_heartbeat, daemon=True).start()
        logger.info("systemd watchdog: heartbeat every %.1fs (timeout %.1fs)",
                    interval_s, timeout_s)

    def _setup_midi_bridge(self):
        """Start a2jmidid and kick off the MIDI auto-connect watcher.

        Idempotent: checks for an existing a2jmidid before spawning so
        systemd restarts don't stack zombie children. On a clean boot the
        pgrep fails (no existing process), we spawn one. On restart, the
        old a2jmidid is usually killed by the cgroup cleanup anyway — but
        the gate is cheap belt-and-suspenders if not.
        """
        if ISOLATED:
            logger.info("Isolated instance: automatic MIDI/audio routing disabled")
            return
        try:
            # pgrep returns 0 if any process matches — skip spawn if so.
            existing = subprocess.run(
                ["pgrep", "-x", "a2jmidid"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            if existing.returncode != 0:
                subprocess.Popen(
                    ["a2jmidid", "-e"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                # Poll for a2jmidid registering its JACK ports instead of the
                # blind 1-second sleep. Typically <200ms; cap at 1s so a
                # broken a2j doesn't block startup.
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    try:
                        result = subprocess.run(
                            ["jack_lsp", "a2j:"],
                            capture_output=True, timeout=0.5,
                        )
                        if result.returncode == 0 and result.stdout.strip():
                            break
                    except Exception:
                        pass
                    time.sleep(0.05)
            else:
                logger.info("a2jmidid already running — reusing existing instance")
        except Exception as e:
            logger.warning("Failed to start a2jmidid: %s", e)

        # Do one immediate connect attempt, then start the watcher
        self._connect_midi_ports()
        self._midi_watch_thread = threading.Thread(
            target=self._midi_watch_loop, daemon=True
        )
        self._midi_watch_thread.start()

        # Audio output watcher — restores last-active sink at startup, and
        # re-routes if the user's preferred DAC gets unplugged + replugged
        # mid-set. Without this, every venue requires a manual MENU → CONN
        # → pick output cycle.
        self._audio_watch_thread = threading.Thread(
            target=self._audio_watch_loop, daemon=True
        )
        self._audio_watch_thread.start()

    def _get_midi_capture_ports(self) -> list[str]:
        """Discover capture ports; remove only unambiguous per-device aliases."""
        from .routing import read_ports, midi_capture_selection
        with self._control_lock:
            try:
                ports, duplicates = midi_capture_selection(
                    read_ports(_run_with_hard_timeout), client=JACK_CLIENT_NAME)
                self._midi_capture_duplicates = duplicates
                self._midi_discovery_error = None
                return ports
            except Exception as exc:
                self._midi_capture_duplicates = {}
                self._midi_discovery_error = str(exc)
                return []

    def _get_port_connections(self, port: str) -> list[str]:
        """Read exact connection rows with the same bounded worker admission."""
        from .routing import read_connections
        try:
            return sorted(read_connections(_run_with_hard_timeout).get(port, set()))
        except Exception:
            return []

    def _connect_midi_ports(self):
        """Connect verified device sources only to this instance's MIDI input."""
        from .routing import connect_midi_sources
        midi_input = f"{JACK_CLIENT_NAME}:midi_in"
        if ISOLATED:
            return bool(self._get_port_connections(midi_input))
        with self._control_lock:
            if self._stopping:
                return False
            if not hasattr(self, "_routing_lock"):
                self._routing_lock = threading.RLock()
            try:
                with self._routing_lock:
                    ports = self._get_midi_capture_ports()
                    if self._midi_discovery_error:
                        return False
                    return connect_midi_sources(_run_with_hard_timeout, JACK_CLIENT_NAME,
                                                ports, self._midi_capture_duplicates,
                                                should_stop=lambda: self._stopping)
            except Exception as exc:
                self._midi_discovery_error = str(exc)
                logger.warning("MIDI routing did not verify: %s", exc)
                return False

    def _audio_watch_loop(self):
        """Reconcile only the saved stereo preference; never choose a fallback."""
        from .routing import read_ports, read_connections, stereo_sinks, route_matches
        if ISOLATED:
            return
        first_run = True
        while self._running and not self._stopping:
            if self._stop_event.wait(0.5 if first_run else 2.5):
                break
            first_run = False
            try:
                # Same lock order as explicit selection. A user change or
                # shutdown cannot be overwritten by a stale watcher snapshot.
                with self._control_lock:
                    if self._stopping or not self._running:
                        break
                    pref = self.state.get("master", {}).get("audio_output_pref")
                    if not pref:
                        continue
                    sinks = stereo_sinks(read_ports(_run_with_hard_timeout), JACK_CLIENT_NAME)
                    if pref not in sinks:
                        continue
                    graph = read_connections(_run_with_hard_timeout)
                    if not route_matches(graph, JACK_CLIENT_NAME, sinks[pref]):
                        result = self._handle_set_audio_output({"name": pref})
                        if not result.get("success"):
                            logger.warning("Preferred audio route was not restored: %s", result.get("error"))
            except Exception as exc:
                logger.debug("audio watch loop iteration error: %s", exc)

    def _midi_watch_loop(self):
        """Poll for new MIDI devices every 2 seconds and auto-connect them.
        Also tracks current connection state and pushes status to the UI on
        change — drives the top-bar MIDI indicator (solid green when a
        keyboard is talking, dim when none)."""
        last_connected = None  # force first broadcast
        while self._running:
            if self._stop_event.wait(2):
                break
            try:
                connected = bool(self._connect_midi_ports())
                self._midi_connected = connected
                if connected != last_connected and self.ws_server:
                    self.ws_server.broadcast_sync({
                        "type": "midi_status", "connected": connected,
                    })
                    last_connected = connected
            except Exception:
                pass

    def stop(self, *, save_final_state=True):
        """Retire every owner before freeing its native dependencies."""
        logger.info("Shutting down Stave Synth...")
        self._stopping = True
        self._running = False
        self._stop_event.set()
        self._crossfade_cancel.set()
        drone_cancel = getattr(self, "_drone_fade_cancel", None)
        if drone_cancel is not None:
            drone_cancel.set()
        deadline = time.monotonic() + 12.0
        remaining = lambda: max(0.0, deadline - time.monotonic())
        controls_safe = True
        with self._ui_lifecycle_lock:
            if self.ws_server:
                controls_safe = self.ws_server.stop() is True
        if not self._controls.stop(timeout=min(2.0, remaining())):
            controls_safe = False
        for name in ("_crossfade_thread", "_drone_fade_thread", "_autosave_thread",
                     "_midi_watch_thread", "_audio_watch_thread", "_ui_watch_thread"):
            worker = getattr(self, name, None)
            if worker and worker is not threading.current_thread():
                worker.join(timeout=remaining())
                controls_safe = controls_safe and not worker.is_alive()
        if not controls_safe:
            # Do not free audio pointers a stuck handler still owns. systemd's
            # process-level stop timeout remains the last-resort recovery.
            raise RuntimeError("Control workers did not stop; native resources retained")
        # Drain even on partial-start cleanup, where no final save is allowed.
        if not self._control_lock.acquire(timeout=remaining()):
            raise RuntimeError("Control ownership did not drain; native resources retained")
        self._control_lock.release()
        if self.jack and self.jack.stop(timeout=remaining()) is False:
            raise RuntimeError("Audio workers did not stop; native resources retained")
        if self.piano and self.piano.stop(timeout=remaining()) is False:
            raise RuntimeError("Soundfont loader did not stop; FluidSynth retained")
        if self.organ:
            self.organ.all_notes_off()
        if save_final_state:
            try:
                with self._control_lock:
                    save_state(self.state)
            except Exception as exc:
                logger.error("Final state save failed: %s", exc)
        logger.info("Stave Synth stopped.")


def _run_application():
    """Entry point."""
    app = StaveSynth()
    started = False

    def signal_handler(sig, frame):
        # The finally block owns cleanup, including interruption during start.
        raise SystemExit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        app.start()
        started = True

        # Check if we have a display for native window
        has_display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        use_gui = has_display and "--no-gui" not in sys.argv
        if use_gui and LOW_RAM_MODE:
            # Keep the small-Pi profile headless.
            logger.info("LOW_RAM_MODE: skipping native window — "
                        "open http://<this-pi>:%d from a browser", HTTP_PORT)
            use_gui = False

        if use_gui:
            try:
                import webview

                webview.create_window(
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
            logger.info("Running headless — UI at http://%s:%d", RUNTIME.host, HTTP_PORT)
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
    finally:
        # A listener/native-start failure must release already-started
        # components, without persisting a partially initialized state.
        failure_in_flight = sys.exc_info()[0] is not None
        try:
            app.stop(save_final_state=started)
        except Exception:
            logger.exception("Application cleanup failed")
            if not failure_in_flight:
                raise


def main():
    """Hold one process owner before loading or changing this instance's data."""
    with InstanceLock(RUNTIME):
        _run_application()


if __name__ == "__main__":
    main()
