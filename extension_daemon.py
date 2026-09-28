"""Authenticated loopback bridge between the Jev MCP and its Chrome extension."""

from __future__ import annotations

import json
import math
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
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
DEFAULT_RPC_TIMEOUT = 15.0
MAX_RPC_TIMEOUT = 60.0


@dataclass
class PendingCall:
    websocket: object
    event: threading.Event
    result: dict


def ensure_credentials() -> str:
    STATE.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        STATE.chmod(0o700)
    try:
        token_fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(token_fd, "w", encoding="ascii") as token_file:
            token = secrets.token_urlsafe(32)
            token_file.write(token)
            token_file.flush()
            os.fsync(token_file.fileno())
    except FileExistsError:
        # Another initializer may have created the file but not finished
        # writing it yet. Wait for that single creator instead of minting a
        # second token and racing its extension config.
        token = ""
        for _ in range(100):
            try:
                token = TOKEN_FILE.read_text(encoding="ascii").strip()
            except FileNotFoundError:
                pass
            if token:
                break
            time.sleep(0.01)
        if not token:
            raise RuntimeError("Unable to read the Jev extension bridge token")
    if len(token) < 32:
        raise RuntimeError("Invalid Jev extension bridge token")
    CONFIG_FILE.write_text(
        f"self.JEV_BRIDGE_CONFIG = {{port: {WS_PORT}, token: {json.dumps(token)}}};\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        TOKEN_FILE.chmod(0o600)
        CONFIG_FILE.chmod(0o600)
    return token


class Hub:
    def __init__(self, token: str):
        self.token = token
        self.connection = None
        self.info: dict = {}
        self.last_seen = 0.0
        self.lock = threading.RLock()
        self.rpc_lock = threading.Lock()
        self.pending: dict[str, PendingCall] = {}

    def _connected_unlocked(self) -> bool:
        return self.connection is not None and time.monotonic() - self.last_seen < 50

    def _fail_pending_for(self, websocket, error: str) -> None:
        for pending in self.pending.values():
            if pending.websocket is websocket:
                pending.result.setdefault("error", error)
                pending.event.set()

    @staticmethod
    def _validate_timeout(timeout: object) -> float:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a finite number between 0 and 60 seconds")
        try:
            value = float(timeout)
        except (OverflowError, ValueError):
            raise ValueError("timeout must be a finite number between 0 and 60 seconds") from None
        if not math.isfinite(value) or value <= 0 or value > MAX_RPC_TIMEOUT:
            raise ValueError("timeout must be a finite number between 0 and 60 seconds")
        return value

    def connected(self) -> bool:
        with self.lock:
            return self._connected_unlocked()

    def status(self) -> dict:
        with self.lock:
            connected = self._connected_unlocked()
            info = self.info if connected else {}
            return {"daemon": True, "connected": connected, **info}

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
                self._fail_pending_for(old, "Chrome extension connection was replaced")
        if old is not None and old is not websocket:
            try:
                old.close(code=1001, reason="replaced")
            except Exception:
                pass
        try:
            for raw in websocket:
                message = json.loads(raw)
                with self.lock:
                    # A socket may deliver buffered frames after a replacement.
                    # They must not refresh or mutate the new connection state.
                    if self.connection is not websocket or not isinstance(message, dict):
                        continue
                    self.last_seen = time.monotonic()
                    if message.get("type") == "hello":
                        self.info = {
                            "extensionId": message.get("extensionId"),
                            "extensionVersion": message.get("version"),
                        }
                    elif message.get("type") == "response":
                        request_id = message.get("id")
                        pending = self.pending.get(request_id) if isinstance(request_id, str) else None
                        if pending is not None and pending.websocket is websocket:
                            pending.result.update(message)
                            pending.event.set()
        finally:
            with self.lock:
                if self.connection is websocket:
                    self.connection = None
                    self.info = {}
                self._fail_pending_for(websocket, "Chrome extension disconnected")

    def call(self, method: str, params: dict, timeout: float = 15.0) -> dict:
        timeout = self._validate_timeout(timeout)
        deadline = time.monotonic() + timeout
        if not self.rpc_lock.acquire(timeout=timeout):
            raise TimeoutError(f"Chrome extension timed out waiting to run {method}")
        request_id = None
        pending = None
        failed_send = False
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Chrome extension timed out waiting to run {method}")
            with self.lock:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Chrome extension timed out before sending {method}")
                websocket = self.connection
                if websocket is None or not self._connected_unlocked():
                    raise RuntimeError("Jev Chrome extension is not connected")
                request_id = str(uuid.uuid4())
                pending = PendingCall(websocket, threading.Event(), {})
                self.pending[request_id] = pending
                payload = json.dumps(
                    {
                        "type": "command",
                        "id": request_id,
                        "method": method,
                        "params": params,
                    }
                )
                if time.monotonic() >= deadline:
                    self.pending.pop(request_id, None)
                    request_id = None
                    raise TimeoutError(f"Chrome extension timed out before sending {method}")
                try:
                    # Keep replacement from interleaving between selecting the
                    # socket and sending the command.
                    websocket.send(payload)
                except Exception:
                    if self.connection is websocket:
                        self.connection = None
                        self.info = {}
                    self._fail_pending_for(websocket, "Chrome extension connection failed")
                    failed_send = True

            if failed_send:
                try:
                    websocket.close(code=1011, reason="send failed")
                except Exception:
                    pass
                raise RuntimeError("Chrome extension connection failed") from None

            if not pending.event.wait(max(0.0, deadline - time.monotonic())):
                with self.lock:
                    timed_out = not pending.event.is_set()
                if timed_out:
                    # The command may have reached Chrome. Surface the timeout
                    # and never replay it.
                    raise TimeoutError(f"Chrome extension timed out during {method}")
            if pending.result.get("error"):
                raise RuntimeError(str(pending.result["error"]))
            value = pending.result.get("result")
            if not isinstance(value, dict):
                raise RuntimeError("Chrome extension returned an invalid response")
            return value
        finally:
            if request_id is not None:
                with self.lock:
                    self.pending.pop(request_id, None)
            self.rpc_lock.release()


TOKEN: str | None = None
HUB: Hub | None = None


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "JevExtensionBridge/1.0"

    def log_message(self, _format, *_args):
        return

    def authorized(self) -> bool:
        return TOKEN is not None and self.headers.get("Authorization") == f"Bearer {TOKEN}"

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
            if HUB is None:
                self.send_json(503, {"error": "bridge is starting"})
                return
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
            timeout = Hub._validate_timeout(request.get("timeout", DEFAULT_RPC_TIMEOUT))
            if not isinstance(method, str) or not isinstance(params, dict):
                raise ValueError("invalid request")
            if HUB is None:
                self.send_json(503, {"error": "bridge is starting"})
                return
            self.send_json(200, {"result": HUB.call(method, params, timeout=timeout)})
        except TimeoutError as exc:
            self.send_json(504, {"error": str(exc)[:500]})
        except RuntimeError as exc:
            self.send_json(503, {"error": str(exc)[:500]})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)[:500]})


def _write_pid_file(pid: int) -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = PID_FILE.with_name(f"{PID_FILE.name}.{pid}.tmp")
    temporary.write_text(str(pid), encoding="ascii")
    temporary.replace(PID_FILE)


def _remove_pid_file(pid: int) -> None:
    try:
        if PID_FILE.read_text(encoding="ascii").strip() == str(pid):
            PID_FILE.unlink()
    except FileNotFoundError:
        pass


def main() -> None:
    global HUB, TOKEN

    # Bind the WebSocket port before creating credentials. Competing cold-start
    # daemons then fail before they can race to replace the shared token/config.
    HUB = Hub("")
    pid = os.getpid()
    websocket_server = None
    websocket_thread = None
    api = None
    pid_written = False
    try:
        websocket_server = serve(HUB.handle_socket, "127.0.0.1", WS_PORT)
        websocket_thread = threading.Thread(target=websocket_server.serve_forever, daemon=True)
        websocket_thread.start()
        api = ThreadingHTTPServer(("127.0.0.1", API_PORT), ApiHandler)
        TOKEN = ensure_credentials()
        HUB.token = TOKEN
        _write_pid_file(pid)
        pid_written = True
        api.serve_forever()
    finally:
        if api is not None:
            api.server_close()
        if websocket_server is not None:
            websocket_server.shutdown()
        if websocket_thread is not None:
            websocket_thread.join(timeout=1)
        if pid_written:
            _remove_pid_file(pid)


if __name__ == "__main__":
    main()
