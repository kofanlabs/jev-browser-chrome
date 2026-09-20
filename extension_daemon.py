"""Authenticated loopback bridge between the Jev MCP and its Chrome extension."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from websockets.sync.server import serve

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "local-state"
TOKEN_FILE = STATE / "extension-token"
PID_FILE = STATE / "extension-daemon.pid"
CONFIG_FILE = ROOT / "extension/config.js"
WS_PORT = 8765
API_PORT = 8766


def ensure_credentials() -> str:
    STATE.mkdir(parents=True, exist_ok=True)
    try:
        token = TOKEN_FILE.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        token = secrets.token_urlsafe(32)
        TOKEN_FILE.write_text(token, encoding="ascii")
    if len(token) < 32:
        raise RuntimeError("Invalid Jev extension bridge token")
    CONFIG_FILE.write_text(
        f"self.JEV_BRIDGE_CONFIG = {{port: {WS_PORT}, token: {json.dumps(token)}}};\n",
        encoding="utf-8",
    )
    return token


class Hub:
    def __init__(self, token: str):
        self.token = token
        self.connection = None
        self.info: dict = {}
        self.last_seen = 0.0
        self.lock = threading.RLock()
        self.rpc_lock = threading.Lock()
        self.pending: dict[str, tuple[threading.Event, dict]] = {}

    def connected(self) -> bool:
        with self.lock:
            return self.connection is not None and time.monotonic() - self.last_seen < 50

    def status(self) -> dict:
        with self.lock:
            return {"daemon": True, "connected": self.connected(), **self.info}

    def handle_socket(self, websocket) -> None:
        if websocket.request.path != f"/{self.token}":
            websocket.close(code=1008, reason="unauthorized")
            return
        with self.lock:
            old = self.connection
            self.connection = websocket
            self.info = {}
            self.last_seen = time.monotonic()
        if old is not None and old is not websocket:
            try:
                old.close(code=1001, reason="replaced")
            except Exception:
                pass
        try:
            for raw in websocket:
                message = json.loads(raw)
                with self.lock:
                    self.last_seen = time.monotonic()
                    if message.get("type") == "hello":
                        self.info = {
                            "extensionId": message.get("extensionId"),
                            "extensionVersion": message.get("version"),
                        }
                    elif message.get("type") == "response":
                        item = self.pending.get(message.get("id"))
                        if item:
                            item[1].update(message)
                            item[0].set()
        finally:
            with self.lock:
                if self.connection is websocket:
                    self.connection = None
                    self.info = {}
                for event, result in self.pending.values():
                    result.setdefault("error", "Chrome extension disconnected")
                    event.set()

    def call(self, method: str, params: dict, timeout: float = 15.0) -> dict:
        with self.rpc_lock:
            with self.lock:
                websocket = self.connection
                if websocket is None or not self.connected():
                    raise RuntimeError("Jev Chrome extension is not connected")
                request_id = str(uuid.uuid4())
                event = threading.Event()
                result: dict = {}
                self.pending[request_id] = (event, result)
                payload = json.dumps(
                    {
                        "type": "command",
                        "id": request_id,
                        "method": method,
                        "params": params,
                    }
                )
            try:
                websocket.send(payload)
                if not event.wait(timeout):
                    raise TimeoutError(f"Chrome extension timed out during {method}")
                if result.get("error"):
                    raise RuntimeError(str(result["error"]))
                value = result.get("result")
                if not isinstance(value, dict):
                    raise RuntimeError("Chrome extension returned an invalid response")
                return value
            finally:
                with self.lock:
                    self.pending.pop(request_id, None)


TOKEN = ensure_credentials()
HUB = Hub(TOKEN)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "JevExtensionBridge/1.0"

    def log_message(self, _format, *_args):
        return

    def authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.authorized():
            self.send_json(403, {"error": "forbidden"})
        elif self.path == "/status":
            self.send_json(200, HUB.status())
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self.authorized():
            self.send_json(403, {"error": "forbidden"})
            return
        if self.path != "/call":
            self.send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("invalid request size")
            request = json.loads(self.rfile.read(length))
            method = request["method"]
            params = request.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise ValueError("invalid request")
            self.send_json(200, {"result": HUB.call(method, params)})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)[:500]})


def main() -> None:
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    websocket_server = serve(HUB.handle_socket, "127.0.0.1", WS_PORT)
    websocket_thread = threading.Thread(target=websocket_server.serve_forever, daemon=True)
    websocket_thread.start()
    api = ThreadingHTTPServer(("127.0.0.1", API_PORT), ApiHandler)
    try:
        api.serve_forever()
    finally:
        api.server_close()
        websocket_server.shutdown()
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
