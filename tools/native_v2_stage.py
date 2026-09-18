#!/usr/bin/env python3
"""Explicit native512 candidate controller; never installs/stops a service.

Owns HTTP, saved knobs, exact device selection and bounded child recovery.
The child alone owns audio. Every new audio owner starts MUTED and receives
only saved absolute controls: notes/freeze/bed triggers are never replayed;
an explicitly saved Master target restores last, after healthy routing and tone.
Not legacy feature parity or completed stage qualification.
"""
import argparse
import fcntl
import importlib.util
import ipaddress
import json
import os
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
            port = ports.get(value, {})
            kind, direction = ("8 bit raw midi", "output") if key == "midi_source" else ("32 bit float mono audio", "input")
            if port.get("type") != kind or direction not in port.get("flags", ()):
                raise ValueError("Selected device port missing or wrong type/direction")
    return dict(routes)


class CandidateHub:
    def __init__(self, store, routes, recording_dir=None, pad_library=None):
        self.lock = threading.RLock()
        self.store = store
        self.route_file = store.path.with_name("native_routes.json")
        self.routes = validate_routes(routes)
        self.saved = {}
        self.presets = None
        self.midi_map = None
        self.save_message = "No native sound saved yet. Master stays muted until you save a sound with a deliberate level."
        self.state_warning = None
        try:
            self.saved = store.load()
            if self.saved:
                self.save_message = "Saved native sound loaded. Master restores last after healthy routing."
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
            self.state_warning = "Saved-state warning: " + str(error)
            self.save_message = self.state_warning + "; file preserved. Save only when the new sound is intentional."
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
        self.epoch = uuid.uuid4().hex
        self.restoring = True
        self.restore_sequence = None
        self.map_restore_queued = False
        self.map_restored = False
        self.restore_started = 0
        self.message = "Waiting for selected devices."
        self.restart = False
        self.ports = {}
        self.restarts = 0
        self.network = {}
        self.recording_dir = Path(recording_dir) if recording_dir else None
        self.pad_library = Path(pad_library) if pad_library else None
        self.recording_take = None
        if self.recording_dir is not None:
            completed = [p for p in self.recording_dir.glob("take-*.wav") if p.is_file() and not p.is_symlink()]
            if completed:
                self.recording_take = max(completed, key=lambda p: p.stat().st_mtime_ns)

    def attach(self, child, recording_take=None):
        with self.lock:
            self.active = audition.Controller(child, instance="native-v2-stage")
            if recording_take is not None:
                self.recording_take = Path(recording_take)
            self.epoch = uuid.uuid4().hex
            self.restoring = True
            self.restore_sequence = None
            self.map_restore_queued = False
            self.map_restored = False
            self.restore_started = time.monotonic()
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
            if time.monotonic() - self.restore_started > 10:
                raise ValueError("Audio startup/saved-control acknowledgment timed out")
            data = self.active.snapshot()
            if data["stale"] or data["status"].get("fault", 1) or not data["status"].get("routed"):
                return
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
            if self.restore_sequence is None:
                last = 0
                for key, value in audition.restore_items(self.prepared):
                    if key.startswith("bed_") and not data["status"].get("bed_mask"):
                        continue
                    last = self.active.submit({"key": key, "value": value})
                self.restore_sequence = last
            if data["error"]:
                raise ValueError("Saved controls were rejected: " + data["error"])
            if not self.restore_sequence or data["status"].get("applied", 0) >= self.restore_sequence:
                self.restoring = False
                self.message = "Audio ready. Saved Master was restored last through the output ramp."

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
                persistence=True, save_message=self.save_message, state_warning=self.state_warning, can_restart=True,
                attempts=self.restarts, network=dict(self.network), devices={"midi": midi, "audio": audio},
                recording={"take": self.recording_take.name if self.recording_take else None,
                           "capture": data['status'].get('capture_end', 0),
                           "writer": data['status'].get('writer_state', 0),
                           "frames": data['status'].get('writer_frames', 0)},
                presets=self.presets.summary() if self.presets else [None] * 5, **self.routes,
                midi_maps=dict(self.midi_map.mappings) if self.midi_map else {},
                scope="Native512 stage engine: selected-device recovery restores saved controls with Master last. Browser disconnect does not stop audio."))
            return data

    def dispatch(self, path, data, epoch):
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
                return {"message": "Restart requested; audio will return muted."}
            if path == "/routes":
                routes = validate_routes(data, self.ports)
                if self.route_file.is_symlink():
                    raise ValueError("Symlink route state refused")
                storage.atomic.atomic_write_json(self.route_file, {"schema": 1, "routes": routes}, max_bytes=4096)
                self.remember_tone()
                self.routes = routes
                self.restart = True
                self.restoring = True
                self.epoch = uuid.uuid4().hex
                return {"message": "Selected routes saved; audio restarting muted."}
            if path == "/record-assign":
                if (not isinstance(data, dict) or set(data) != {"slot"} or type(data["slot"]) is not int
                        or not 0 <= data["slot"] < 12):
                    raise ValueError("Choose one pad key slot")
                if not self.active or self.restoring:
                    raise ValueError("Audio must be ready before assigning a take")
                status = self.active.snapshot()["status"]
                if status.get("writer_state") != 2 or status.get("capture_end") != 2:
                    raise ValueError("Stop the recording and wait for Ready before assigning it")
                if self.recording_take is None or self.pad_library is None:
                    raise ValueError("Recording library is not configured")
                names = ("C", "Cs", "D", "Ds", "E", "F", "Fs", "G", "Gs", "A", "As", "B")
                self._install_take(self.recording_take, self.pad_library / f"pad_{names[data['slot']]}.wav")
                self.remember_tone()
                self.restart = True
                self.restoring = True
                self.epoch = uuid.uuid4().hex
                return {"message": f"Saved to {names[data['slot']]}; audio restarting muted with the new pad."}
            if path == "/preset-load":
                if not isinstance(data, dict) or set(data) != {"slot"} or not self.presets:
                    raise ValueError("Choose one native preset")
                values = self.presets.controls_for(data["slot"])
                if self.active is None and not self.restart:
                    self.prepared = values
                    return {"message": "Preset prepared; it will sound when devices connect."}
                if not self.active or self.restoring:
                    raise ValueError("Audio is not ready for a preset change")
                status = self.active.snapshot()["status"]
                for key, value in audition.restore_items(values):
                    if key.startswith("bed_") and not status.get("bed_mask"):
                        self.prepared[key] = value
                    else:
                        self.active.submit({"key": key, "value": value})
                return {"message": "Preset queued with Master last."}
            if path == "/preset-save":
                if not isinstance(data, dict) or set(data) != {"slot", "name"} or not self.presets:
                    raise ValueError("Choose one native preset slot and name")
                if self.active is not None:
                    snapshot = self.active.snapshot()
                    if self.restoring or snapshot["stale"] or snapshot["pending"] or snapshot["status"].get("fault", 1):
                        raise ValueError("Wait for healthy acknowledged controls before storing a preset")
                    values = {k: v for k, v in snapshot["values"].items() if k not in storage.TRANSIENT}
                elif not self.restart:
                    values = self.prepared
                else:
                    raise ValueError("Wait for audio recovery before storing a preset")
                summary = self.presets.save_slot(data["slot"], data["name"], values)
                return {"presets": summary, "message": "Preset stored."}
            if path == "/midi-map":
                key, cc = audition.validate_midi_mapping(data)
                if not self.midi_map:
                    raise ValueError("MIDI-map persistence is unavailable")
                if not self.active or self.restoring:
                    raise ValueError("Connect MIDI and wait for audio before learning a control")
                sequence = self.active.map_cc({"key": key, "cc": cc})
                mappings = self.midi_map.assign(key, cc)
                return {"queued": sequence, "midi_maps": mappings, "message": f"Mapped CC {cc} to {key}."}
            if self.active is None and not self.restart:
                if path == '/control':
                    key, value = audition.validate_control(data)
                    if key in storage.TRANSIENT:
                        raise ValueError('Output and performance actions require audio; tone controls can be prepared now')
                    audition.validate_control_pair(self.prepared, key, value)
                    audition.apply_value(self.prepared, key, value)
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
                return {"queued": self.active.submit(data), "applied": False}
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
            self.state_warning = None
            self.save_message = "Native sound saved. Master restores last; notes, freeze and bed actions remain excluded."
            return {"saved": True, "message": self.save_message}

    @staticmethod
    def _install_take(source, target):
        source, target = Path(source), Path(target)
        if source.is_symlink() or target.is_symlink():
            raise ValueError("Symlink recording destinations are refused")
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        backup = target.with_name(f"undo-{target.stem}-{uuid.uuid4().hex}.wav")
        out = -1
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 58 <= info.st_size <= 64 * 1024 * 1024:
                raise ValueError("Invalid or oversized finalized recording")
            out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            remaining = info.st_size
            while remaining:
                block = os.read(fd, min(1024 * 1024, remaining))
                if not block:
                    raise OSError("Short recording read")
                view = memoryview(block)
                while view:
                    written = os.write(out, view)
                    if written <= 0:
                        raise OSError("Short recording write")
                    view = view[written:]
                remaining -= len(block)
            os.fsync(out); os.close(out); out = -1
            if target.exists():
                os.link(target, backup, follow_symlinks=False)
            os.replace(temp, target)
            directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                try: os.fsync(directory)
                except OSError: pass  # file payload was fsynced before atomic replacement
            finally: os.close(directory)
        finally:
            os.close(fd)
            if out >= 0: os.close(out)
            try: temp.unlink()
            except FileNotFoundError: pass


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
    for name in ("listen", "midi-source", "audio-left", "audio-right"):
        parser.add_argument("--"+name, required=True)
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--hostname", action="append", default=[])
    parser.add_argument("--network-interface", action="append", default=[],
                        help="With --listen auto, bind RFC1918 addresses on these interfaces plus loopback")
    parser.add_argument("--pad-library", type=Path)
    parser.add_argument("--allow-stage-candidate", action="store_true")
    args = parser.parse_args()
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
                       {k: getattr(args, k) for k in ("midi_source", "audio_left", "audio_right")},
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
            if now >= next_inventory:
                try:
                    result = subprocess.run(["/usr/bin/pw-jack", "jack_lsp", "-t", "-p"], capture_output=True, text=True, timeout=2)
                    hub.inventory(parse_ports(result.stdout) if result.returncode == 0 else {})
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    hub.inventory({})
                next_inventory = now + (5 if child else 1)
            if child is None and now >= next_launch and not hub.restart:
                launching_bank = None
                try:
                    with hub.lock:
                        routes = validate_routes(hub.routes, hub.ports)
                        if any(name.startswith(("StaveSynth:", "stave-v2-stage-", "stave-v2-audition-")) for name in hub.ports):
                            raise ValueError("Another Stave audio owner is present; not taking its devices")
                    bed_args = []
                    if args.pad_library is not None:
                        launching_bank = tempfile.TemporaryDirectory(prefix="stave-native-bank-")
                        assets = module("stave_candidate_assets", ROOT/"native_v2/bed_assets.py")
                        bank = Path(launching_bank.name)/"prepared.bank"
                        assets.prepare_bank(args.pad_library, bank)
                        bed_args = ["--bed-bank", str(bank)]
                    take = recording_root / f"take-{uuid.uuid4().hex}.wav"
                    child = subprocess.Popen(["/usr/bin/pw-jack", str(args.binary.resolve()), str(args.soundfont.resolve()),
                        "512", "stave-v2-stage-candidate", routes["midi_source"], routes["audio_left"], routes["audio_right"],
                        "0", "--allow-stage-candidate", *bed_args, "--record-dir", str(recording_root), "--record-name", take.name], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        text=True, bufsize=1, start_new_session=True)
                    prepared = launching_bank; launching_bank = None
                    control = hub.attach(child, take)
                    workers = [threading.Thread(target=control.read, daemon=True), threading.Thread(target=control.write, daemon=True)]
                    for worker in workers: worker.start()
                except (OSError, ValueError) as error:
                    if launching_bank is not None: launching_bank.cleanup()
                    hub.message = str(error); next_launch = now + 2
            try:
                hub.reconcile()
            except ValueError as error:
                hub.detached(str(error)); stop_child(); next_launch = now + 10
            listeners.poll()
            if now >= next_notify:
                lifecycle.notify('WATCHDOG=1')
                next_notify = now + 5
        return 0
    finally:
        hub.detached("Candidate stopped.")
        lifecycle.notify('STOPPING=1')
        listeners.close()
        stop_child()
        for s, previous in prior.items(): signal.signal(s, previous)
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
