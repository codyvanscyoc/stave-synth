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

    async def close(self, **_kwargs):
        pass


class _OutputSocket:
    remote_address = ("127.0.0.1", 12345)

    def __init__(self, blocked=False):
        self.sent = []
        self.closed = False
        self._gate = asyncio.Event()
        if not blocked:
            self._gate.set()

    async def send(self, payload):
        await self._gate.wait()
        self.sent.append(json.loads(payload))

    async def close(self, **_kwargs):
        self.closed = True
        self._gate.set()


class _RequestSocket(_OutputSocket):
    def __init__(self, messages):
        super().__init__()
        self._messages = iter(json.dumps(message) for message in messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._messages)
        except StopIteration:
            raise StopAsyncIteration


class _StalledLoop:
    def __init__(self):
        self.callbacks = []

    def is_running(self):
        return True

    def call_soon_threadsafe(self, callback, *args):
        self.callbacks.append((callback, args))


class WebSocketLifecycleTests(unittest.TestCase):
    def test_only_shared_mutation_responses_are_fanned_out(self):
        shared = (
            {"type": "preset_saved", "slot": 2},
            {"type": "preset_deleted", "slot": 2},
            {"type": "preset_transition", "slot": 2, "pending": True},
            {"type": "recording_deleted", "filename": "take.wav", "ok": True},
            {"type": "pad_slot_saved", "note": 60, "slots": []},
            {"type": "pad_slot_cleared", "note": 60, "slots": []},
            {"type": "instrument_mode", "mode": "organ"},
            {"type": "audio_output_set", "name": "USB", "success": True},
            {"type": "setting_ack", "section": "master", "param": "bpm"},
            {"type": "panic_ack", "fade_reset": True},
            {"type": "macro_assign_ack", "idx": 0, "assignments": []},
        )
        for response in shared:
            with self.subTest(response=response):
                self.assertTrue(ws_module._response_is_shared_mutation(response))

        private_or_failed = (
            {"type": "state", "state": {}},
            {"type": "recordings_list", "takes": []},
            {"type": "pad_slots", "slots": []},
            {"type": "cc_map", "map": {}},
            {"type": "audio_outputs", "outputs": []},
            {"type": "debug", "secret": "request-local"},
            {"type": "recording_deleted", "filename": "missing.wav", "ok": False},
            {"type": "audio_output_set", "name": "missing", "success": False},
        )
        for response in private_or_failed:
            with self.subTest(response=response):
                self.assertFalse(ws_module._response_is_shared_mutation(response))
        self.assertTrue(ws_module._response_is_shared_mutation(
            {"type": "cc_map", "map": {}}, {"type": "midi_learn_clear"}))
        self.assertFalse(ws_module._response_is_shared_mutation(
            {"type": "cc_map", "map": {}}, {"type": "get_cc_map"}))

    def test_successful_start_runtime_config_and_stop(self):
        with contextlib.ExitStack() as stack:
            server, http_port, websocket_port = _server_on_ephemeral_ports(stack)
            try:
                server.start(timeout=2.0)
                health = server.health_status()
                self.assertTrue(health["healthy"])
                self.assertTrue(health["event_loop_progressing"])
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
                self.assertTrue(server.stop())

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

    def test_stop_reports_nonquiescent_owner(self):
        class WedgedThread:
            @staticmethod
            def is_alive():
                return True

            @staticmethod
            def join(timeout=None):
                pass

        class FakeHTTP:
            @staticmethod
            def server_close():
                pass

        server = ws_module.WebSocketServer()
        server._http_thread = WedgedThread()
        server._http_server = FakeHTTP()
        self.assertFalse(server.stop())

    def test_stop_reports_active_handler_worker(self):
        server = ws_module.WebSocketServer()
        with server._handler_lock:
            server._handler_active = 1
            server._handler_busy_since = time.monotonic()
        self.assertFalse(server.stop())

    def test_handler_entering_after_stop_never_calls_retired_owner(self):
        called = []
        server = ws_module.WebSocketServer(message_handler=called.append)
        server._stop_requested.set()
        response = server._invoke_handler({"type": "setting"})
        self.assertEqual(called, [])
        self.assertEqual(response["type"], "error")
        self.assertEqual(server._handler_active, 0)
        server.stop()

    def test_loop_stall_burst_has_one_callback_and_bounded_mailbox(self):
        server = ws_module.WebSocketServer()
        loop = _StalledLoop()
        server._loop = loop
        try:
            for value in range(5000):
                self.assertTrue(server.broadcast_sync({
                    "type": "bus_comp_gr", "value": value,
                }))
            self.assertEqual(len(loop.callbacks), 1)
            self.assertEqual(len(server._producer_coalesced), 1)
            for value in range(ws_module._PRODUCER_CONTROL_LIMIT + 50):
                server.broadcast_sync({"type": "setting_ack", "value": value})
            self.assertEqual(len(loop.callbacks), 1)
            self.assertEqual(len(server._producer_control),
                             ws_module._PRODUCER_CONTROL_LIMIT)
            self.assertTrue(server._producer_overflow)
        finally:
            server._loop = None
            server.stop()


class WebSocketOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = ws_module.WebSocketServer()

    async def asyncTearDown(self):
        for socket, outbox in list(self.server._client_outboxes.items()):
            outbox.closed = True
            outbox.wakeup.set()
            if outbox.task:
                outbox.task.cancel()
                await asyncio.gather(outbox.task, return_exceptions=True)
        self.server._handler_pool.shutdown(wait=False)

    def _add(self, socket):
        outbox = ws_module._ClientOutbox()
        self.server.clients.add(socket)
        self.server._client_outboxes[socket] = outbox
        outbox.task = asyncio.create_task(
            self.server._client_sender(socket, outbox))
        return outbox

    async def test_mutation_reaches_peer_but_private_get_does_not(self):
        def dispatch(message):
            if message["type"] == "preset_save":
                return {"type": "preset_saved", "slot": message["slot"]}
            return {"type": "recordings_list", "takes": ["private"]}

        self.server.message_handler = dispatch
        observer = _OutputSocket()
        self._add(observer)
        requester = _RequestSocket([
            {"type": "preset_save", "slot": 3},
            {"type": "list_recordings"},
        ])
        await self.server._handle_client(requester)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertIn({"type": "preset_saved", "slot": 3}, requester.sent)
        self.assertIn({"type": "recordings_list", "takes": ["private"]}, requester.sent)
        self.assertEqual(observer.sent, [{"type": "preset_saved", "slot": 3}])

    async def test_slow_client_does_not_delay_fast_client_and_snapshots_coalesce(self):
        slow = _OutputSocket(blocked=True)
        fast = _OutputSocket()
        slow_box = self._add(slow)
        self._add(fast)

        self.server._broadcast_now({"type": "setting_ack", "value": 1})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(fast.sent, [{"type": "setting_ack", "value": 1}])
        self.assertEqual(slow.sent, [])

        for value in range(1000):
            self.server._broadcast_now({"type": "bus_comp_gr", "value": value})
        self.assertEqual(len(slow_box.coalesced), 1)
        self.assertLessEqual(len(slow_box.control), 1)

    async def test_noncoalescible_overflow_disconnects_only_slow_client(self):
        slow = _OutputSocket(blocked=True)
        fast = _OutputSocket()
        slow_box = self._add(slow)
        self._add(fast)
        self.server._send_to_client(slow, {"type": "setting_ack", "value": -1})
        await asyncio.sleep(0)
        for value in range(ws_module._CLIENT_CONTROL_LIMIT + 1):
            self.server._send_to_client(
                slow, {"type": "setting_ack", "value": value})
        await asyncio.sleep(0)
        self.assertTrue(slow_box.overflowed)
        self.assertTrue(slow.closed)
        self.assertFalse(fast.closed)
        self.assertEqual(self.server._slow_client_disconnects, 1)

    async def test_stale_heartbeat_makes_health_unhealthy(self):
        class AliveThread:
            @staticmethod
            def is_alive():
                return True

        self.server._loop = asyncio.get_running_loop()
        self.server._ws_thread = AliveThread()
        self.server._http_thread = AliveThread()
        self.server._ws_server = object()
        self.server._http_server = object()
        self.server._loop_last_progress = time.monotonic() - (
            ws_module._LOOP_STALE_SECONDS + 1.0)
        health = self.server.health_status()
        self.assertFalse(health["healthy"])
        self.assertFalse(health["event_loop_progressing"])

    async def test_stalled_handler_makes_health_unhealthy(self):
        class AliveThread:
            @staticmethod
            def is_alive():
                return True

        self.server._loop = asyncio.get_running_loop()
        self.server._ws_thread = AliveThread()
        self.server._http_thread = AliveThread()
        self.server._ws_server = object()
        self.server._http_server = object()
        self.server._loop_last_progress = time.monotonic()
        with self.server._handler_lock:
            self.server._handler_active = 1
            self.server._handler_busy_since = time.monotonic() - (
                ws_module._HANDLER_STALE_SECONDS + 1.0)
        health = self.server.health_status()
        self.assertFalse(health["healthy"])
        self.assertFalse(health["handler_worker_progressing"])
        self.assertGreater(health["handler_busy_age_seconds"],
                           ws_module._HANDLER_STALE_SECONDS)
        with self.server._handler_lock:
            self.server._handler_active = 0
            self.server._handler_busy_since = None

    async def test_client_admission_limit_rejects_extra_socket(self):
        class RejectedSocket:
            def __init__(self):
                self.close_args = None

            async def close(self, **kwargs):
                self.close_args = kwargs

        self.server.clients.update(object() for _ in range(ws_module._MAX_CLIENTS))
        rejected = RejectedSocket()
        await self.server._handle_client(rejected)
        self.assertEqual(rejected.close_args["code"], 1013)
        self.assertNotIn(rejected, self.server.clients)

    async def test_websocket_listener_uses_bounded_protocol_options(self):
        class DummyListener:
            def close(self):
                pass

            async def wait_closed(self):
                pass

        self.server._stop_requested.set()
        with mock.patch.object(
                ws_module.websockets, "serve",
                new=mock.AsyncMock(return_value=DummyListener())) as serve:
            await self.server._run_ws()
        kwargs = serve.await_args.kwargs
        self.assertEqual(kwargs["max_size"], 64 * 1024)
        self.assertEqual(kwargs["max_queue"], 16)
        self.assertEqual(kwargs["close_timeout"], 2)


if __name__ == "__main__":
    unittest.main()
