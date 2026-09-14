"""WebSocket server for real-time UI communication."""

import asyncio
import json
import logging
import threading
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
    "panic_ack",            # client-local UX feedback
    "midi_learn_active",    # learn-mode is per-client
    "recall_params_ack",    # already triggers a state broadcast
    "macro_assign_ack",     # already triggers a state broadcast
})


class WebSocketServer:
    """Bidirectional WebSocket server + HTTP server for serving the UI."""

    def __init__(self, message_handler=None):
        self.message_handler = message_handler  # Callback: (msg_dict) -> response_dict
        self.clients: set = set()
        self._ws_server = None
        self._ws_thread = None
        self._http_thread = None
        self._http_server = None
        self._loop = None
        self._ws_ready = threading.Event()
        self._ws_start_error = None
        self._stop_requested = threading.Event()
        self._stopped = False
        # Handlers run in ONE worker thread, not inline on the event loop:
        # some (set_audio_output, get_state's MIDI probe) run subprocess
        # chains with 5s timeouts that would otherwise stall every client's
        # messages and all broadcast traffic. max_workers=1 preserves the
        # serialized-dispatch ordering handlers were written for.
        self._handler_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ws-handler")

    async def _handle_client(self, websocket):
        """Handle a single WebSocket client connection."""
        self.clients.add(websocket)
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
                    await websocket.send(json.dumps({
                        "type": "error",
                        "for": None,
                        "message": "message must be a JSON object",
                    }))
                    continue

                logger.debug("WS received: %s", msg)

                if self.message_handler:
                    # Guard the handler: one exception (malformed field, a
                    # transient engine error) must not unwind the client
                    # coroutine and drop the connection mid-set — that's a
                    # UI blink and it masks the underlying bug.
                    try:
                        response = await asyncio.get_running_loop().run_in_executor(
                            self._handler_pool, self.message_handler, msg)
                    except Exception:
                        logger.exception("Handler error for message type %r",
                                         msg.get("type"))
                        response = {"type": "error",
                                    "for": msg.get("type"),
                                    "message": "internal handler error"}
                    if response:
                        await websocket.send(json.dumps(response))
                        # Broadcast UI-visible state changes to other clients so
                        # multi-screen setups (phone + tablet + pywebview) stay
                        # in sync without waiting for reconnect. Skip pure-data
                        # responses (peak meters, midi activity) and one-shot
                        # request/response acks that don't change UI state.
                        rtype = response.get("type", "") if isinstance(response, dict) else ""
                        if rtype.endswith("_ack") and rtype not in _ACK_NO_BROADCAST:
                            await self._broadcast(response, exclude=websocket)

        except websockets.ConnectionClosed:
            logger.info("WebSocket client disconnected: %s", remote)
        finally:
            self.clients.discard(websocket)

    async def _broadcast(self, msg: dict, exclude=None):
        """Send a message to all connected clients except exclude."""
        data = json.dumps(msg)
        for client in list(self.clients):
            if client != exclude:
                try:
                    await client.send(data)
                except websockets.ConnectionClosed:
                    self.clients.discard(client)

    def broadcast_sync(self, msg: dict):
        """Thread-safe broadcast from non-async code."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(msg), loop)
        except RuntimeError:
            # Loop was shut down between the is_running() check and scheduling.
            pass

    async def _run_ws(self):
        """Start the WebSocket server."""
        self._ws_server = await websockets.serve(
            self._handle_client,
            WEBSOCKET_HOST,
            WEBSOCKET_PORT,
        )
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
        self._handler_pool.shutdown(wait=False)
        logger.info("WebSocket server stopped")
