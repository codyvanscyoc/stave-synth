"""WebSocket server for real-time UI communication."""

import asyncio
from collections import deque
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path

import websockets

from .config import DATA_DIR, HTTP_PORT, INSTANCE_NAME, WEBSOCKET_HOST, WEBSOCKET_PORT

logger = logging.getLogger(__name__)

UI_DIR = Path(__file__).parent.parent / "ui"
RECORDINGS_DIR = DATA_DIR / "recordings"

# Acks that are per-client request/response only — no point broadcasting them.
# (Most _ack messages reflect a state change worth syncing; these are the
# exceptions.)
_ACK_NO_BROADCAST = frozenset({
    "midi_learn_active",    # learn-mode is per-client
    "recall_params_ack",    # already triggers a state broadcast
})

# Successful mutations whose historical response names predate the ``_ack``
# convention.  Keep this allowlist narrow: list/get/debug responses may contain
# request-specific data and must not be copied to every connected browser.
_SHARED_MUTATION_RESPONSE_TYPES = frozenset({
    "preset_transition",
    "preset_saved",
    "preset_deleted",
    "preset_labeled",
    "preset_swapped",
    "recording_deleted",
    "pad_slot_saved",
    "pad_slot_cleared",
    "instrument_mode",
    "audio_output_set",
})

# Control replies must remain ordered.  High-rate observations are snapshots:
# when a browser is behind, only the latest snapshot is useful.
_CLIENT_CONTROL_LIMIT = 128
_CLIENT_SNAPSHOT_LIMIT = 64
_PRODUCER_CONTROL_LIMIT = 256
_PRODUCER_SNAPSHOT_LIMIT = 64
_LOOP_STALE_SECONDS = 2.0
_HANDLER_STALE_SECONDS = 15.0
_MAX_CLIENTS = 16
_MAX_HTTP_WORKERS = 8
_COALESCED_TYPES = frozenset({
    "bus_comp_gr", "peak_level", "system_stats", "midi_status",
    "midi_activity", "state", "reverb_types_available", "pad_slots",
    "recordings_list",
})


def _coalesce_key(msg):
    if not isinstance(msg, dict):
        return None
    msg_type = msg.get("type")
    if msg_type in _COALESCED_TYPES:
        return str(msg_type)
    if msg_type in ("macro_cc_value", "macro_value_ack"):
        return f"{msg_type}:{msg.get('idx')}"
    if msg_type == "fader_ack" and msg.get("from_cc"):
        return f"fader_ack:{msg.get('id')}:{msg.get('alt', 0)}"
    return None


def _response_is_shared_mutation(response, request=None):
    """Whether a direct handler response also describes shared UI state."""
    if not isinstance(response, dict):
        return False
    response_type = response.get("type", "")
    # ``cc_map`` is both the private get response and the result of clearing a
    # shared mapping. Only the latter should fan out to the other controllers.
    if response_type == "cc_map":
        return isinstance(request, dict) and request.get("type") == "midi_learn_clear"
    if response_type.endswith("_ack"):
        return response_type not in _ACK_NO_BROADCAST
    if response_type not in _SHARED_MUTATION_RESPONSE_TYPES:
        return False
    # These response names are also used for non-mutating failures.
    if response_type == "recording_deleted":
        return response.get("ok") is True
    if response_type == "audio_output_set":
        return response.get("success") is True
    return True


class _ClientOutbox:
    """Event-loop-owned, bounded output storage for one browser."""

    def __init__(self):
        self.control = deque()
        self.coalesced = {}
        self.wakeup = asyncio.Event()
        self.task = None
        self.closed = False
        self.overflowed = False

    def put(self, data, key=None):
        if self.closed:
            return False
        if key is not None:
            if key not in self.coalesced and len(self.coalesced) >= _CLIENT_SNAPSHOT_LIMIT:
                return False
            self.coalesced[key] = data
        elif len(self.control) < _CLIENT_CONTROL_LIMIT:
            self.control.append(data)
        else:
            return False
        self.wakeup.set()
        return True

    def pop(self):
        if self.control:
            return self.control.popleft()
        if self.coalesced:
            key = next(iter(self.coalesced))
            return self.coalesced.pop(key)
        return None


class WebSocketServer:
    """Bidirectional WebSocket server + HTTP server for serving the UI."""

    def __init__(self, message_handler=None):
        self.message_handler = message_handler  # Callback: (msg_dict) -> response_dict
        self.clients: set = set()
        self._client_outboxes = {}
        self._ws_server = None
        self._ws_thread = None
        self._http_thread = None
        self._http_server = None
        self._loop = None
        self._ws_ready = threading.Event()
        self._ws_start_error = None
        self._stop_requested = threading.Event()
        self._stopped = False
        self._producer_lock = threading.Lock()
        self._producer_control = deque()
        self._producer_coalesced = {}
        self._producer_drain_scheduled = False
        self._producer_overflow = False
        self._producer_overflow_count = 0
        self._slow_client_disconnects = 0
        self._loop_last_progress = 0.0
        self._handler_pool_available = True
        self._handler_lock = threading.Lock()
        self._handler_active = 0
        self._handler_busy_since = None
        self._http_worker_lock = threading.Lock()
        self._http_active_workers = 0
        self._http_workers_zero = threading.Event()
        self._http_workers_zero.set()
        # Handlers run in ONE worker thread, not inline on the event loop:
        # some (set_audio_output, get_state's MIDI probe) run subprocess
        # chains with 5s timeouts that would otherwise stall every client's
        # messages and all broadcast traffic. max_workers=1 preserves the
        # serialized-dispatch ordering handlers were written for.
        self._handler_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ws-handler")

    async def _handle_client(self, websocket):
        """Handle a single WebSocket client connection."""
        if len(self.clients) >= _MAX_CLIENTS:
            await websocket.close(code=1013, reason="too many control clients")
            return
        self.clients.add(websocket)
        outbox = _ClientOutbox()
        self._client_outboxes[websocket] = outbox
        outbox.task = asyncio.create_task(
            self._client_sender(websocket, outbox), name="stave-ws-client-sender")
        remote = websocket.remote_address
        logger.info("WebSocket client connected: %s", remote)

        try:
            async for message in websocket:
                try:
                    msg = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON from client: %s", message[:100])
                    continue

                if not isinstance(msg, dict):
                    self._send_to_client(websocket, {
                        "type": "error",
                        "for": None,
                        "message": "message must be a JSON object",
                    })
                    continue

                logger.debug("WS received: %s", msg)

                if self.message_handler:
                    # Guard the handler: one exception (malformed field, a
                    # transient engine error) must not unwind the client
                    # coroutine and drop the connection mid-set — that's a
                    # UI blink and it masks the underlying bug.
                    try:
                        response = await asyncio.get_running_loop().run_in_executor(
                            self._handler_pool, self._invoke_handler, msg)
                    except RuntimeError as exc:
                        if "schedule" in str(exc).lower() or "shutdown" in str(exc).lower():
                            self._handler_pool_available = False
                        logger.exception("Handler worker unavailable for message type %r",
                                         msg.get("type"))
                        response = {"type": "error", "for": msg.get("type"),
                                    "message": "internal handler error"}
                    except Exception:
                        logger.exception("Handler error for message type %r",
                                         msg.get("type"))
                        response = {"type": "error",
                                    "for": msg.get("type"),
                                    "message": "internal handler error"}
                    if response:
                        self._send_to_client(websocket, response)
                        # Broadcast UI-visible state changes to other clients so
                        # multi-screen setups (phone + tablet + pywebview) stay
                        # in sync without waiting for reconnect. Skip pure-data
                        # responses (peak meters, midi activity) and one-shot
                        # request/response acks that don't change UI state.
                        if _response_is_shared_mutation(response, msg):
                            self._broadcast_now(response, exclude=websocket)

        except websockets.ConnectionClosed:
            logger.info("WebSocket client disconnected: %s", remote)
        finally:
            self.clients.discard(websocket)
            current = self._client_outboxes.pop(websocket, None)
            if current is outbox:
                # Give already-queued terminal replies one event-loop turn to
                # reach fast clients before cancellation during clean EOF.
                await asyncio.sleep(0)
                outbox.closed = True
                outbox.wakeup.set()
                if outbox.task is not asyncio.current_task():
                    outbox.task.cancel()
                    await asyncio.gather(outbox.task, return_exceptions=True)

    def _invoke_handler(self, msg):
        """Track execution in the actual worker, including after cancellation."""
        with self._handler_lock:
            # A Future may be marked running immediately before the executor
            # enters this wrapper. stop() sets this flag before inspecting
            # active ownership, so a late entrant must never call the retired
            # application's handler behind a replacement UI server.
            if self._stop_requested.is_set():
                return {"type": "error", "for": msg.get("type"),
                        "message": "control service is stopping"}
            if self._handler_active == 0:
                self._handler_busy_since = time.monotonic()
            self._handler_active += 1
        try:
            return self.message_handler(msg)
        finally:
            with self._handler_lock:
                self._handler_active -= 1
                if self._handler_active == 0:
                    self._handler_busy_since = None

    async def _client_sender(self, websocket, outbox):
        """The sole websocket.send owner for a client."""
        try:
            while not outbox.closed:
                await outbox.wakeup.wait()
                while not outbox.closed:
                    data = outbox.pop()
                    if data is None:
                        outbox.wakeup.clear()
                        # No producer can mutate an outbox outside this loop,
                        # so clearing after the empty read cannot lose a wakeup.
                        break
                    await websocket.send(data)
        except asyncio.CancelledError:
            raise
        except websockets.ConnectionClosed:
            pass
        except Exception:
            logger.exception("WebSocket client sender failed")
        finally:
            outbox.closed = True
            self.clients.discard(websocket)
            if self._client_outboxes.get(websocket) is outbox:
                self._client_outboxes.pop(websocket, None)
            try:
                await websocket.close()
            except Exception:
                pass

    def _send_serialized(self, websocket, data, key=None):
        outbox = self._client_outboxes.get(websocket)
        if outbox is None:
            return False
        if outbox.put(data, key):
            return True
        if not outbox.overflowed:
            outbox.overflowed = True
            self._slow_client_disconnects += 1
            logger.warning("Disconnecting slow WebSocket client: control queue full")
            asyncio.create_task(websocket.close(
                code=1013, reason="client cannot keep up with control updates"))
        return False

    def _send_to_client(self, websocket, msg):
        try:
            data = json.dumps(msg)
        except (TypeError, ValueError):
            logger.exception("Could not serialize WebSocket response")
            return False
        return self._send_serialized(websocket, data, _coalesce_key(msg))

    def _broadcast_serialized(self, data, key=None, exclude=None):
        for client in list(self.clients):
            if client != exclude:
                self._send_serialized(client, data, key)

    def _broadcast_now(self, msg, exclude=None):
        try:
            data = json.dumps(msg)
        except (TypeError, ValueError):
            logger.exception("Could not serialize WebSocket broadcast")
            return False
        self._broadcast_serialized(data, _coalesce_key(msg), exclude)
        return True

    async def _broadcast(self, msg: dict, exclude=None):
        """Compatibility coroutine: enqueue without waiting on client I/O."""
        self._broadcast_now(msg, exclude)

    def _drain_producer_mailbox(self):
        """Move one bounded cross-thread batch into event-loop outboxes."""
        with self._producer_lock:
            control = list(self._producer_control)
            snapshots = list(self._producer_coalesced.values())
            overflow = self._producer_overflow
            self._producer_control.clear()
            self._producer_coalesced.clear()
            self._producer_overflow = False
            self._producer_drain_scheduled = False
        if overflow:
            # Losing an ordered state transition would be less truthful than a
            # reconnect + authoritative hydration. This path creates at most
            # one close task per currently connected client.
            for client in list(self.clients):
                outbox = self._client_outboxes.get(client)
                if outbox is not None and not outbox.overflowed:
                    outbox.overflowed = True
                    self._slow_client_disconnects += 1
                    asyncio.create_task(client.close(
                        code=1013, reason="server control broadcast overflow"))
            return
        for data, key in control:
            self._broadcast_serialized(data, key)
        for data, key in snapshots:
            self._broadcast_serialized(data, key)

    def broadcast_sync(self, msg: dict):
        """Thread-safe broadcast from non-async code."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return False
        try:
            data = json.dumps(msg)
        except (TypeError, ValueError):
            logger.exception("Could not serialize WebSocket broadcast")
            return False
        key = _coalesce_key(msg)
        schedule = False
        with self._producer_lock:
            if key is not None:
                if (key in self._producer_coalesced
                        or len(self._producer_coalesced) < _PRODUCER_SNAPSHOT_LIMIT):
                    self._producer_coalesced[key] = (data, key)
                else:
                    self._producer_overflow = True
                    self._producer_overflow_count += 1
            elif len(self._producer_control) < _PRODUCER_CONTROL_LIMIT:
                self._producer_control.append((data, None))
            else:
                self._producer_overflow = True
                self._producer_overflow_count += 1
            if not self._producer_drain_scheduled:
                self._producer_drain_scheduled = True
                schedule = True
        if not schedule:
            return True
        try:
            loop.call_soon_threadsafe(self._drain_producer_mailbox)
        except RuntimeError:
            # Loop was shut down between the is_running() check and scheduling.
            with self._producer_lock:
                self._producer_drain_scheduled = False
            return False
        return True

    def _loop_heartbeat(self):
        self._loop_last_progress = time.monotonic()
        if not self._stop_requested.is_set():
            asyncio.get_running_loop().call_later(0.25, self._loop_heartbeat)

    def health_status(self):
        """Return a non-blocking, thread-safe-enough control-plane snapshot."""
        now = time.monotonic()
        loop = self._loop
        loop_running = bool(loop and loop.is_running())
        loop_age = max(0.0, now - self._loop_last_progress) if self._loop_last_progress else None
        ws_thread_alive = bool(self._ws_thread and self._ws_thread.is_alive())
        http_thread_alive = bool(self._http_thread and self._http_thread.is_alive())
        with self._http_worker_lock:
            http_active_workers = self._http_active_workers
        ws_listening = bool(self._ws_server is not None and ws_thread_alive)
        http_listening = bool(self._http_server is not None and http_thread_alive)
        loop_progressing = bool(loop_running and loop_age is not None
                                and loop_age <= _LOOP_STALE_SECONDS)
        with self._handler_lock:
            handler_active = self._handler_active
            handler_busy_since = self._handler_busy_since
        handler_busy_age = (max(0.0, now - handler_busy_since)
                            if handler_busy_since is not None else None)
        handler_progressing = bool(
            self._handler_pool_available
            and (handler_busy_age is None or handler_busy_age <= _HANDLER_STALE_SECONDS))
        healthy = bool(not self._stopped and ws_listening and http_listening
                       and loop_progressing and handler_progressing)
        return {
            "healthy": healthy,
            "websocket_listener": ws_listening,
            "http_listener": http_listening,
            "event_loop_progressing": loop_progressing,
            "event_loop_progress_age_seconds": loop_age,
            "handler_worker_available": self._handler_pool_available,
            "handler_worker_progressing": handler_progressing,
            "handler_active": handler_active,
            "handler_busy_age_seconds": handler_busy_age,
            "clients": len(self.clients),
            "http_active_workers": http_active_workers,
            "slow_client_disconnects": self._slow_client_disconnects,
            "producer_overflows": self._producer_overflow_count,
        }

    async def _run_ws(self):
        """Start the WebSocket server."""
        self._ws_server = await websockets.serve(
            self._handle_client,
            WEBSOCKET_HOST,
            WEBSOCKET_PORT,
            max_size=64 * 1024,
            max_queue=16,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=2,
        )
        self._loop_heartbeat()
        self._ws_ready.set()
        logger.info("WebSocket server listening on ws://%s:%d", WEBSOCKET_HOST, WEBSOCKET_PORT)
        if self._stop_requested.is_set():
            self._ws_server.close()
        await self._ws_server.wait_closed()

    def _make_http_server(self):
        """Bind and return the HTTP server used for UI and recording files."""
        recordings_dir = RECORDINGS_DIR
        recordings_dir.mkdir(parents=True, exist_ok=True)
        ui_dir = UI_DIR
        runtime_config = (
            "window.STAVE_RUNTIME = "
            + json.dumps({
                "websocket_port": WEBSOCKET_PORT,
                "instance": INSTANCE_NAME,
            }, separators=(",", ":"))
            + ";\n"
        ).encode("utf-8")

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                # default directory = UI; /recordings/* is rerouted in translate_path
                super().__init__(*args, directory=str(ui_dir), **kwargs)

            def do_GET(self):
                request_path = self.path.split("?", 1)[0].split("#", 1)[0]
                if request_path == "/runtime-config.js":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/javascript; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(runtime_config)))
                    self.end_headers()
                    self.wfile.write(runtime_config)
                    return
                super().do_GET()

            def translate_path(self, path):
                # Serve WAVs from recordings dir under the /recordings/ prefix
                if path.startswith("/recordings/"):
                    rel = path[len("/recordings/"):].split("?", 1)[0].split("#", 1)[0]
                    # Strip any traversal shenanigans
                    rel = rel.replace("..", "").lstrip("/")
                    return str(recordings_dir / rel)
                return super().translate_path(path)

            def log_message(self, format, *args):
                logger.debug("HTTP: " + format, *args)

        class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True
            block_on_close = False

            def __init__(self, *args, **kwargs):
                self._worker_slots = threading.BoundedSemaphore(_MAX_HTTP_WORKERS)
                super().__init__(*args, **kwargs)

            def process_request(self, request, client_address):
                if not self._worker_slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                request.settimeout(2.0)
                with owner._http_worker_lock:
                    owner._http_active_workers += 1
                    owner._http_workers_zero.clear()
                try:
                    super().process_request(request, client_address)
                except BaseException:
                    with owner._http_worker_lock:
                        owner._http_active_workers -= 1
                        if owner._http_active_workers == 0:
                            owner._http_workers_zero.set()
                    self._worker_slots.release()
                    raise

            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    with owner._http_worker_lock:
                        owner._http_active_workers -= 1
                        if owner._http_active_workers == 0:
                            owner._http_workers_zero.set()
                    self._worker_slots.release()

        owner = self
        return ThreadingHTTPServer((WEBSOCKET_HOST, HTTP_PORT), Handler)

    def _run_http(self):
        """Serve requests on the HTTP listener already bound by start()."""
        server = self._http_server
        if server is None:
            return
        logger.info("HTTP server serving UI on http://%s:%d", WEBSOCKET_HOST, HTTP_PORT)
        server.timeout = 0.1
        try:
            # handle_request() honors server.timeout, so stop is controlled by
            # our event and never waits inside HTTPServer.shutdown(). This also
            # handles stop-before-thread-entry deterministically.
            while not self._stop_requested.is_set():
                server.handle_request()
        finally:
            server.server_close()

    def start(self, timeout: float = 5.0):
        """Start both listeners, raising unless both binds succeed."""
        if self._stopped:
            raise RuntimeError("WebSocketServer cannot be restarted after stop")
        if ((self._http_thread and self._http_thread.is_alive())
                or (self._ws_thread and self._ws_thread.is_alive())):
            raise RuntimeError("WebSocketServer is already running")

        self._stop_requested.clear()
        # Bind HTTP synchronously so a conflict cannot be hidden in a daemon
        # thread while the main service reports ready.
        self._http_server = self._make_http_server()
        self._http_thread = threading.Thread(target=self._run_http, daemon=True)
        self._http_thread.start()

        self._ws_ready.clear()
        self._ws_start_error = None

        # WebSocket binding is asynchronous, so its thread reports the bind
        # result through _ws_ready before start() is allowed to return.
        def run_ws_loop():
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._run_ws())
            except BaseException as exc:
                self._ws_start_error = exc
                self._ws_ready.set()
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                finally:
                    loop.close()
                    if self._loop is loop:
                        self._loop = None

        self._ws_thread = threading.Thread(target=run_ws_loop, daemon=True)
        self._ws_thread.start()

        if not self._ws_ready.wait(timeout=max(0.1, float(timeout))):
            self.stop()
            raise TimeoutError("WebSocket listener did not become ready")
        if self._ws_start_error is not None:
            error = self._ws_start_error
            self.stop()
            raise RuntimeError("Failed to start WebSocket listener") from error

    def stop(self):
        """Stop servers. Called from a foreign thread — the server object
        must be closed on its own event loop."""
        self._stop_requested.set()
        self._stopped = True
        http_server = self._http_server
        http_thread = self._http_thread
        if http_thread and http_thread.is_alive():
            http_thread.join(timeout=2.0)
        if http_server is not None:
            try:
                http_server.server_close()
            except OSError:
                pass
        self._http_workers_zero.wait(timeout=2.0)
        self._http_server = None

        if self._ws_server and self._loop and self._loop.is_running():
            try:
                self._loop.call_soon_threadsafe(self._ws_server.close)
            except RuntimeError:
                pass  # loop shut down between check and call
        elif self._ws_server:
            self._ws_server.close()
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=2.0)
        self._ws_server = None
        self._handler_pool_available = False
        self._handler_pool.shutdown(wait=False, cancel_futures=True)
        with self._handler_lock:
            handler_quiescent = self._handler_active == 0
        with self._http_worker_lock:
            http_workers_quiescent = self._http_active_workers == 0
        http_quiescent = bool(not (http_thread and http_thread.is_alive())
                              and http_workers_quiescent)
        ws_quiescent = not (self._ws_thread and self._ws_thread.is_alive())
        quiescent = bool(handler_quiescent and http_quiescent and ws_quiescent)
        if quiescent:
            logger.info("WebSocket server stopped")
        else:
            logger.error("WebSocket server stop incomplete: http=%s ws=%s handler=%s",
                         http_quiescent, ws_quiescent, handler_quiescent)
        return quiescent
