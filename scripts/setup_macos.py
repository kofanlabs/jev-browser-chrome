"""Prepare local Mac files; never open Chrome or modify host configuration."""

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def save_key(provider, key):
    if provider not in {"vercel", "typesafe"} or len(key.strip()) < 20:
        raise ValueError("Invalid provider or key")
    folder = ROOT / "config"
    folder.mkdir(exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    path = folder / "macos-key.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump({"provider": provider, "key": key.strip()}, stream)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--set-key", choices=["vercel", "typesafe"])
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This setup is for macOS")
    if args.set_key:
        save_key(args.set_key, getpass.getpass("API key (hidden): "))
    sys.path.insert(0, str(ROOT))
    import extension_daemon

    extension_daemon.ensure_credentials()
    config = {
        "mcpServers": {
            "jev-browser-chrome": {"command": str(ROOT / ".venv/bin/python"), "args": [str(ROOT / "chrome_mcp.py")]}
        }
    }
    (ROOT / "mcp-config.json").write_text(json.dumps(config, indent=2) + "\n")
    print("Mac MCP configuration ready:", ROOT / "mcp-config.json")
    print("Load unpacked in chrome://extensions:", ROOT / "extension")
    print("Chrome was not opened; no host settings changed.")


if __name__ == "__main__":
    main()
