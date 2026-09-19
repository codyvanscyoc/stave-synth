#!/usr/bin/env python3
"""Opt-in, temporary listening UI; never changes a service, route policy or state.

Starts only the explicit native audition executable (via existing pw-jack).
The native child owns exact JACK routes and audio. No import of Stave runtime.
Control on a trusted private LAN is open; no pairing or public exposure.
"""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import importlib.util
import json
import math
from pathlib import Path
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
# (minimum, maximum, step, initial acknowledged value). Must match native
# audition_session.hpp, not the production preset schema.
CONTROLS = {
    "piano": (0, 1, .01, .5), "osc1": (0, 1, .01, 0), "osc2": (0, 1, .01, 0),
    "cutoff": (20, 20000, 1, 8000), "wet": (0, 1, .01, .75), "master": (0, 1, .01, 0),
    "wave1": (0, 4, 1, 0), "wave2": (0, 4, 1, 1), "attack": (0, 10000, 10, 200),
    "release": (0, 30000, 10, 500), "resonance": (.5, 10, .01, .707),
    "piano_room": (0, 1, .01, .4), "piano_reverb": (0, 1, .01, 0),
    "delay_wet": (0, 1, .01, 0), "delay_feedback": (0, .99, .01, .35),
    "shimmer": (0, 1, 1, 0), "shimmer_mix": (0, 1, .01, .5),
    "reverb": (0, 6, 1, 0), "freeze": (0, 1, 1, 0), "release_all": (0, 1, 1, 0),
    "piano_tone": (0, 1, .01, 1),
    "bed_level": (0, 1, .01, 1), "bed_key": (0, 11, 1, -1),
    "bed_rise": (0, 60, .5, 0), "bed_rise_cutoff": (200, 20000, 1, 3000),
    "bed_mellow": (0, 1, 1, 0), "bed_mellow_cutoff": (100, 8000, 1, 400),
    "bed_fade": (0, 1, 1, 0), "bed_release": (0, 1, 1, 0),
    "attack1": (0, 10000, 1, 200), "decay1": (0, 20000, 1, 1500),
    "sustain1": (0, 100, .1, 80), "release1": (0, 30000, 1, 500),
    "attack2": (0, 10000, 1, 200), "decay2": (0, 20000, 1, 1500),
    "sustain2": (0, 100, .1, 80), "release2": (0, 30000, 1, 500),
    "envelope_link": (0, 1, 1, 0), "volume_link": (0, 1, 1, 0),
    "master_low": (-6, 6, .1, 0), "master_mid": (-6, 6, .1, 0), "master_high": (-6, 6, .1, 0),
    "master_lowcut": (0, 1, 1, 0), "master_lowcut_hz": (20, 200, 1, 80),
    "piano_room_size": (0, 1, .01, .5), "piano_room_damp": (0, .99, .01, .6),
    "reverb_decay": (0, 30, .1, 6), "reverb_predelay": (0, 150, 1, 25),
    "reverb_lowcut": (20, 20000, 1, 80), "reverb_highcut": (20, 20000, 1, 7000), "reverb_damp": (0, .99, .01, .5),
    "delay_time": (1, 1000, 1, 500), "delay_lowcut": (20, 1000, 1, 20), "delay_highcut": (500, 20000, 1, 18000),
    "record_start": (0, 1, 1, 0), "record_stop": (0, 1, 1, 0),
    "transpose": (-24, 24, 1, 0), "piano_octave": (-3, 3, 1, 0),
    "octave1": (-3, 3, 1, 0), "octave2": (-3, 3, 1, 0),
    "pan1": (-1, 1, .01, 0), "pan2": (-1, 1, .01, 0),
    "detune": (0, 1, .001, .07), "spread": (0, 1, .001, .85),
    "piano_lowcut": (20, 500, 1, 20), "piano_velocity": (1, 4, .01, 1),
    "osc1_reverb": (0, 1, .01, 1), "osc2_reverb": (0, 1, .01, 1),
    "piano_delay": (0, 1, .01, 0), "filter_slope": (0, 1, 1, 0),
    "piano_filter": (0, 1, 1, 0), "bpm": (40, 240, 1, 120),
    "delay_division": (0, 8, 1, 3),
}
INTEGRAL = {"wave1", "wave2", "shimmer", "reverb", "freeze", "release_all", "bed_key", "bed_mellow", "bed_fade", "bed_release", "envelope_link", "volume_link", "master_lowcut", "record_start", "record_stop", "transpose", "piano_octave", "octave1", "octave2", "filter_slope", "piano_filter", "delay_division"}
MIDI_RESERVED = frozenset((64, 66, 120, 123))
MIDI_UNMAPPABLE = frozenset(("release_all", "bed_key", "bed_fade", "bed_release", "record_start", "record_stop",
                             "reverb_lowcut", "reverb_highcut", "delay_lowcut", "delay_highcut",
                             "piano_lowcut", "piano_velocity", "filter_slope", "piano_filter"))
MIDI_MAPPABLE = frozenset(CONTROLS) - MIDI_UNMAPPABLE


def midi_value(key, raw):
    if key not in MIDI_MAPPABLE or type(raw) is not int or not 0 <= raw <= 127:
        raise ValueError("Invalid MIDI mapping value")
    minimum, maximum, step, _ = CONTROLS[key]
    value = minimum + (maximum - minimum) * raw / 127
    value = min(maximum, max(minimum, minimum + math.floor((value - minimum) / step + .5) * step))
    return int(value) if key in INTEGRAL else round(value, 8)


def validate_midi_mapping(data):
    if (not isinstance(data, dict) or set(data) != {"key", "cc"} or data["key"] not in MIDI_MAPPABLE or
            type(data["cc"]) is not int or not 0 <= data["cc"] < 128 or data["cc"] in MIDI_RESERVED):
        raise ValueError("Choose a mappable control and a non-reserved MIDI CC")
    return data["key"], data["cc"]


def apply_value(values, key, value, volume_offset=None):
    """Mirror one acknowledged native transaction (also used without devices)."""
    if key in ("osc1", "osc2") and values.get("volume_link", 0) and volume_offset is None:
        volume_offset = values.get("osc1", 0) - values.get("osc2", 0)
    values[key] = value
    if key == "reverb":
        preset = ((6, 25, 80, 7000, .5), (9, 45, 120, 8500, .35), (1.5, 8, 200, 10000, .7),
                  (3, 5, 150, 11000, .3), (7, 30, 150, 7000, .55), (10, 15, 50, 4000, .3), (8, 20, 100, 6500, .5))[int(value)]
        for name, setting in zip(("reverb_decay", "reverb_predelay", "reverb_lowcut", "reverb_highcut", "reverb_damp"), preset):
            values[name] = setting
    if key == "volume_link":
        return values.get("osc1", 0) - values.get("osc2", 0) if value else None
    if key in ("osc1", "osc2") and values.get("volume_link", 0):
        other = "osc2" if key == "osc1" else "osc1"
        linked = value - volume_offset if key == "osc1" else value + volume_offset
        values[other] = min(1, max(0, linked))
    if key in ("attack", "release"):
        values[key + "1"] = values[key + "2"] = value
    elif key == "delay_time":
        values["delay_division"] = 0
    elif key in {p + n for p in ("attack", "decay", "sustain", "release") for n in ("1", "2")}:
        if values.get("envelope_link", 0):
            values[key[:-1] + ("2" if key[-1] == "1" else "1")] = value
    return volume_offset


def restore_items(values):
    # A newly attached native owner starts unlinked. Legacy aliases first,
    # explicit independent values next, LINK last: unequal linked patches
    # remain unequal until the player's next linked edit.
    values = dict(values)
    result = []
    # Cross linked cutoff pairs through their safe minimum so either endpoint
    # can move past the old range without an invalid intermediate state.
    for low in ("reverb_lowcut", "delay_lowcut"):
        if low in values:
            result.append((low, CONTROLS[low][0]))
    result.extend(sorted(((k, v) for k, v in values.items() if k not in
                          ("reverb_lowcut", "delay_lowcut", "envelope_link", "volume_link", "master")),
                         key=lambda item: (0 if item[0] in ("attack", "release", "reverb") else
                                           1 if item[0] == "delay_time" else
                                           3 if item[0] == "delay_division" else 2)))
    for low in ("reverb_lowcut", "delay_lowcut"):
        if low in values:
            result.append((low, values[low]))
    if "envelope_link" in values:
        result.append(("envelope_link", values["envelope_link"]))
    if "volume_link" in values:
        result.append(("volume_link", values["volume_link"]))
    if "master" in values:
        result.append(("master", values["master"]))
    return result


def validate_control(data):
    if not isinstance(data, dict) or set(data) != {"key", "value"}:
        raise ValueError("Expected one absolute key/value control")
    key, value = data["key"], data["value"]
    if not isinstance(key, str) or key not in CONTROLS or type(value) not in (int, float):
        raise ValueError("Unknown control or nonnumeric value")
    low, high, _, _ = CONTROLS[key]
    if not low <= value <= high or not math.isfinite(value) or (key in INTEGRAL and value != int(value)):
        raise ValueError("Control outside audition range")
    return key, value


def validate_control_pair(values, key, value):
    """Reject crossed tone filters before they can reach the audio owner."""
    pairs = {"reverb_lowcut": "reverb_highcut", "delay_lowcut": "delay_highcut"}
    reverse = {high: low for low, high in pairs.items()}
    if key in pairs and value >= values.get(pairs[key], CONTROLS[pairs[key]][3]):
        raise ValueError("Low cut must remain below high cut")
    if key in reverse and value <= values.get(reverse[key], CONTROLS[reverse[key]][3]):
        raise ValueError("High cut must remain above low cut")


class Controller:
    def __init__(self, child, instance="native-v2-audition"):
        self.child = child
        self.instance = instance
        self.lock = threading.Lock()
        self.outgoing = queue.Queue(maxsize=64)
        self.sequence = 0
        self.pending = {}
        self.map_pending = {}
        self.mapped_serials = [0] * len(CONTROLS)
        self.values = {k: v[3] for k, v in CONTROLS.items()}
        self.status = {}
        self.last_status = 0
        self.last_progress = 0
        self.error = None
        self.volume_link_offset = None

    def submit(self, data):
        key, value = validate_control(data)
        with self.lock:
            if (self.child.poll() is not None or self.status.get("fault", 1) or
                    not self.status.get("routed") or time.monotonic() - self.last_status > 3 or
                    time.monotonic() - self.last_progress > 3):
                raise ValueError("Audio owner is not ready; no control replay")
            if len(self.pending) >= 64 or self.outgoing.full():
                raise ValueError("Control backlog full; wait for acknowledgment")
            effective = self.values.copy()
            effective_offset = self.volume_link_offset
            for pending_key, pending_value in self.pending.values():
                effective_offset = apply_value(effective, pending_key, pending_value, effective_offset)
            validate_control_pair(effective, key, value)
            mask = self.status.get("bed_mask", 0)
            if key.startswith("bed_") and (not mask or (key == "bed_key" and not mask & (1 << int(value)))):
                raise ValueError("No recording loaded for this pad/key")
            self.sequence += 1
            self.pending[self.sequence] = (key, value)
            self.outgoing.put_nowait((self.sequence, key, value))
            return self.sequence

    def map_cc(self, data):
        key, cc = validate_midi_mapping(data)
        with self.lock:
            if (self.child.poll() is not None or self.status.get("fault", 1) or not self.status.get("routed") or
                    time.monotonic() - self.last_status > 3 or time.monotonic() - self.last_progress > 3):
                raise ValueError("Audio owner is not ready for MIDI mapping")
            if len(self.pending) + len(self.map_pending) >= 64 or self.outgoing.full():
                raise ValueError("Control backlog full; wait for acknowledgment")
            self.sequence += 1
            self.map_pending[self.sequence] = (key, cc)
            self.outgoing.put_nowait(("map", self.sequence, cc, key))
            return self.sequence

    def receive(self, message):
        with self.lock:
            if message.get("type") == "accepted":
                seq = message.get("id")
                if not message.get("ok"):
                    self.pending.pop(seq, None)
                    self.error = f"Native owner rejected control {seq}"
            elif message.get("type") == "mapped":
                seq = message.get("id")
                self.map_pending.pop(seq, None)
                if not message.get("ok"):
                    self.error = f"Native owner rejected MIDI mapping {seq}"
            elif message.get("type") == "status" and message.get("instance") == self.instance:
                if message.get("blocks", 0) > self.status.get("blocks", 0):
                    self.last_progress = time.monotonic()
                self.status = message
                self.last_status = time.monotonic()
                for seq in sorted(list(self.pending)):
                    if seq <= message.get("applied", 0):
                        key, value = self.pending.pop(seq)
                        self.volume_link_offset = apply_value(
                            self.values, key, value, self.volume_link_offset)
                if "bed_key" in message:
                    self.values["bed_key"] = message["bed_key"]
                raw, serials = message.get("midi_mapped_raw"), message.get("midi_mapped_serial")
                if (isinstance(raw, list) and isinstance(serials, list) and
                        len(raw) == len(CONTROLS) and len(serials) == len(CONTROLS)):
                    for index, (key, value, serial) in enumerate(zip(CONTROLS, raw, serials)):
                        if (type(value) is int and value >= 0 and type(serial) is int and
                                serial > self.mapped_serials[index] and key in MIDI_MAPPABLE):
                            self.volume_link_offset = apply_value(
                                self.values, key, midi_value(key, value), self.volume_link_offset)
                            self.mapped_serials[index] = serial

    def snapshot(self):
        with self.lock:
            return dict(instance=self.instance, status=self.status.copy(), values=self.values.copy(),
                        pending=len(self.pending) + len(self.map_pending), map_pending=len(self.map_pending), error=self.error, exited=self.child.poll(),
                        stale=time.monotonic() - self.last_status > 3 or time.monotonic() - self.last_progress > 3)

    def read(self):
        try:
            while True:
                line = self.child.stdout.readline(4097)
                if not line:
                    break
                if len(line) > 4096:
                    raise ValueError("Oversized native telemetry")
                self.receive(json.loads(line))
        except (OSError, ValueError, TypeError) as error:
            with self.lock:
                self.error = str(error)

    def write(self):
        try:
            while self.child.poll() is None:
                try:
                    item = self.outgoing.get(timeout=.2)
                except queue.Empty:
                    continue
                if item[0] == "map":
                    _, seq, cc, key = item
                    self.child.stdin.write(f"map {seq} {cc} {key}\n")
                else:
                    seq, key, value = item
                    self.child.stdin.write(f"{seq} {key} {value}\n")
                self.child.stdin.flush()
        except (OSError, ValueError) as error:
            with self.lock:
                self.error = str(error)


def authority_set(address, port, hostnames=()):
    result = {f"{ipaddress.ip_address(address)}:{port}"}
    for hostname in hostnames:
        name = hostname.lower()
        if (len(name) > 253 or not name.endswith(".local") or
                any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in name.split("."))):
            raise ValueError("Explicit valid .local hostname required")
        result.add(f"{name}:{port}")
    return frozenset(result)


def handler_for(controller, authority):
    authorities = frozenset((authority,)) if isinstance(authority, str) else frozenset(authority)
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(1)

        def log_message(self, *_):
            pass

        def reply(self, code, data, content_type="application/json"):
            body = data if isinstance(data, bytes) else json.dumps(data, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("Host", "").lower() not in authorities:
                return self.reply(403, {"error": "Use an explicitly configured Stave address"})
            if self.path == "/":
                return self.reply(200, (ROOT / "native_v2/audition.html").read_bytes(), "text/html; charset=utf-8")
            if self.path == "/logo.png":
                return self.reply(200, (ROOT / "ui/logo.png").read_bytes(), "image/png")
            if self.path == "/status":
                return self.reply(200, controller.snapshot())
            if self.path == "/controls":
                return self.reply(200, CONTROLS)
            self.reply(404, {"error": "Unknown audition endpoint"})

        def do_POST(self):
            host = self.headers.get("Host", "").lower()
            if (self.path not in ("/control", "/save", "/restart-audio", "/routes", "/record-assign", "/preset-save", "/preset-load", "/midi-map") or host not in authorities or
                    self.headers.get("Origin", "").lower() != "http://" + host or
                    self.headers.get("Content-Type") != "application/json" or self.headers.get("Transfer-Encoding")):
                return self.reply(403, {"error": "Same-origin JSON control required"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= (1024 if self.path == "/routes" else 512):
                    raise ValueError("Bounded request body required")
                data = json.loads(self.rfile.read(length))
                if hasattr(controller, "dispatch"):
                    self.reply(202, controller.dispatch(self.path, data, self.headers.get("X-Stave-Epoch")))
                elif self.path == "/control":
                    seq = controller.submit(data)
                    self.reply(202, {"queued": seq, "applied": False})
                else:
                    raise ValueError("Not available in this audition")
            except (ValueError, TypeError, OSError) as error:
                self.reply(400, {"error": str(error)})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--soundfont", type=Path, required=True)
    parser.add_argument("--listen", required=True, help="Explicit private IPv4; no wildcard/public binding")
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--midi-source", required=True)
    parser.add_argument("--audio-left", required=True)
    parser.add_argument("--audio-right", required=True)
    parser.add_argument("--frames", type=int, choices=(512, 256), default=512)
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument("--allow-live-audition", action="store_true")
    parser.add_argument("--pad-library", type=Path, help="Explicit WAV library; prepared privately before audio starts, never modified")
    parser.add_argument("--hostname", action="append", default=[], help="Additional explicit .local hostname for same-origin control")
    args = parser.parse_args()
    address = ipaddress.ip_address(args.listen)
    if (not args.allow_live_audition or address.version != 4 or not address.is_private or address.is_unspecified or
            address.is_multicast or not 8082 <= args.port <= 8090 or not 10 <= args.seconds <= 3600 or
            not args.binary.is_file() or not args.soundfont.is_file()):
        parser.error("Explicit live opt-in, private IPv4,8082..8090,10..3600 seconds and existing binary/SF2 required")
    authority = f"{address}:{args.port}"
    try:
        authorities = authority_set(str(address), args.port, args.hostname)
    except ValueError as error:
        parser.error(str(error))
    # Bind first. A port conflict must not leave an orphan audio process.
    server = HTTPServer((str(address), args.port), BaseHTTPRequestHandler)
    server.timeout = .2
    child = None
    prepared = None
    threads = []
    stopped = False
    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True
    prior = {s: signal.signal(s, stop) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        bed_args = []
        if args.pad_library is not None:
            prepared = tempfile.TemporaryDirectory(prefix="stave-v2-bed-")
            spec = importlib.util.spec_from_file_location("stave_native_bed_assets", ROOT / "native_v2/bed_assets.py")
            assets = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(assets)
            bank = Path(prepared.name) / "prepared.bank"
            result = assets.prepare_bank(args.pad_library, bank)
            print(f"Prepared recorded pads: {len(result['slots'])}/12 keys, {result['pcm_bytes']} PCM bytes", flush=True)
            bed_args = ["--bed-bank", str(bank)]
        child = subprocess.Popen(["/usr/bin/pw-jack", str(args.binary.resolve()), str(args.soundfont.resolve()),
                                  str(args.frames), "stave-v2-audition-listen", args.midi_source, args.audio_left,
                                  args.audio_right, str(args.seconds), "--allow-live-audition", *bed_args],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
        controller = Controller(child)
        threads = [threading.Thread(target=controller.read, daemon=True), threading.Thread(target=controller.write, daemon=True)]
        for thread in threads:
            thread.start()
        server.RequestHandlerClass = handler_for(controller, authorities)
        print(f"ISOLATED AUDITION ONLY: http://{authority} — starts MUTED; not the normal Stave UI", flush=True)
        while not stopped and child.poll() is None:
            server.handle_request()
        return 0 if stopped else child.returncode
    finally:
        server.server_close()
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            for thread in threads:
                thread.join(timeout=2)
            child.stdin.close()
            child.stdout.close()
        if prepared is not None:
            prepared.cleanup()
        for s, previous in prior.items():
            signal.signal(s, previous)


if __name__ == "__main__":
    raise SystemExit(main())
