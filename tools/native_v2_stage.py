#!/usr/bin/env python3
"""Explicit native512 candidate controller; never installs/stops a service.

Owns HTTP, saved knobs, exact device selection and bounded child recovery.
The child alone owns audio. Every new audio owner starts MUTED and receives
only saved absolute knobs: notes/freeze/master/bed triggers are never replayed.
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
    def __init__(self, store, routes):
        self.lock = threading.RLock()
        self.store = store
        self.route_file = store.path.with_name("native_routes.json")
        self.routes = validate_routes(routes)
        self.saved = {}
        self.save_message = "No native sound saved yet. Every restart starts muted."
        self.state_warning = None
        try:
            self.saved = store.load()
            if self.saved:
                self.save_message = "Saved native tone controls loaded. Master and performance actions are not restored."
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
        self.active = None
        self.epoch = uuid.uuid4().hex
        self.restoring = True
        self.restore_sequence = None
        self.restore_started = 0
        self.message = "Waiting for selected devices."
        self.restart = False
        self.ports = {}
        self.restarts = 0
        self.network = {}

    def attach(self, child):
        with self.lock:
            self.active = audition.Controller(child, instance="native-v2-stage")
            self.epoch = uuid.uuid4().hex
            self.restoring = True
            self.restore_sequence = None
            self.restore_started = time.monotonic()
            self.message = "Starting audio and restoring saved tone controls; output stays muted."
            self.restarts += 1
            return self.active

    def detached(self, message):
        with self.lock:
            self.active = None
            self.epoch = uuid.uuid4().hex
            self.restoring = True
            self.message = message

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
            if self.restore_sequence is None:
                last = 0
                for key, value in self.saved.items():
                    if key.startswith("bed_") and not data["status"].get("bed_mask"):
                        continue
                    last = self.active.submit({"key": key, "value": value})
                self.restore_sequence = last
            if data["error"]:
                raise ValueError("Saved controls were rejected: " + data["error"])
            if not self.restore_sequence or data["status"].get("applied", 0) >= self.restore_sequence:
                self.restoring = False
                self.message = "Audio ready. Raise Master deliberately; recovery never replays output gain."

    def snapshot(self):
        with self.lock:
            data = self.active.snapshot() if self.active else dict(instance="native-v2-stage", status={},
                values={k: v[3] for k, v in audition.CONTROLS.items()}, pending=0, error=None, exited=None, stale=True)
            midi = [name for name, p in self.ports.items() if p["type"] == "8 bit raw midi" and "output" in p["flags"] and not name.startswith(("StaveSynth:", "stave-v2-"))]
            audio = [name for name, p in self.ports.items() if p["type"] == "32 bit float mono audio" and "input" in p["flags"] and not name.startswith(("StaveSynth:", "stave-v2-"))]
            data.update(epoch=self.epoch, runtime=dict(restoring=self.restoring, message=self.message,
                persistence=True, save_message=self.save_message, state_warning=self.state_warning, can_restart=True,
                attempts=self.restarts, network=dict(self.network), devices={"midi": midi, "audio": audio}, **self.routes,
                scope="Native512 candidate: selected-device recovery restarts MUTED with saved tone controls. Browser disconnect does not stop audio. No legacy preset/recording parity or stage-release approval yet."))
            return data

    def dispatch(self, path, data, epoch):
        with self.lock:
            if epoch != self.epoch:
                raise ValueError("Audio session changed; refresh status. Old actions are never replayed.")
            if path == "/restart-audio":
                if data != {}:
                    raise ValueError("Empty restart request required")
                self.restart = True
                self.restoring = True
                self.epoch = uuid.uuid4().hex
                return {"message": "Restart requested; audio will return muted."}
            if path == "/routes":
                routes = validate_routes(data, self.ports)
                if self.route_file.is_symlink():
                    raise ValueError("Symlink route state refused")
                storage.atomic.atomic_write_json(self.route_file, {"schema": 1, "routes": routes}, max_bytes=4096)
                self.routes = routes
                self.restart = True
                self.restoring = True
                self.epoch = uuid.uuid4().hex
                return {"message": "Selected routes saved; audio restarting muted."}
            if not self.active or self.restoring:
                raise ValueError("Audio is not ready; no queued performance actions")
            if path == "/control":
                return {"queued": self.active.submit(data), "applied": False}
            if path != "/save" or data != {}:
                raise ValueError("Unknown or malformed native action")
            snapshot = self.active.snapshot()
            if (snapshot["stale"] or snapshot["exited"] is not None or snapshot["status"].get("fault", 1)
                    or not snapshot["status"].get("routed") or snapshot["pending"]):
                raise ValueError("Wait for healthy acknowledged controls before saving")
            values = {k: v for k, v in snapshot["values"].items() if k not in storage.TRANSIENT}
            if not snapshot["status"].get("bed_mask"):
                for key, value in self.saved.items():
                    if key.startswith("bed_"):
                        values[key] = value
            self.saved = self.store.save(values)
            self.state_warning = None
            self.save_message = "Native tone snapshot saved. Master, notes, freeze and bed actions excluded."
            return {"saved": True, "message": self.save_message}


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
    hub = CandidateHub(storage.ControlStore(state_root/"native_controls.json", audition.validate_control),
                       {k: getattr(args, k) for k in ("midi_source", "audio_left", "audio_right")})
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
        nonlocal child, workers
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=3)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=3)
            for worker in workers:
                worker.join(timeout=2)
            child.stdin.close(); child.stdout.close()
            child = None; workers = []
    try:
        # Audio does not depend on DHCP/Wi-Fi or a successful HTTP bind.
        # No wildcard listener: LAN addresses are explicitly inventoried.
        listeners.reconcile({str(address)})
        bed_args = []
        if args.pad_library is not None:
            prepared = tempfile.TemporaryDirectory(prefix="stave-native-bank-")
            assets = module("stave_candidate_assets", ROOT/"native_v2/bed_assets.py")
            bank = Path(prepared.name)/"prepared.bank"
            assets.prepare_bank(args.pad_library, bank)
            bed_args = ["--bed-bank", str(bank)]
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
                try:
                    with hub.lock:
                        routes = validate_routes(hub.routes, hub.ports)
                        if any(name.startswith(("StaveSynth:", "stave-v2-stage-", "stave-v2-audition-")) for name in hub.ports):
                            raise ValueError("Another Stave audio owner is present; not taking its devices")
                    child = subprocess.Popen(["/usr/bin/pw-jack", str(args.binary.resolve()), str(args.soundfont.resolve()),
                        "512", "stave-v2-stage-candidate", routes["midi_source"], routes["audio_left"], routes["audio_right"],
                        "0", "--allow-stage-candidate", *bed_args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        text=True, bufsize=1, start_new_session=True)
                    control = hub.attach(child)
                    workers = [threading.Thread(target=control.read, daemon=True), threading.Thread(target=control.write, daemon=True)]
                    for worker in workers: worker.start()
                except (OSError, ValueError) as error:
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
        if prepared is not None: prepared.cleanup()
        for s, previous in prior.items(): signal.signal(s, previous)
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
