"""Client and process lifecycle for the Jev Chrome extension bridge."""

from __future__ import annotations

import http.client
import json
import math
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "local-state"
TOKEN_FILE = STATE / "extension-token"
API = "http://127.0.0.1:8766"
_DAEMON_LOCK = threading.RLock()
_DAEMON_KNOWN = False
MAX_RPC_TIMEOUT = 60.0


def token() -> str:
    return TOKEN_FILE.read_text(encoding="ascii").strip()


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


def request(path: str, body: dict | None = None, timeout: float = 20.0) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        API + path,
        data=data,
        method="GET" if data is None else "POST",
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("error", str(exc))
        except Exception:
            detail = str(exc)
        raise RuntimeError(detail) from None
    if payload.get("error"):
        raise RuntimeError(payload["error"])
    return payload


def status() -> dict:
    global _DAEMON_KNOWN
    try:
        current = request("/status", timeout=1.0)
    except Exception:
        with _DAEMON_LOCK:
            _DAEMON_KNOWN = False
        return {"connected": False, "daemon": False}
    with _DAEMON_LOCK:
        _DAEMON_KNOWN = bool(current.get("daemon"))
    return current


def ensure_daemon() -> dict:
    global _DAEMON_KNOWN
    with _DAEMON_LOCK:
        # Explicit ensure requests always verify the current listener. RPC calls
        # with a warm cache use /call itself as their liveness check.
        current = status()
        if current.get("daemon"):
            return current

        STATE.mkdir(parents=True, exist_ok=True)
        try:
            with (STATE / "extension-daemon.log").open("ab") as log:
                subprocess.Popen(
                    [sys.executable, str(ROOT / "extension_daemon.py")],
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
        except OSError:
            # Another client process may have won the startup race. Check for
            # its listener before surfacing a startup failure.
            current = status()
            if current.get("daemon"):
                return current
            raise

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = status()
            if current.get("daemon"):
                return current
            time.sleep(0.1)
        return status()


def call(method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
    global _DAEMON_KNOWN
    timeout = _validate_timeout(timeout)
    deadline = time.monotonic() + timeout
    with _DAEMON_LOCK:
        daemon_known = _DAEMON_KNOWN
    if not daemon_known:
        bridge = ensure_daemon()
        if not bridge.get("daemon"):
            raise RuntimeError("Jev extension daemon did not start")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"Jev extension call timed out before dispatch: {method}")
    try:
        response = request(
            "/call",
            {"method": method, "params": params or {}, "timeout": remaining},
            timeout=remaining,
        )
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        # The POST may already have reached the daemon. Invalidate liveness
        # state, but never send the command again after an ambiguous failure.
        with _DAEMON_LOCK:
            _DAEMON_KNOWN = False
        raise
    with _DAEMON_LOCK:
        _DAEMON_KNOWN = True
    return response["result"]
