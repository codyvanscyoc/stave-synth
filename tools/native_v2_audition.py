#!/usr/bin/env python3
"""Opt-in, temporary listening UI; never changes a service, route policy or state.

Starts only the explicit native audition executable (via existing pw-jack).
The native child owns exact JACK routes and audio. No import of Stave runtime.
Control on a trusted private LAN is open; no pairing or public exposure.
"""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import math
from pathlib import Path
import queue
import signal
import subprocess
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
}
INTEGRAL = {"wave1", "wave2", "shimmer", "reverb", "freeze", "release_all"}


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


class Controller:
    def __init__(self, child):
        self.child = child
        self.lock = threading.Lock()
        self.outgoing = queue.Queue(maxsize=64)
        self.sequence = 0
        self.pending = {}
        self.values = {k: v[3] for k, v in CONTROLS.items()}
        self.status = {}
        self.last_status = 0
        self.last_progress = 0
        self.error = None

    def submit(self, data):
        key, value = validate_control(data)
        with self.lock:
            if (self.child.poll() is not None or self.status.get("fault", 1) or
                    not self.status.get("routed") or time.monotonic() - self.last_status > 3 or
                    time.monotonic() - self.last_progress > 3):
                raise ValueError("Audio owner is not ready; no control replay")
            if len(self.pending) >= 64 or self.outgoing.full():
                raise ValueError("Control backlog full; wait for acknowledgment")
            self.sequence += 1
            self.pending[self.sequence] = (key, value)
            self.outgoing.put_nowait((self.sequence, key, value))
            return self.sequence

    def receive(self, message):
        with self.lock:
            if message.get("type") == "accepted":
                seq = message.get("id")
                if not message.get("ok"):
                    self.pending.pop(seq, None)
                    self.error = f"Native owner rejected control {seq}"
            elif message.get("type") == "status" and message.get("instance") == "native-v2-audition":
                if message.get("blocks", 0) > self.status.get("blocks", 0):
                    self.last_progress = time.monotonic()
                self.status = message
                self.last_status = time.monotonic()
                for seq in sorted(list(self.pending)):
                    if seq <= message.get("applied", 0):
                        key, value = self.pending.pop(seq)
                        self.values[key] = value

    def snapshot(self):
        with self.lock:
            return dict(instance="native-v2-audition", status=self.status.copy(), values=self.values.copy(),
                        pending=len(self.pending), error=self.error, exited=self.child.poll(),
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
                    seq, key, value = self.outgoing.get(timeout=.2)
                except queue.Empty:
                    continue
                self.child.stdin.write(f"{seq} {key} {value}\n")
                self.child.stdin.flush()
        except (OSError, ValueError) as error:
            with self.lock:
                self.error = str(error)


def handler_for(controller, authority):
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
            if self.headers.get("Host") != authority:
                return self.reply(403, {"error": "Use the displayed private-IP URL"})
            if self.path == "/":
                return self.reply(200, (ROOT / "native_v2/audition.html").read_bytes(), "text/html; charset=utf-8")
            if self.path == "/status":
                return self.reply(200, controller.snapshot())
            if self.path == "/controls":
                return self.reply(200, CONTROLS)
            self.reply(404, {"error": "Unknown audition endpoint"})

        def do_POST(self):
            if (self.path != "/control" or self.headers.get("Host") != authority or
                    self.headers.get("Origin") != "http://" + authority or
                    self.headers.get("Content-Type") != "application/json" or self.headers.get("Transfer-Encoding")):
                return self.reply(403, {"error": "Same-origin JSON control required"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 512:
                    raise ValueError("Bounded request body required")
                seq = controller.submit(json.loads(self.rfile.read(length)))
                self.reply(202, {"queued": seq, "applied": False})
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
    args = parser.parse_args()
    address = ipaddress.ip_address(args.listen)
    if (not args.allow_live_audition or address.version != 4 or not address.is_private or address.is_unspecified or
            address.is_multicast or not 8082 <= args.port <= 8090 or not 10 <= args.seconds <= 3600 or
            not args.binary.is_file() or not args.soundfont.is_file()):
        parser.error("Explicit live opt-in, private IPv4,8082..8090,10..3600 seconds and existing binary/SF2 required")
    authority = f"{address}:{args.port}"
    # Bind first. A port conflict must not leave an orphan audio process.
    server = HTTPServer((str(address), args.port), BaseHTTPRequestHandler)
    server.timeout = .2
    child = None
    threads = []
    stopped = False
    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True
    prior = {s: signal.signal(s, stop) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        child = subprocess.Popen(["/usr/bin/pw-jack", str(args.binary.resolve()), str(args.soundfont.resolve()),
                                  str(args.frames), "stave-v2-audition-listen", args.midi_source, args.audio_left,
                                  args.audio_right, str(args.seconds), "--allow-live-audition"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
        controller = Controller(child)
        threads = [threading.Thread(target=controller.read, daemon=True), threading.Thread(target=controller.write, daemon=True)]
        for thread in threads:
            thread.start()
        server.RequestHandlerClass = handler_for(controller, authority)
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
        for s, previous in prior.items():
            signal.signal(s, previous)


if __name__ == "__main__":
    raise SystemExit(main())
