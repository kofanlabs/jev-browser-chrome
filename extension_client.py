"""Client and process lifecycle for the Jev Chrome extension bridge."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "local-state"
TOKEN_FILE = STATE / "extension-token"
API = "http://127.0.0.1:8766"


def token() -> str:
    return TOKEN_FILE.read_text(encoding="ascii").strip()


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
    try:
        return request("/status", timeout=1.0)
    except Exception:
        return {"connected": False, "daemon": False}


def ensure_daemon() -> dict:
    current = status()
    if current.get("daemon", True) or current.get("connected"):
        return current
    STATE.mkdir(parents=True, exist_ok=True)
    if not TOKEN_FILE.is_file():
        # Running the daemon once creates both the secret and extension/config.js.
        subprocess.run(
            [sys.executable, "-c", "import extension_daemon"],
            cwd=ROOT,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    log = (STATE / "extension-daemon.log").open("ab")
    subprocess.Popen(
        [sys.executable, str(ROOT / "extension_daemon.py")],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=log,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = status()
        if current.get("daemon", True):
            return current
        time.sleep(0.1)
    return status()


def call(method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
    ensure_daemon()
    return request("/call", {"method": method, "params": params or {}}, timeout=timeout)["result"]
