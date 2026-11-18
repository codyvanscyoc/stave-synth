#!/usr/bin/env python3
"""Explicit native512 candidate controller; never installs/stops a service.

Owns HTTP, saved knobs, exact device selection and bounded child recovery.
The child alone owns audio. Every new audio owner starts MUTED and receives
only saved absolute controls: notes/freeze/bed triggers are never replayed;
an explicitly saved Master target restores last, after healthy routing and tone.
Not legacy feature parity or completed stage qualification.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import importlib.util
import ipaddress
import json
import math
import os
import re
from pathlib import Path
import signal
import socketserver
import stat
import subprocess
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


audition = module("stave_candidate_protocol", ROOT / "tools/native_v2_audition.py")
storage = module("stave_candidate_store", ROOT / "native_v2/control_store.py")
lifecycle = module("stave_native_lifecycle", ROOT / "tools/native_v2_lifecycle.py")
assets = module("stave_candidate_assets", ROOT / "native_v2/bed_assets.py")


def parse_ports(text):
    if len(text) > 262144:
        raise ValueError("Port inventory exceeds bound")
    ports = {}
    name = None
    for line in text.splitlines():
        if line and not line[0].isspace():
            if len(ports) >= 512 or len(line) > 256:
                raise ValueError("Port inventory exceeds bound")
            name = line
            ports[name] = {"flags": [], "type": ""}
        elif name:
            value = line.strip()
            if value.startswith("properties:"):
                ports[name]["flags"] = [v.strip() for v in value[11:].split(",") if v.strip()]
            elif value:
                ports[name]["type"] = value
    return ports


def missing_routes(routes, ports):
    missing = {}
    for key, name in routes.items():
        port = ports.get(name, {})
        kind, direction = ("8 bit raw midi", "output") if key == "midi_source" else ("32 bit float mono audio", "input")
        if port.get("type") != kind or direction not in port.get("flags", ()):
            missing[key] = name
    return missing


def validate_routes(routes, ports=None):
    if not isinstance(routes, dict) or set(routes) != {"midi_source", "audio_left", "audio_right"}:
        raise ValueError("Explicit MIDI and stereo route required")
    if any(not isinstance(v, str) or not 1 <= len(v) <= 256 or any(ord(c) < 32 for c in v) for v in routes.values()):
        raise ValueError("Invalid route name")
    if routes["audio_left"] == routes["audio_right"]:
        raise ValueError("Select distinct left/right audio ports")
    for key, value in routes.items():
        if value.startswith(("StaveSynth:", "stave-v2-")):
            raise ValueError("Instrument/probe ports cannot be device selections")
    if ports is not None:
        missing = missing_routes(routes, ports)
        if missing:
            raise ValueError("Waiting for selected ports: " + ", ".join(missing.values()))
    return dict(routes)


class HeadphoneMixer:
    """Non-audio-thread control of the Pi analogue mixer, never USB outputs."""
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.warning = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError('Invalid headphone state file')
                raw = stream.read(1025)
                if len(raw) > 1024:
                    raise ValueError('Oversized headphone state file')
            self.apply(json.loads(raw))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as error:
            self.warning = str(error)

    def run(self, *args):
        try:
            result = subprocess.run(['/usr/bin/amixer', '-c', 'Headphones', *args],
                                    capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError('Pi headphone mixer unavailable') from error
        if result.returncode:
            raise ValueError('Pi headphone mixer unavailable')
        match = re.search(r'\[(\d+)%\].*?\[(on|off)\]', result.stdout)
        if not match:
            raise ValueError('Cannot read Pi headphone mixer')
        return dict(volume=int(match[1]), muted=match[2] == 'off')

    def get(self):
        with self.lock:
            try:
                return dict(available=True, **self.run('sget', 'PCM'), warning=self.warning)
            except ValueError as error:
                return dict(available=False, error=str(error))

    def apply(self, data):
        if (not isinstance(data, dict) or set(data) != {'volume', 'muted'}
                or type(data['volume']) is not int or not 0 <= data['volume'] <= 100
                or type(data['muted']) is not bool):
            raise ValueError('Headphone volume must be 0–100 with a boolean mute')
        return self.run('sset', 'PCM', str(data['volume']) + '%',
                        'mute' if data['muted'] else 'unmute')

    def set(self, data):
        with self.lock:
            if self.path.is_symlink():
                raise ValueError('Symlink headphone state destination refused')
            actual = self.apply(data)
            try:
                storage.atomic.atomic_write_json(self.path, actual, max_bytes=1024)
            except OSError:
                self.warning = 'Headphone level changed, but saving the startup level failed. Check available storage and try again.'
                return dict(available=True, **actual, warning=self.warning)
            self.warning = None
            return dict(available=True, **actual)


class CandidateHub:
    def __init__(self, store, routes, recording_dir=None, pad_library=None):
        self.lock = threading.RLock()
        self.store = store
        self.map_change_lock = threading.Lock()
        self.learn_owner = None
        self.learn_until = 0.0
        self.active_preset = None
        self.output_muted = False
        self.headphones = HeadphoneMixer(store.path.with_name("headphone_output.json"))
        self.route_file = store.path.with_name("native_routes.json")
        self.routes = {} if routes is None else validate_routes(routes)
        self.saved = {}
        self.presets = None
        self.midi_map = None
        self.save_message = "No native sound saved yet. Master stays muted until you save a sound with a deliberate level."
        self.state_warning = None
        try:
            self.saved = store.load()
            if self.saved:
                self.save_message = "Saved native sound loaded. Master restores last after healthy routing."
        except (OSError, ValueError, TypeError) as error:
            self.state_warning = "Saved-state warning: " + str(error)
            self.save_message = self.state_warning + "; file preserved. Save only when the new sound is intentional."
        try:
            if self.route_file.exists() or self.route_file.is_symlink():
                fd = os.open(self.route_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise ValueError("Routes must be a regular JSON file")
                    data = source.read(4097)
                if len(data) > 4096:
                    raise ValueError("Oversized route state")
                value = json.loads(data)
                if set(value) != {"schema", "routes"} or type(value["schema"]) is not int or value["schema"] != 1:
                    raise ValueError("Unsupported route schema")
                self.routes = validate_routes(value["routes"])
        except (OSError, ValueError, TypeError) as error:
            self.state_warning = "Saved-route warning: " + str(error)
        try:
            self.presets = storage.PresetStore(store.path.with_name("native_presets.json"), store)
        except (OSError, ValueError, TypeError) as error:
            self.state_warning = "Preset warning: " + str(error)
        try:
            self.midi_map = storage.MidiMapStore(store.path.with_name("native_midi_map.json"), audition.validate_midi_mapping)
        except (OSError, ValueError, TypeError) as error:
            self.state_warning = "MIDI-map warning: " + str(error)
        self.active = None
        # Preparation belongs to the instrument, not to a connected keyboard.
        # Only acknowledged tone values enter this draft; never performance actions.
        self.prepared = self.saved.copy()
        self.prepared_volume_link_offset = (self.prepared.get("osc1", 0) - self.prepared.get("osc2", 0)
                                            if self.prepared.get("volume_link", 0) else None)
        self.epoch = uuid.uuid4().hex
        self.restoring = True
        self.restore_sequence = None
        self.map_restore_queued = False
        self.map_restored = False
        self.startup_started = 0
        self.restore_started = None
        self.message = ("Waiting for selected devices." if self.routes else
                        "Open Soundcheck to choose a MIDI keyboard and stereo output.")
        self.restart = False
        self.ports = {}
        self.restarts = 0
        self.network = {}
        self.recording_dir = Path(recording_dir) if recording_dir else None
        self.pad_library = Path(pad_library) if pad_library else None
        self.recording_take = None
        self.assigning = False
        self.bank_status = dict(used_bytes=None, limit_bytes=assets.BANK_BYTES, slots=0, warning=None,
                                slot_seconds=[0.] * len(assets.FILENAMES), remaining_seconds=None)
        self.next_bank = None
        if self.recording_dir is not None:
            completed = [p for p in self.recording_dir.glob("take-*.wav") if p.is_file() and not p.is_symlink()]
            if completed:
                self.recording_take = max(completed, key=lambda p: p.stat().st_mtime_ns)

    def attach(self, child, recording_take=None):
        with self.lock:
            self.learn_owner = None
            self.learn_until = 0.0
            self.active = audition.Controller(child, instance="native-v2-stage")
            if recording_take is not None:
                self.recording_take = Path(recording_take)
            self.epoch = uuid.uuid4().hex
            self.restoring = True
            self.restore_sequence = None
            self.map_restore_queued = False
            self.map_restored = False
            self.startup_started = time.monotonic()
            self.restore_started = None
            self.message = "Starting audio and restoring the saved sound; Master restores last."
            self.restarts += 1
            return self.active

    def detached(self, message):
        with self.lock:
            self.remember_tone()
            self.active = None
            self.epoch = uuid.uuid4().hex
            self.restoring = True
            self.message = message

    def remember_tone(self):
        # Called under hub.lock. During startup, controller defaults must never
        # replace a prepared sound before its restore transaction is acknowledged.
        if self.active is None or self.restoring:
            return
        data = self.active.snapshot()
        for key, value in data['values'].items():
            if key not in storage.TRANSIENT and (not key.startswith('bed_') or data['status'].get('bed_mask')):
                self.prepared[key] = value
        self.prepared_volume_link_offset = self.active.volume_link_offset
        if not data["pending"] and "output_mute" in data["values"] and data["status"].get("blocks"):
            self.output_muted = bool(data["values"]["output_mute"])

    def inventory(self, ports):
        with self.lock:
            self.ports = ports

    def take_restart(self):
        # Consume before stopping the old child; a newer request arriving
        # during teardown must survive for the next supervisor iteration.
        with self.lock:
            requested = self.restart
            self.restart = False
            return requested

    def reconcile(self):
        with self.lock:
            if not self.active:
                return
            if not self.restoring:
                data = self.active.snapshot()
                if data['stale'] or data['exited'] is not None or data['status'].get('fault', 1) or not data['status'].get('routed'):
                    raise ValueError('Audio progress/route lost; restarting muted')
                return
            data = self.active.snapshot()
            now = time.monotonic()
            # Loading FluidR3 from an SD card can take longer than the
            # control acknowledgment budget. Start that clock at first audio.
            if self.restore_started is None:
                if now - self.startup_started > 60:
                    raise ValueError("Audio startup timed out waiting for the selected route")
            elif now - self.restore_started > 10:
                raise ValueError("Saved-control acknowledgment timed out")
            if data["stale"] or data["status"].get("fault", 1) or not data["status"].get("routed"):
                return
            if self.restore_started is None:
                self.restore_started = now
            if not self.map_restored:
                if not self.map_restore_queued:
                    for key, cc in (self.midi_map.mappings.items() if self.midi_map else ()):
                        self.active.map_cc({"key": key, "cc": cc})
                    self.map_restore_queued = True
                mapped = self.active.snapshot()
                if mapped["error"]:
                    raise ValueError("Saved MIDI mapping was rejected: " + mapped["error"])
                if mapped.get("map_pending"):
                    return
                self.map_restored = True
            missing_voice = not audition.piano_voice_available(data["status"], self.prepared.get("piano_voice", 0))
            if self.restore_sequence is None:
                prepared = dict(self.prepared, piano_voice=0) if missing_voice else self.prepared
                items = [(key, value) for key, value in audition.restore_items(prepared)
                         if not key.startswith("bed_") or data["status"].get("bed_mask")]
                if self.output_muted:
                    items.insert(0, ("output_mute", 1))
                self.restore_sequence = self.active.submit_batch(items) if items else 0
            if data["error"]:
                raise ValueError("Saved controls were rejected: " + data["error"])
            if not self.restore_sequence or data["status"].get("applied", 0) >= self.restore_sequence:
                self.restoring = False
                self.message = ("Audio ready with Grand. The loaded SoundFont lacks the saved piano voice."
                                if missing_voice else "Audio ready. Saved Master was restored last through the output ramp.")

    @staticmethod
    def tone_differs(values, reference, include_keyboard=True):
        for key, setting in audition.CONTROLS.items():
            if key in storage.TRANSIENT or key in ("attack", "release"):
                continue
            if not include_keyboard and key in storage.KEYBOARD_CONTROLS:
                continue
            if not math.isclose(values.get(key, setting[3]), reference.get(key, setting[3]),
                                rel_tol=1e-8, abs_tol=1e-7):
                return True
        return False

    def snapshot(self):
        with self.lock:
            self.remember_tone()
            data = self.active.snapshot() if self.active else dict(instance="native-v2-stage", status={},
                values={**{k: v[3] for k, v in audition.CONTROLS.items()}, **self.prepared}, pending=0, error=None, exited=None, stale=True)
            if not data['status'].get('bed_mask'):
                data['values'].update({k: v for k, v in self.prepared.items() if k.startswith('bed_')})
            midi = [name for name, p in self.ports.items() if p["type"] == "8 bit raw midi" and "output" in p["flags"] and not name.startswith(("StaveSynth:", "stave-v2-"))]
            audio = [name for name, p in self.ports.items() if p["type"] == "32 bit float mono audio" and "input" in p["flags"] and not name.startswith(("StaveSynth:", "stave-v2-"))]
            data.update(epoch=self.epoch, runtime=dict(restoring=self.restoring, message=self.message,
                preparation=self.active is None and not self.restart,
                active_preset=self.active_preset,
                preset_dirty=(self.active_preset is not None and
                              self.tone_differs(data["values"], self.presets.controls_for(self.active_preset), False)),
                startup_dirty=self.tone_differs(data["values"], self.saved),
                persistence=True, save_message=self.save_message, state_warning=self.state_warning, can_restart=True,
                attempts=self.restarts, network=dict(self.network), missing_routes=missing_routes(self.routes, self.ports),
                pad_bank=dict(self.bank_status, assigning=self.assigning), devices={"midi": midi, "audio": audio},
                recording={"take": self.recording_take.name if self.recording_take else None,
                           "capture": data['status'].get('capture_end', 0),
                           "writer": data['status'].get('writer_state', 0),
                           "frames": data['status'].get('writer_frames', 0)},
                presets=self.presets.summary() if self.presets else [None] * 5, **self.routes,
                midi_maps=dict(self.midi_map.mappings) if self.midi_map else {},
                scope="Native512 stage engine: selected-device recovery restores saved controls with Master last. Browser disconnect does not stop audio."))
            return data

    def learning(self, data, epoch):
        if (not isinstance(data, dict) or not {"active", "owner"} <= data.keys() <= {"active", "owner", "renew"}
                or type(data["active"]) is not bool or type(data.get("renew", False)) is not bool
                or not isinstance(data["owner"], str) or not re.fullmatch(r"[A-Za-z0-9-]{16,64}", data["owner"])):
            raise ValueError("Invalid MIDI Learn session")
        token = data["owner"]
        with self.map_change_lock:
            with self.lock:
                if self.epoch != epoch or not self.active or self.restoring:
                    raise ValueError("Audio session changed. Open MIDI Learn again.")
                now = time.monotonic()
                owned = self.learn_owner if now < self.learn_until else None
                if owned is not None and owned != token:
                    raise ValueError("Another panel is using MIDI Learn")
                if data.get("renew") and (not data["active"] or owned != token):
                    raise ValueError("MIDI Learn expired. Open it again.")
                if not data["active"] and owned is None:
                    return {"ok": True, "serial": 0}
                engine = self.active
                if data["active"] and engine.snapshot()["status"].get("midi_connected") is False:
                    raise ValueError("Connect the selected keyboard before using MIDI Learn")
                seq = engine.set_learning(data["active"])
            serial = engine.wait_mapping(seq)
            with self.lock:
                if self.epoch != epoch or self.active is not engine:
                    raise ValueError("Audio session changed. Open MIDI Learn again.")
                self.learn_owner = token if data["active"] else None
                self.learn_until = now + 6 if data["active"] else 0.0
            return {"ok": True, "serial": serial, "lease_seconds": 6}

    def change_mapping(self, path, data, epoch):
        token = None
        if path == "/midi-map":
            if isinstance(data, dict) and "owner" in data:
                data = data.copy()
                token = data.pop("owner")
                if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9-]{16,64}", token):
                    raise ValueError("Invalid MIDI Learn owner")
            key, cc = audition.validate_midi_mapping(data)
        else:
            if (not isinstance(data, dict) or set(data) != {"key"} or
                    not isinstance(data["key"], str) or data["key"] not in audition.MIDI_MAPPABLE):
                raise ValueError("Choose a mappable control to clear")
            key, cc = data["key"], None
        with self.map_change_lock:
            with self.lock:
                if self.epoch != epoch or not self.active or self.restoring or not self.midi_map:
                    raise ValueError("Wait for a fresh audio session before changing a MIDI assignment")
                learning = self.learn_owner if time.monotonic() < self.learn_until else None
                if token != learning:
                    raise ValueError("MIDI Learn ownership changed. Open it again.")
                owner = self.active
                before = self.midi_map.mappings.copy()
                after = {k: v for k, v in before.items() if k != key and v != cc}
                if cc is not None:
                    after[key] = cc
                if after == before:
                    return {"midi_maps": before, "message": "Assignment unchanged."}
                seq = owner.clear_mapping(key) if cc is None else owner.map_cc({"key": key, "cc": cc})
            try:
                owner.wait_mapping(seq)
                with self.lock:
                    if self.epoch != epoch or self.active is not owner:
                        raise ValueError("Audio session changed before the assignment was saved")
                    mappings = self.midi_map.save(after)
            except (OSError, ValueError):
                # Reconcile with durable state, including an assignment displaced by this CC.
                with self.lock:
                    same_owner = self.active is owner and self.epoch == epoch
                if same_owner:
                    try:
                        if key not in before:
                            owner.wait_mapping(owner.clear_mapping(key))
                        for old_key, old_cc in before.items():
                            if old_key == key or old_cc == cc:
                                owner.wait_mapping(owner.map_cc({"key": old_key, "cc": old_cc}))
                    except ValueError:
                        with self.lock:
                            if self.active is owner and self.epoch == epoch:
                                self.state_warning = "MIDI assignment could not be confirmed. Restart audio to restore saved assignments."
                raise
            message = "MIDI assignment cleared." if cc is None else f"Mapped CC {cc} to {key}."
            return {"queued": seq, "midi_maps": mappings, "message": message}

    def dispatch(self, path, data, epoch):
        if path in ("/save", "/preset-save"):
            with self.lock:
                if epoch != self.epoch:
                    raise ValueError("Audio session changed; refresh status")
                owner = self.active
                if owner is not None:
                    if self.restoring or self.restart:
                        raise ValueError("Wait for healthy acknowledged controls before saving")
                    initial = owner.snapshot()
                    if initial["pending"] or initial["stale"]:
                        raise ValueError("Wait for healthy acknowledged controls before saving")
            if owner is not None and "control_values" in initial["status"]:
                # MIDI is applied by the audio thread between periodic reports.
                # Leave the hub available to UI, routing and restart requests.
                owner.wait_for_save_snapshot(initial["status"].get("blocks", 0))
        if path == "/midi-learn":
            return self.learning(data, epoch)
        if path == "/record-assign":
            return self.assign_take(data, epoch)
        if path in ("/midi-map", "/midi-unmap"):
            return self.change_mapping(path, data, epoch)
        if path == "/headphones":
            if epoch != self.epoch:
                raise ValueError("Audio session changed; refresh status")
            return self.headphones.set(data)
        with self.lock:
            if epoch != self.epoch:
                raise ValueError("Audio session changed; refresh status. Old actions are never replayed.")
            if path == "/restart-audio":
                if data != {}:
                    raise ValueError("Empty restart request required")
                self.remember_tone()
                self.restart = True
                self.restoring = True
                self.epoch = uuid.uuid4().hex
                return {"message": "Restart requested. Master returns to its previous level once routing and tone are ready."}
            if path == "/routes":
                routes = validate_routes(data)
                if routes == self.routes:
                    return {"message": "Selected connections are unchanged.", "changed": False}
                validate_routes(routes, self.ports)
                if self.route_file.is_symlink():
                    raise ValueError("Symlink route state refused")
                storage.atomic.atomic_write_json(self.route_file, {"schema": 1, "routes": routes}, max_bytes=4096)
                self.remember_tone()
                self.routes = routes
                self.restart = True
                self.restoring = True
                self.epoch = uuid.uuid4().hex
                return {"message": "Connections saved. Master returns to its previous level once the new route is ready."}
            if path == "/preset-load":
                if not isinstance(data, dict) or set(data) != {"slot"} or not self.presets:
                    raise ValueError("Choose one native preset")
                # Device-free presets from earlier versions can omit controls
                # that still had their default value when the preset was saved.
                values = {k: setting[3] for k, setting in audition.CONTROLS.items()
                          if k not in storage.TRANSIENT and k not in storage.KEYBOARD_CONTROLS and k not in ("attack", "release")}
                values.update({k: v for k, v in self.presets.controls_for(data["slot"]).items()
                               if k not in storage.KEYBOARD_CONTROLS})
                if self.active is None and not self.restart:
                    values.update({k: self.prepared[k] for k in storage.KEYBOARD_CONTROLS if k in self.prepared})
                    self.prepared = values
                    self.active_preset = data["slot"]
                    self.prepared_volume_link_offset = (values.get("osc1", 0) - values.get("osc2", 0)
                                                        if values.get("volume_link", 0) else None)
                    return {"message": "Preset prepared; it will sound when devices connect."}
                if not self.active or self.restoring:
                    raise ValueError("Audio is not ready for a preset change")
                status = self.active.snapshot()["status"]
                items = audition.restore_items(values)
                if status.get("bed_mask"):
                    self.active.submit_batch(items)
                else:
                    self.active.submit_batch([(k, v) for k, v in items if not k.startswith("bed_")])
                    self.prepared.update((k, v) for k, v in items if k.startswith("bed_"))
                self.active_preset = data["slot"]
                return {"message": "Preset queued with Master last."}
            if path == "/preset-save":
                if not isinstance(data, dict) or set(data) != {"slot", "name"} or not self.presets:
                    raise ValueError("Choose one native preset slot and name")
                if self.active is not None:
                    snapshot = self.snapshot()
                    if (self.restoring or snapshot["stale"] or snapshot["pending"] or snapshot["exited"] is not None
                            or snapshot["status"].get("fault", 1) or not snapshot["status"].get("routed")):
                        raise ValueError("Wait for healthy acknowledged controls before storing a preset")
                    values = {k: v for k, v in snapshot["values"].items() if k not in storage.TRANSIENT}
                elif not self.restart:
                    values = self.prepared
                else:
                    raise ValueError("Wait for audio recovery before storing a preset")
                values = {k: v for k, v in values.items() if k not in storage.KEYBOARD_CONTROLS}
                summary = self.presets.save_slot(data["slot"], data["name"], values)
                self.active_preset = data["slot"]
                return {"presets": summary, "message": "Preset stored."}
            if self.active is None and not self.restart:
                if path == '/control':
                    key, value = audition.validate_control(data)
                    if key in storage.TRANSIENT:
                        raise ValueError('Output and performance actions require audio; tone controls can be prepared now')
                    audition.validate_control_pair(self.prepared, key, value)
                    self.prepared_volume_link_offset = audition.apply_value(
                        self.prepared, key, value, self.prepared_volume_link_offset)
                    return {'prepared': True, 'applied': False}
                if path == '/save' and data == {}:
                    saved = self.store.save(self.prepared)
                    self.saved = saved
                    self.state_warning = None
                    self.save_message = 'Prepared sound saved. If Master is zero, startup remains muted.'
                    return {'saved': True, 'message': self.save_message}
            if not self.active or self.restoring:
                raise ValueError("Audio is not ready; no queued performance actions")
            if path == "/control":
                key, value = audition.validate_control(data)
                if key.startswith('bed_') and key not in storage.TRANSIENT and not self.active.snapshot()['status'].get('bed_mask'):
                    self.prepared[key] = value
                    return {'prepared': True, 'applied': False}
                seq = self.active.submit(data)
                if key == "output_mute" and value or key == "panic" and value:
                    # Keep an accepted mute across child loss, even before its next status.
                    self.output_muted = True
                return {"queued": seq, "applied": False}
            if path != "/save" or data != {}:
                raise ValueError("Unknown or malformed native action")
            snapshot = self.active.snapshot()
            if (snapshot["stale"] or snapshot["exited"] is not None or snapshot["status"].get("fault", 1)
                    or not snapshot["status"].get("routed") or snapshot["pending"]):
                raise ValueError("Wait for healthy acknowledged controls before saving")
            values = {k: v for k, v in snapshot["values"].items() if k not in storage.TRANSIENT}
            if not snapshot["status"].get("bed_mask"):
                for key, value in self.prepared.items():
                    if key.startswith("bed_"):
                        values[key] = value
            self.saved = self.store.save(values)
            self.prepared = self.saved.copy()
            self.prepared_volume_link_offset = self.active.volume_link_offset
            self.state_warning = None
            self.save_message = "Native sound saved. Master restores last; notes, freeze and bed actions remain excluded."
            return {"saved": True, "message": self.save_message}

    def assign_take(self, data, epoch):
        if (not isinstance(data, dict) or set(data) != {"slot"} or type(data["slot"]) is not int
                or not 0 <= data["slot"] < len(assets.FILENAMES)):
            raise ValueError("Choose one pad key slot")
        slot = data["slot"]
        with self.lock:
            if epoch != self.epoch:
                raise ValueError("Audio session changed; refresh status")
            if self.assigning:
                raise ValueError("Another pad assignment is being validated")
            if not self.active or self.restoring:
                raise ValueError("Audio must be ready before assigning a take")
            status = self.active.snapshot()["status"]
            if status.get("writer_state") != 2 or status.get("capture_end") not in (2, 5):
                raise ValueError("Stop the recording and wait for Ready before assigning it")
            if self.recording_take is None or self.pad_library is None:
                raise ValueError("Recording library is not configured")
            source = self.recording_take
            target = self.pad_library / assets.FILENAMES[slot]
            self.assigning = True

        candidate = None
        try:
            if target.is_symlink() or source.is_symlink():
                raise ValueError("Symlink recording destinations are refused")
            before = assets.inventory(self.pad_library)
            candidate = tempfile.TemporaryDirectory(prefix="stave-native-bank-")
            with tempfile.TemporaryDirectory(dir=self.pad_library, prefix=".assign-") as staging:
                staged_take = Path(staging) / "take.wav"
                self._copy_take(source, staged_take)
                report = assets.prepare_bank(self.pad_library, Path(candidate.name) / "prepared.bank",
                                             replacement=(slot, staged_take))
                with self.lock:
                    if epoch != self.epoch or self.restoring or self.recording_take != source:
                        raise ValueError("Audio session changed during assignment; try again")
                    if assets.inventory(self.pad_library) != before:
                        raise ValueError("Pad library changed during assignment; try again")
                    self._replace_slot(staged_take, target)
                    self.next_bank = candidate
                    candidate = None
                    self._report_bank(report)
                    self.remember_tone()
                    self.restart = True
                    self.restoring = True
                    self.epoch = uuid.uuid4().hex
            return {"message": f"Saved {target.name}. Audio is restarting muted, then restoring the previous Master level."}
        finally:
            if candidate is not None:
                candidate.cleanup()
            with self.lock:
                self.assigning = False

    @staticmethod
    def _copy_take(source, destination):
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as incoming:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 58 <= info.st_size <= assets.SOURCE_BYTES:
                raise ValueError("Invalid or oversized finalized recording")
            out_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(out_fd, "wb") as outgoing:
                remaining = info.st_size
                while remaining:
                    block = incoming.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError("Short recording read")
                    outgoing.write(block)
                    remaining -= len(block)
                if assets.signature(os.fstat(fd)) != assets.signature(info):
                    raise ValueError("Recording changed during assignment")
                outgoing.flush()
                os.fsync(outgoing.fileno())

    @staticmethod
    def _replace_slot(staged, target):
        backup = target.with_name(f"undo-{target.stem}-{uuid.uuid4().hex}.wav") if target.exists() else None
        if backup:
            os.link(target, backup, follow_symlinks=False)
        try:
            os.replace(staged, target)
        except OSError:
            if backup:
                backup.unlink()
            raise
        directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            try:
                os.fsync(directory)
            except OSError:
                pass
        finally:
            os.close(directory)

    def _report_bank(self, report):
        seconds = [0.] * len(assets.FILENAMES)
        for record in report["slots"]:
            seconds[record["slot"]] = record["frames"] / 48000
        self.bank_status.update(used_bytes=report["pcm_bytes"], slots=len(report["slots"]), warning=None,
                                slot_seconds=seconds, remaining_seconds=(assets.BANK_BYTES - report["pcm_bytes"]) / (48000 * 16))

    def prepare_current_bank(self):
        """Runs outside the supervisor loop so decoding cannot starve its watchdog."""
        temporary = None
        try:
            temporary = tempfile.TemporaryDirectory(prefix="stave-native-bank-")
            report = assets.prepare_bank(self.pad_library, Path(temporary.name) / "prepared.bank")
            with self.lock:
                self._report_bank(report)
            return temporary
        except (ValueError, OSError, ImportError, MemoryError) as error:
            if temporary is not None:
                temporary.cleanup()
            with self.lock:
                self.bank_status.update(used_bytes=None, slots=0, remaining_seconds=None, slot_seconds=[0.] * len(assets.FILENAMES),
                    warning=f"Pads unavailable: {error}. Piano and oscillators can still play.")
            return None


class BoundedHTTPServer(socketserver.ThreadingMixIn, audition.HTTPServer):
    daemon_threads = True
    block_on_close = False
    def __init__(self, *args, slots=None, **kwargs):
        self.slots = slots if slots is not None else threading.BoundedSemaphore(4)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("binary", "soundfont", "state-dir"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--listen", required=True)
    for name in ("midi-source", "audio-left", "audio-right"):
        parser.add_argument("--"+name, help="Optional initial route. Supply all three routes or select devices in System.")
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--hostname", action="append", default=[])
    parser.add_argument("--network-interface", action="append", default=[],
                        help="With --listen auto, bind RFC1918 addresses on these interfaces plus loopback")
    parser.add_argument("--pad-library", type=Path)
    parser.add_argument("--allow-stage-candidate", action="store_true")
    args = parser.parse_args()
    routes = {key: getattr(args, key) for key in ("midi_source", "audio_left", "audio_right")}
    if all(value is None for value in routes.values()):
        routes = None
    else:
        try:
            validate_routes(routes)
        except ValueError as error:
            parser.error(str(error))
    automatic = args.listen == 'auto'
    address = ipaddress.ip_address('127.0.0.1' if automatic else args.listen)
    if (automatic and (not args.network_interface or any(n not in ('wlan0', 'eth0') for n in args.network_interface))):
        parser.error('Automatic networking requires explicit wlan0/eth0 interfaces')
    if (not args.allow_stage_candidate or address.version != 4 or not address.is_private or address.is_unspecified or
            address.is_multicast or not 8082 <= args.port <= 8090 or not args.binary.is_file() or not args.soundfont.is_file()):
        parser.error("Explicit candidate opt-in, private IPv4, isolated8082..8090, binary and soundfont required")
    authorities = audition.authority_set(str(address), args.port, args.hostname)
    state_root = args.state_dir.expanduser().resolve()
    for protected in (Path.home()/".config/stave-synth", Path.home()/".local/share/stave-synth"):
        if state_root == protected.resolve() or protected.resolve() in state_root.parents:
            parser.error("Candidate state must not share the working Stave data directory")
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(state_root/"native-stage.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
        os.close(lock_fd)
        parser.error("Invalid candidate lock file")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        parser.error("Candidate state is already owned by another controller")
    recording_root = state_root / "recordings"
    recording_root.mkdir(mode=0o700, exist_ok=True)
    if args.pad_library is not None:
        args.pad_library.mkdir(mode=0o700, parents=True, exist_ok=True)
    hub = CandidateHub(storage.ControlStore(state_root/"native_controls.json", audition.validate_control),
                       routes,
                       recording_root, args.pad_library)
    slots = threading.BoundedSemaphore(4)
    def listener(address):
        allowed = audition.authority_set(address, args.port, args.hostname)
        server = BoundedHTTPServer((address, args.port), audition.handler_for(hub, allowed), slots=slots)
        server.timeout = .1
        return server
    listeners = lifecycle.Listeners(listener)
    child = None
    workers = []
    prepared = None
    bank_worker = ThreadPoolExecutor(max_workers=1)
    bank_job = bank_worker.submit(hub.prepare_current_bank) if args.pad_library else None
    stopped = False
    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True
    prior = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    def stop_child():
        nonlocal child, workers, prepared
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=3)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=3)
            for worker in workers:
                worker.join(timeout=2)
            child.stdin.close(); child.stdout.close()
            child = None; workers = []
        if prepared is not None:
            prepared.cleanup(); prepared = None
    try:
        # Audio does not depend on DHCP/Wi-Fi or a successful HTTP bind.
        # No wildcard listener: LAN addresses are explicitly inventoried.
        listeners.reconcile({str(address)})
        next_inventory = next_launch = next_network = next_notify = 0
        lifecycle.notify('READY=1\nSTATUS=Native controller running; audio readiness is reported separately')
        print("NATIVE512 CANDIDATE: "+", ".join("http://"+a for a in sorted(authorities))+" — starts/restarts MUTED", flush=True)
        while not stopped:
            now = time.monotonic()
            if now >= next_network:
                network_error = None
                try:
                    addresses = lifecycle.discover(args.network_interface) if automatic else {str(address)}
                except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                    addresses = {'127.0.0.1'} if automatic else {str(address)}
                    network_error = str(error)
                listeners.reconcile(addresses)
                with hub.lock:
                    hub.network = dict(addresses=sorted(listeners.servers), errors=dict(listeners.errors), discovery_error=network_error)
                next_network = now + 2
            requested = hub.take_restart()
            if requested or (child is not None and child.poll() is not None):
                code = child.poll() if child is not None else None
                hub.detached("Audio restarting muted." if requested else f"Audio owner stopped ({code}); waiting for selected devices.")
                stop_child()
                next_launch = now + (0 if code is None else 2)
                next_inventory = 0
                with hub.lock:
                    if hub.next_bank is not None:
                        prepared, hub.next_bank = hub.next_bank, None
                    elif args.pad_library is not None and bank_job is None:
                        bank_job = bank_worker.submit(hub.prepare_current_bank)
            if bank_job is not None and bank_job.done():
                prepared = bank_job.result()
                bank_job = None
            if now >= next_inventory:
                try:
                    result = subprocess.run(["/usr/bin/pw-jack", "jack_lsp", "-t", "-p"], capture_output=True, text=True, timeout=2)
                    hub.inventory(parse_ports(result.stdout) if result.returncode == 0 else {})
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    hub.inventory({})
                next_inventory = now + (5 if child else 1)
            if child is None and bank_job is None and now >= next_launch and hub.routes and not hub.restart:
                try:
                    with hub.lock:
                        routes = validate_routes(hub.routes, hub.ports)
                        if any(name.startswith(("StaveSynth:", "stave-v2-stage-", "stave-v2-audition-")) for name in hub.ports):
                            raise ValueError("Another Stave audio owner is present; not taking its devices")
                    bed_args = ["--bed-bank", str(Path(prepared.name) / "prepared.bank")] if prepared else []
                    take = recording_root / f"take-{uuid.uuid4().hex}.wav"
                    child = subprocess.Popen(["/usr/bin/pw-jack", str(args.binary.resolve()), str(args.soundfont.resolve()),
                        "512", "stave-v2-stage-candidate", routes["midi_source"], routes["audio_left"], routes["audio_right"],
                        "0", "--allow-stage-candidate", *bed_args, "--record-dir", str(recording_root), "--record-name", take.name], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        text=True, bufsize=1, start_new_session=True)
                    control = hub.attach(child, take)
                    workers = [threading.Thread(target=control.read, daemon=True), threading.Thread(target=control.write, daemon=True)]
                    for worker in workers: worker.start()
                except (OSError, ValueError) as error:
                    hub.message = str(error); next_launch = now + 2
            try:
                hub.reconcile()
            except ValueError as error:
                hub.detached(str(error)); stop_child(); next_launch = now + 10
                if args.pad_library and bank_job is None:
                    bank_job = bank_worker.submit(hub.prepare_current_bank)
            listeners.poll()
            if now >= next_notify:
                lifecycle.notify('WATCHDOG=1')
                next_notify = now + 5
        return 0
    finally:
        hub.detached("Candidate stopped.")
        lifecycle.notify('STOPPING=1')
        listeners.close()
        bank_worker.shutdown(wait=True)
        if bank_job is not None and bank_job.result() is not None:
            bank_job.result().cleanup()
        if hub.next_bank is not None:
            hub.next_bank.cleanup()
        stop_child()
        for s, previous in prior.items(): signal.signal(s, previous)
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
