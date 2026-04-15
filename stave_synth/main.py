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
from .preset_manager import PresetManager
from .websocket_server import WebSocketServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

UI_DIR = Path(__file__).parent.parent / "ui"


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
             "-r", "48000", "-p", "256", "-n", "2", "-S"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
        return True
    except Exception as e:
        logger.error("Failed to start JACK: %s", e)
        return False


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
        self.jack = None
        self.ws_server = None

        self._running = False
        self._autosave_thread = None

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
        elif msg_type == "transpose":
            return self._handle_transpose(msg)
        elif msg_type == "panic":
            return self._handle_panic()
        elif msg_type == "shimmer_toggle":
            return self._handle_shimmer_toggle(msg)
        elif msg_type == "freeze_toggle":
            return self._handle_freeze_toggle(msg)
        elif msg_type == "octave":
            return self._handle_octave(msg)
        elif msg_type == "get_state":
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
        elif msg_type == "setting":
            return self._handle_setting(msg)
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
            if not alt:
                osc_max = self.state["synth_pad"].get("osc1_max", 1.0)
                scaled = value * osc_max
                self.state["synth_pad"]["osc1_blend"] = scaled
                self.synth.osc1_blend = scaled
            else:
                osc_max = self.state["synth_pad"].get("osc2_max", 1.0)
                scaled = value * osc_max
                self.state["synth_pad"]["osc2_blend"] = scaled
                self.synth.osc2_blend = scaled

        elif fader_id == 1:  # Piano: Volume(0) / Tone(1) / Compressor(2)
            alt_state = int(alt) if isinstance(alt, (int, float)) else (1 if alt else 0)
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
                # Compressor amount: 0=off, 1=heavy compression
                # Maps to threshold: 0dB (off) down to -30dB (heavy)
                if value > 0.01:
                    self.state["piano"]["comp_enabled"] = True
                    self.state["piano"]["comp_threshold_db"] = -30.0 * value
                    self.state["piano"]["comp_makeup_db"] = 6.0 * value
                else:
                    self.state["piano"]["comp_enabled"] = False
                if self.piano:
                    self.piano.comp_enabled = self.state["piano"]["comp_enabled"]
                    self.piano.comp_threshold_db = self.state["piano"].get("comp_threshold_db", -12)
                    self.piano.comp_makeup_db = self.state["piano"].get("comp_makeup_db", 0)

        elif fader_id == 2:  # Filter (standalone, no alt)
            f_min = self.state["synth_pad"].get("filter_range_min", 150)
            f_max = self.state["synth_pad"].get("filter_range_max", 20000)
            freq = f_min * ((f_max / f_min) ** value)
            self.state["synth_pad"]["filter_cutoff_hz"] = freq
            self.synth.filter_cutoff = freq

        elif fader_id == 3:  # FX: Reverb Mix(0) / Shimmer Vol(1)
            alt_state = int(alt) if isinstance(alt, (int, float)) else (1 if alt else 0)
            if alt_state == 0:
                self.state["synth_pad"]["reverb_dry_wet"] = value
                self.synth.reverb.dry_wet = value
            elif alt_state == 1:
                self.state["synth_pad"]["shimmer_mix"] = value
                self.synth.shimmer_mix = value

        elif fader_id == 4:  # Master Volume
            if not alt:
                self.state["master"]["volume"] = value
                if self.jack:
                    self.jack.master_volume = value

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
            from .config import DEFAULT_STATE, _deep_merge
            import json
            defaults = json.loads(json.dumps(DEFAULT_STATE))
            self.state = _deep_merge(defaults, state)
            # Rebuild preset_saved from disk — never trust the snapshot
            self._rebuild_preset_saved()
            self.synth.update_params(state.get("synth_pad", {}))
            if self.piano:
                self.piano.update_params(state.get("piano", {}))
            master = state.get("master", {})
            if self.jack:
                self.jack.master_volume = master.get("volume", 0.85)
                self.jack.transpose = master.get("transpose_semitones", 0)
            self.midi.set_transpose(master.get("transpose_semitones", 0))
            logger.info("Loaded preset slot %d", slot)
            # Send both state update and loaded ack
            if self.ws_server:
                self.ws_server.broadcast_sync({"type": "state", "state": self.state})
            return {"type": "preset_loaded", "slot": slot}
        return {"type": "error", "message": f"Failed to load preset {slot}"}

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
            save_state(self.state)
            return {"type": "preset_deleted", "slot": slot}
        return {"type": "error", "message": f"Failed to delete preset {slot}"}

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

    def _handle_freeze_toggle(self, msg: dict) -> dict:
        enabled = msg.get("enabled")
        if enabled is None:
            enabled = not self.synth.freeze_enabled
        self.synth.freeze_enabled = enabled
        self.synth.reverb.set_freeze(enabled)
        self.state["synth_pad"]["freeze_enabled"] = enabled
        return {"type": "freeze_ack", "enabled": enabled}

    def _handle_panic(self) -> dict:
        """All notes off on every instrument."""
        self.synth.all_notes_off()
        if self.piano:
            self.piano.all_notes_off()
        logger.info("PANIC — all notes off")
        return {"type": "panic_ack"}

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
        import subprocess
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
        import subprocess
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
                    if i + 1 < len(lines) and lines[i + 1].startswith("   "):
                        dest = lines[i + 1].strip()
                        subprocess.run(
                            ["pw-jack", "jack_disconnect", stripped, dest],
                            capture_output=True, timeout=3
                        )
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

    def _handle_setting(self, msg: dict) -> dict:
        """Handle deep settings changes from the settings menu."""
        section = msg.get("section", "")
        param = msg.get("param", "")
        value = msg.get("value")

        if section == "synth_pad":
            if param in ("osc1_max", "osc2_max"):
                self.state["synth_pad"][param] = value
                # No synth param to update — max is applied when fader moves
            elif param in self.state["synth_pad"]:
                self.state["synth_pad"][param] = value
                self.synth.update_params({param: value})
            elif param.startswith("adsr."):
                adsr_key = param.split(".", 1)[1]
                self.state["synth_pad"]["adsr"][adsr_key] = value
                self.synth.update_params({"adsr": {adsr_key: value}})
        elif section == "piano":
            if param in self.state["piano"]:
                self.state["piano"][param] = value
                if self.piano:
                    self.piano.update_params({param: value})
                    if param == "enabled" and not value:
                        self.piano.all_notes_off()

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
            if self.ws_server:
                self.ws_server.broadcast_sync({"type": "midi_activity", "event": "on"})
        elif event_type == "note_off":
            self.midi.on_note_off(note)
        elif event_type == "all_notes_off":
            self.midi.all_notes_off()

    def _autosave_loop(self):
        """Periodically save state and broadcast peak levels."""
        peak_counter = 0
        silence_ticks = 0  # how many 200ms ticks we've been silent
        while self._running:
            time.sleep(0.2)  # Check peaks every 200ms
            peak_counter += 1

            # Broadcast peak level for meters
            if self.jack and self.ws_server:
                peak = self.jack.get_and_reset_peak()
                if peak > 0.01:
                    self.ws_server.broadcast_sync({"type": "peak_level", "peak": peak})
                    silence_ticks = 0
                elif silence_ticks < 5:
                    # Send zeros for ~1 second after signal stops to clear meters
                    self.ws_server.broadcast_sync({"type": "peak_level", "peak": 0.0})
                    silence_ticks += 1

            # Autosave every AUTOSAVE_INTERVAL
            if peak_counter >= int(AUTOSAVE_INTERVAL / 0.2):
                peak_counter = 0
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

        # Start JACK engine (piano audio rendered through our pipeline)
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
            self.jack.start()
        except Exception as e:
            logger.error("Failed to start JACK engine: %s", e)
            logger.info("Make sure JACK is running: jackd -d alsa -r 48000 -p 256")
            sys.exit(1)

        # Start WebSocket + HTTP server
        self.ws_server = WebSocketServer(message_handler=self._handle_ws_message)
        self.ws_server.start()

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
        """List a2j MIDI capture ports (real devices, not Midi Through)."""
        try:
            result = subprocess.run(
                ["jack_lsp", "-t"], capture_output=True, text=True, timeout=5
            )
            ports = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if (line.startswith("a2j:") and "capture" in line
                        and "Midi Through" not in line):
                    ports.append(line)
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

        # Save final state
        try:
            save_state(self.state)
        except Exception:
            pass

        if self.jack:
            self.jack.stop()
        if self.piano:
            self.piano.stop()
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
