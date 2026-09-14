"""Isolated lifecycle tests for the UI HTTP/WebSocket listeners.

All listeners use loopback ports reserved at runtime; configured stage ports are
never contacted.
"""

import asyncio
import contextlib
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock
from pathlib import Path

import stave_synth.websocket_server as ws_module


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _server_on_ephemeral_ports(stack):
    http_port = _free_port()
    websocket_port = _free_port()
    while websocket_port == http_port:
        websocket_port = _free_port()
    stack.enter_context(mock.patch.object(ws_module, "WEBSOCKET_HOST", "127.0.0.1"))
    stack.enter_context(mock.patch.object(ws_module, "HTTP_PORT", http_port))
    stack.enter_context(mock.patch.object(ws_module, "WEBSOCKET_PORT", websocket_port))
    stack.enter_context(mock.patch.object(ws_module, "INSTANCE_NAME", "lifecycle-test"))
    temp_data = Path(stack.enter_context(tempfile.TemporaryDirectory(
        prefix="stave-websocket-lifecycle-")))
    stack.enter_context(mock.patch.object(
        ws_module, "RECORDINGS_DIR", temp_data / "recordings"))
    return ws_module.WebSocketServer(), http_port, websocket_port


class _FakeSocket:
    remote_address = ("127.0.0.1", 12345)

    def __init__(self):
        self.messages = iter(("[]",))
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.messages)
        except StopIteration:
            raise StopAsyncIteration

    async def send(self, payload):
        self.sent.append(json.loads(payload))


class WebSocketLifecycleTests(unittest.TestCase):
    def test_successful_start_runtime_config_and_stop(self):
        with contextlib.ExitStack() as stack:
            server, http_port, websocket_port = _server_on_ephemeral_ports(stack)
            try:
                server.start(timeout=2.0)
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{http_port}/runtime-config.js",
                        timeout=2.0) as response:
                    body = response.read().decode("utf-8")
                    self.assertEqual(response.headers.get_content_type(), "application/javascript")
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                prefix = "window.STAVE_RUNTIME = "
                self.assertTrue(body.startswith(prefix) and body.endswith(";\n"))
                config = json.loads(body[len(prefix):-2])
                self.assertEqual(config, {
                    "websocket_port": websocket_port,
                    "instance": "lifecycle-test",
                })
            finally:
                server.stop()

            self.assertFalse(server._http_thread.is_alive())
            self.assertFalse(server._ws_thread.is_alive())
            # Both listener addresses must be reusable after stop.
            for port in (http_port, websocket_port):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind(("127.0.0.1", port))

    def test_http_bind_conflict_is_reported_before_ws_starts(self):
        with contextlib.ExitStack() as stack:
            server, http_port, _ = _server_on_ephemeral_ports(stack)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                occupied.bind(("127.0.0.1", http_port))
                occupied.listen()
                with self.assertRaises(OSError):
                    server.start(timeout=1.0)
            self.assertIsNone(server._ws_thread)
            server.stop()

    def test_ws_bind_conflict_rolls_back_http(self):
        with contextlib.ExitStack() as stack:
            server, http_port, websocket_port = _server_on_ephemeral_ports(stack)
            original_run_http = server._run_http
            http_thread_entered = threading.Event()

            def delayed_run_http():
                http_thread_entered.set()
                # Force the WS failure/stop request to happen before the HTTP
                # request loop starts. The event-driven loop must then exit
                # immediately instead of relying on serve_forever scheduling.
                time.sleep(0.2)
                original_run_http()

            server._run_http = delayed_run_http
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                occupied.bind(("127.0.0.1", websocket_port))
                occupied.listen()
                with self.assertRaisesRegex(RuntimeError, "WebSocket listener"):
                    server.start(timeout=2.0)

            self.assertTrue(http_thread_entered.is_set())
            self.assertFalse(server._http_thread.is_alive())
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", http_port))

    def test_stopped_server_rejects_restart(self):
        with contextlib.ExitStack() as stack:
            server, _, _ = _server_on_ephemeral_ports(stack)
            server.start(timeout=2.0)
            server.stop()
            with self.assertRaisesRegex(RuntimeError, "cannot be restarted"):
                server.start(timeout=2.0)

    def test_non_object_json_returns_error_without_dispatch(self):
        dispatched = []
        server = ws_module.WebSocketServer(message_handler=dispatched.append)
        fake = _FakeSocket()
        try:
            asyncio.run(server._handle_client(fake))
        finally:
            server.stop()

        self.assertEqual(dispatched, [])
        self.assertEqual(fake.sent, [{
            "type": "error",
            "for": None,
            "message": "message must be a JSON object",
        }])
        self.assertNotIn(fake, server.clients)


if __name__ == "__main__":
    unittest.main()
