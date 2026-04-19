"""WebSocket server for real-time UI communication."""

import asyncio
import json
import logging
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path

import websockets

from .config import WEBSOCKET_HOST, WEBSOCKET_PORT, HTTP_PORT

logger = logging.getLogger(__name__)

UI_DIR = Path(__file__).parent.parent / "ui"
RECORDINGS_DIR = Path.home() / ".local" / "share" / "stave-synth" / "recordings"

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
        self._http_thread = None
        self._loop = None

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

                logger.debug("WS received: %s", msg)

                if self.message_handler:
                    response = self.message_handler(msg)
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
        logger.info("WebSocket server listening on ws://%s:%d", WEBSOCKET_HOST, WEBSOCKET_PORT)
        await self._ws_server.wait_closed()

    def _run_http(self):
        """Run a simple HTTP server to serve the UI files + recordings dir."""

        recordings_dir = RECORDINGS_DIR
        recordings_dir.mkdir(parents=True, exist_ok=True)
        ui_dir = UI_DIR

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                # default directory = UI; /recordings/* is rerouted in translate_path
                super().__init__(*args, directory=str(ui_dir), **kwargs)

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

        server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
        logger.info("HTTP server serving UI on http://0.0.0.0:%d", HTTP_PORT)
        server.serve_forever()

    def start(self):
        """Start both WebSocket and HTTP servers."""
        # Start HTTP server in a daemon thread
        self._http_thread = threading.Thread(target=self._run_http, daemon=True)
        self._http_thread.start()

        # Start WebSocket server in its own event loop (also daemon thread)
        def run_ws_loop():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._run_ws())

        self._ws_thread = threading.Thread(target=run_ws_loop, daemon=True)
        self._ws_thread.start()

    def stop(self):
        """Stop servers."""
        if self._ws_server:
            self._ws_server.close()
        logger.info("WebSocket server stopped")
