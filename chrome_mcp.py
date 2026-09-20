"""Local Jev Ultrafast MCP: attach to existing Chrome, never launch a browser.

The upstream policy/guard/act loop is retained. The host supplies free text.
"""

# ruff: noqa: E402, E501, I001
from __future__ import annotations

import atexit
import base64
import copy
import os
from pathlib import Path
import threading
import time
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
# A dedicated daemon namespace cannot attach to a cloud or isolated profile.
os.environ["BU_NAME"] = "jev-personal-chrome"
os.environ["BH_HOME"] = str(ROOT / "local-state")
os.environ["BH_RECORD"] = "0"
for name in ("BU_CDP_URL", "BU_CDP_WS", "BU_REMOTE_ID", "BU_BROWSER_ID", "BU_API_KEY"):
    os.environ[name] = ""

from extension_client import call as extension_call
from extension_client import ensure_daemon, status as extension_status
from jev_credentials import prepare_provider
from jev_ultrafast.agent import Agent
from jev_ultrafast.browser import StalePage
from jev_ultrafast.extension_browser import ExtensionBrowser
from jev_ultrafast.model import field_context
from mcp.server.mcpserver import MCPServer

server = MCPServer(
    "jev-browser-chrome",
    instructions=(
        "Use the user's existing Chrome only. Jev chooses actions. Host supplies requested text and independently "
        "verifies completion. No browser launch, profile copying or saved credentials are exposed."
    ),
)
lock = threading.RLock()
stopped = threading.Event()
responded = threading.Event()
state = {"status": "idle"}
worker = None
listed = {}
pending_reply = None


def origin(url):
    p = urlsplit(url)
    if p.scheme not in ("https", "http") or not p.hostname:
        raise ValueError("Only an observed HTTP(S) tab is supported")
    if p.username or p.password:
        raise ValueError("Credentials in URLs are not supported")
    return p.scheme + "://" + p.netloc


def connection_ready():
    return bool(extension_status().get("connected"))


def provider_setup():
    prepare_provider()
    base = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
    os.environ["JEV_SYSTEMONE_ENDPOINT"] = base + "/v1/systemone"
    os.environ["TYPESAFE_MODEL"] = os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest")


def public_state():
    with lock:
        return copy.deepcopy(state)


def publish(**values):
    with lock:
        state.update(values)


@server.tool()
def jev_browser_status() -> dict:
    """Read local run/connection status; does not start Chrome or read page content."""
    bridge = extension_status()
    return {
        **public_state(),
        "chromeConnected": bool(bridge.get("connected")),
        "browserMode": "existing-personal-chrome-extension",
        "extensionVersion": bridge.get("extensionVersion"),
    }


@server.tool()
def jev_browser_connect() -> dict:
    """Connect through the installed Jev Chrome extension; remote debugging is never used."""
    ensure_daemon()
    until = time.monotonic() + 3
    while time.monotonic() < until:
        if connection_ready():
            return {"connected": True, "mode": "existing-personal-chrome-extension"}
        time.sleep(0.1)
    return {
        "connected": False,
        "status": "needs_extension",
        "instruction": "Install or enable the local Jev Browser Bridge extension from this project's extension folder, then retry. Remote debugging is not required.",
    }


@server.tool()
def jev_browser_tabs() -> dict:
    """List existing normal tabs in the connected Chrome. Select an observed tab ID and URL; this does not create tabs."""
    global listed
    if not connection_ready():
        return {"status": "needs_extension", "tabs": []}
    tabs = extension_call("list_tabs")["tabs"]
    listed = {t["tabId"]: t for t in tabs}
    return {"status": "connected", "tabs": tabs}


def drive(goal, tab, allowed_origins, max_steps, max_seconds, act):
    global pending_reply
    agent = None
    started = time.monotonic()
    try:
        provider_setup()

        def check_page(page):
            if origin(page["url"]) not in allowed_origins:
                raise ValueError("Navigation left authorized origins; stopped before model request")

        browser = ExtensionBrowser(tab["url"], tab["tabId"])
        agent = Agent(tab["url"], goal, screenshots=False, page_guard=check_page, browser=browser)
        budget_end = started + max_seconds
        decisions = 0
        while (
            not stopped.is_set()
            and time.monotonic() < budget_end
            and len(agent.state["history"]) < max_steps
            and decisions < max_steps * 2
        ):
            if origin(agent.state["page"]["url"]) not in allowed_origins:
                publish(status="origin_blocked")
                break
            try:
                agent.command("predict")
                decisions += 1
                if stopped.is_set():
                    break
                page = agent.state["page"]
                if origin(page["url"]) not in allowed_origins:
                    publish(status="origin_blocked")
                    break
                decision = agent.state["decision"]
                publish(
                    lastDecision={
                        k: decision[k] for k in ("operation", "confidence", "target_confidence", "model", "latency_ms")
                    }
                )
                if not act:
                    publish(status="dry_run", proposedAction=decision["choice"])
                    break
                if decision["choice"] not in ("DONE", "BLOCKED"):
                    action = next(a for a in page["actions"] if a["id"] == decision["choice"])
                    if action["kind"] == "fill":
                        context = field_context(goal, action, page, agent.state["history"])
                        request_id = str(uuid.uuid4())
                        responded.clear()
                        pending_reply = None
                        publish(
                            status="needs_host",
                            request={
                                "id": request_id,
                                "kind": "text",
                                "context": context,
                                "requiredReply": {"text": "string"},
                            },
                        )
                        while not stopped.is_set() and time.monotonic() < budget_end and not responded.wait(0.2):
                            pass
                        if stopped.is_set() or pending_reply is None:
                            break
                        agent.pending_text = (
                            context,
                            pending_reply,
                            {"model": "host-agent", "latency_ms": 0, "usage": {}},
                        )
                        publish(status="running", request=None)
                if stopped.is_set() or time.monotonic() >= budget_end:
                    break
                # The upstream act method consumes the decision once and checks
                # target freshness again after a host text handoff.
                agent.command("act", {"fingerprint": page["fingerprint"]})
                publish(steps=len(agent.state["history"]), elapsedSeconds=round(time.monotonic() - started, 3))
                if agent.state["status"] in ("done", "blocked"):
                    publish(status="needs_verification" if agent.state["status"] == "done" else "blocked")
                    break
            except StalePage:
                agent.state["page"] = agent.browser.observe(screenshot=False)
                agent.state["decision"] = None
                agent.state["status"] = "ready"
                decisions += 1
        if stopped.is_set():
            publish(status="stopped")
        elif public_state()["status"] in ("running", "needs_host"):
            publish(status="budget_reached")
        page = agent.browser.observe(screenshot=False)
        # Single final verification artifact, not a background recording.
        if origin(page["url"]) in allowed_origins:
            final = {"url": page["url"], "title": page["title"], "text": page["text"][:12000]}
            try:
                screen = agent.browser.call("Page.captureScreenshot", format="png")["data"]
                proof = ROOT / "runs" / public_state()["runId"] / "final.png"
                proof.parent.mkdir(parents=True, exist_ok=True)
                proof.write_bytes(base64.b64decode(screen))
                final["screenshotPath"] = str(proof)
            except Exception as exc:
                # A missing optional screenshot permission must not erase a
                # successfully completed browser task and its DOM evidence.
                final["screenshotError"] = str(exc)[:300]
            publish(final=final, history=agent.state["history"])
        publish(request=None, elapsedSeconds=round(time.monotonic() - started, 3), goalVerified=False)
    except Exception as exc:
        # Provider code suppresses response bodies; never emit credentials.
        publish(status="error", error=str(exc)[:500], request=None, elapsedSeconds=round(time.monotonic() - started, 3))
    finally:
        if agent is not None:
            try:
                agent.close()  # Detaches; never closes the user's tab.
            except Exception:
                pass


@server.tool()
def jev_browser_run(
    goal: str,
    tab_id: str,
    expected_url: str,
    allowed_origins: list[str],
    act: bool = False,
    max_steps: int = 30,
    max_seconds: int = 180,
) -> dict:
    """Run Jev on an existing tab returned by tabs. act defaults false. Host must authorize task scope and handle consequential steps separately. No browser/profile creation. Text requests return needs_host; inspect final screenshot independently."""
    global worker, state
    with lock:
        if worker is not None and worker.is_alive():
            raise ValueError("A run already owns this connection")
        if not connection_ready():
            return {"status": "needs_browser_connection"}
        tab = listed.get(tab_id)
        if tab is None or tab["url"] != expected_url:
            raise ValueError("List and select the existing tab again")
        if not goal.strip() or len(goal) > 6000 or not 1 <= max_steps <= 60 or not 10 <= max_seconds <= 900:
            raise ValueError("Invalid goal or budget")
        if (
            not allowed_origins
            or origin(expected_url) not in allowed_origins
            or any(origin(o) != o for o in allowed_origins)
        ):
            raise ValueError("Exact authorized origins required")
        state = {"runId": str(uuid.uuid4()), "status": "running", "steps": 0, "goalVerified": False}
        stopped.clear()
        worker = threading.Thread(
            target=drive, args=(goal, dict(tab), list(allowed_origins), max_steps, max_seconds, act), daemon=True
        )
        worker.start()
        return public_state()


@server.tool()
def jev_browser_respond(request_id: str, text: str) -> dict:
    """Supply only the free text requested by needs_host. The existing live target is rechecked before typing."""
    global pending_reply
    with lock:
        request = state.get("request") or {}
        if state["status"] != "needs_host" or request.get("id") != request_id or responded.is_set():
            raise ValueError("Stale or already answered host request")
        if not isinstance(text, str) or not text or len(text) > 2000:
            raise ValueError("Expected nonempty text, at most 2000 characters")
        pending_reply = text
        responded.set()
        return {"accepted": True}


@server.tool()
def jev_browser_wait(seconds: float = 5) -> dict:
    """Wait at most 10 seconds for a host request or final state."""
    until = time.monotonic() + min(10, max(0, seconds))
    while time.monotonic() < until and public_state()["status"] == "running":
        time.sleep(0.1)
    return public_state()


@server.tool()
def jev_browser_stop() -> dict:
    """Stop at the next decision/input boundary. Does not close the user's tab."""
    stopped.set()
    responded.set()
    return {"stopRequested": True}


atexit.register(stopped.set)

if __name__ == "__main__":
    server.run()
