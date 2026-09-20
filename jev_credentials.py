"""Load a Jev provider key without storing plaintext in the repository."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def prepare_provider() -> None:
    """Prefer environment variables, then the optional Windows DPAPI store."""
    if os.environ.get("TYPESAFE_API_KEY"):
        return
    if os.environ.get("AI_GATEWAY_API_KEY"):
        os.environ["TYPESAFE_API_KEY"] = os.environ["AI_GATEWAY_API_KEY"]
        os.environ.setdefault("TYPESAFE_BASE_URL", "https://ai-gateway.vercel.sh/typesafe")
        os.environ.setdefault("TYPESAFE_DEFAULT_MODEL", "typesafe-ai/jev")
        return
    if sys.platform != "win32":
        raise RuntimeError("Set TYPESAFE_API_KEY before starting the Jev MCP server")
    provider_file = ROOT / "config/provider.json"
    if not provider_file.is_file():
        raise RuntimeError("No Jev API key is configured. Run Settings.cmd and choose API key setup")
    provider = json.loads(provider_file.read_text(encoding="utf-8-sig"))["provider"]
    if provider not in {"vercel", "typesafe"}:
        raise ValueError("Unknown locally configured provider")
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / "scripts/Read-Key.ps1"),
            "-Provider",
            provider,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("The locally encrypted API key could not be read by this Windows account")
    key = result.stdout.strip()
    if len(key) < 20:
        raise RuntimeError("The configured API key is missing or invalid")
    os.environ["TYPESAFE_API_KEY"] = key
    if provider == "vercel":
        os.environ.setdefault("TYPESAFE_BASE_URL", "https://ai-gateway.vercel.sh/typesafe")
        os.environ.setdefault("TYPESAFE_DEFAULT_MODEL", "typesafe-ai/jev")
