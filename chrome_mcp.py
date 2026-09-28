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
from jev_ultrafast.agent import Agent, RunControlReached
from jev_ultrafast.browser import StalePage
from jev_ultrafast.extension_browser import ExtensionBrowser, ObservationUnavailable
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
state_changed = threading.Condition(lock)
stopped = threading.Event()
responded = threading.Event()
state = {"status": "idle"}
worker = None
run_active = False
active_run_id = None
run_deadline = None
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
    with state_changed:
        state.update(values)
        state_changed.notify_all()


def bridge_failure(exc, *, mutation=False):
    """Return a host-readable diagnostic without retrying an uncertain RPC."""
    message = str(exc)[:500]
    lowered = message.lower()
    if "no current window" in lowered:
        return {
            "status": "needs_chrome_window",
            "chromeConnected": True,
            "usableWindow": False,
            "error": message,
            "instruction": "Open a normal Chrome window, then retry. Jev will not launch Chrome or create a profile.",
        }
    if isinstance(exc, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
        if not mutation:
            return {
                "status": "bridge_unavailable",
                "chromeConnected": False,
                "error": message,
                "instruction": "Reconnect the Jev Browser Bridge extension, then repeat the read.",
            }
        return {
            "status": "outcome_unknown",
            "chromeConnected": True,
            "error": message,
            "instruction": "List tabs and check whether the requested tab already opened before retrying.",
        }
    if "not connected" in lowered or "disconnected" in lowered:
        return {
            "status": "needs_browser_connection",
            "chromeConnected": False,
            "error": message,
            "instruction": "Reconnect the Jev Browser Bridge extension, then list tabs again.",
        }
    return {
        "status": "bridge_error",
        "chromeConnected": True,
        "error": message,
        "instruction": "Check the Jev Browser Bridge extension and retry only after verifying whether the operation took effect.",
    }


@server.tool()
def jev_browser_status() -> dict:
    """Read local run/connection status; does not start Chrome or read page content."""
    bridge = extension_status()
    result = {
        **public_state(),
        "chromeConnected": bool(bridge.get("connected")),
        "browserMode": "existing-personal-chrome-extension",
        "extensionVersion": bridge.get("extensionVersion"),
    }
    if not bridge.get("connected"):
        result.update(
            connectionStatus="needs_browser_connection",
            instruction="Connect the Jev Browser Bridge extension in an existing Chrome window.",
        )
    else:
        result["connectionStatus"] = "connected"
    return result


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
def jev_browser_open_tab(url: str, active: bool = False) -> dict:
    """Open a task-related HTTP(S) URL in a new tab in the connected personal Chrome."""
    origin(url)
    with lock:
        if run_active or (worker is not None and worker.is_alive()):
            raise ValueError("Stop or finish the current run before opening a tab")
        bridge = extension_status()
        if not bridge.get("connected"):
            return {
                "status": "needs_browser_connection",
                "chromeConnected": False,
                "instruction": "Reconnect the Jev Browser Bridge extension, then retry. Jev will not launch Chrome.",
            }
        try:
            version = tuple(int(part) for part in bridge.get("extensionVersion", "").split("."))
        except (ValueError, AttributeError):
            version = ()
        if version < (1, 0, 2):
            return {
                "status": "needs_extension_update",
                "minimumVersion": "1.0.2",
                "instruction": "Update and reload Jev Browser Bridge to version 1.0.2 or newer, then retry.",
            }
        # Never retry this mutation: a timeout can mean the tab was already created.
        try:
            tab = extension_call("create_tab", {"url": url, "active": active})
        except Exception as exc:
            # Never repeat tab creation: a lost response can follow a successful create.
            return bridge_failure(exc, mutation=True)
        listed[tab["tabId"]] = tab
        return {"status": "opened", "tab": tab}


@server.tool()
def jev_browser_tabs() -> dict:
    """List existing normal tabs in the connected Chrome. Select an observed tab ID and URL; this does not create tabs."""
    global listed
    if not connection_ready():
        return {
            "status": "needs_browser_connection",
            "chromeConnected": False,
            "usableWindow": False,
            "tabs": [],
            "instruction": "Connect the Jev Browser Bridge extension in an existing Chrome window.",
        }
    try:
        tabs = extension_call("list_tabs")["tabs"]
    except Exception as exc:
        return {**bridge_failure(exc), "tabs": []}
    with lock:
        listed = {t["tabId"]: t for t in tabs}
    if not tabs:
        return {
            "status": "connected_no_accessible_http_tabs",
            "chromeConnected": True,
            "windowAvailability": "unknown",
            "tabs": [],
            "instruction": "No accessible HTTP(S) tabs were found. A normal Chrome window may still be open on a new-tab page; open a web tab, then list tabs again.",
        }
    return {"status": "connected", "chromeConnected": True, "usableWindow": True, "tabs": tabs}


def check_run_control(deadline):
    if stopped.is_set():
        raise RunControlReached("stopped")
    if time.monotonic() >= deadline:
        raise RunControlReached("budget_reached")


def reobserve(browser, deadline):
    """Retry only bounded reads after navigation; this never repeats an action."""
    for attempt in range(3):
        check_run_control(deadline)
        try:
            return browser.observe(screenshot=False)
        except (ObservationUnavailable, StalePage) as exc:
            if (isinstance(exc, ObservationUnavailable) and not exc.retryable) or attempt == 2:
                raise
            if stopped.wait(min(0.04 * (attempt + 1), max(0, deadline - time.monotonic()))):
                raise RunControlReached("stopped") from None


def verify_unsettled(browser, deadline):
    """Give a delayed UI one short read-only settle window; the action is never replayed."""
    remaining = deadline - time.monotonic()
    if remaining > 0 and stopped.wait(min(0.8, remaining)):
        return None, "stopped while waiting for the executed action to settle"
    if time.monotonic() >= deadline:
        return None, "deadline reached while waiting for the executed action to settle"
    try:
        return browser.observe(screenshot=False), None
    except Exception as exc:
        return None, str(exc)[:300]


def wait_for_host_text(context, deadline):
    global pending_reply
    request_id = str(uuid.uuid4())
    with state_changed:
        pending_reply = None
        responded.clear()
        state.update(
            status="needs_host",
            request={
                "id": request_id,
                "kind": "text",
                "context": context,
                "requiredReply": {"text": "string"},
            },
        )
        state_changed.notify_all()

    responded.wait(max(0, deadline - time.monotonic()))
    with state_changed:
        reply = pending_reply
        pending_reply = None
        if (state.get("request") or {}).get("id") == request_id:
            state.update(status="running", request=None)
        responded.clear()
        state_changed.notify_all()
        if stopped.is_set() or time.monotonic() >= deadline:
            return None
        return reply


def validate_text_inputs(text_inputs, allowed_origins):
    """Copy caller data into private exact-match descriptors; never echo values."""
    if text_inputs is None:
        return {}
    if not isinstance(text_inputs, list) or len(text_inputs) > 16:
        raise ValueError("text_inputs must be a list of at most 16 entries")
    prepared = {}
    for item in text_inputs:
        if not isinstance(item, dict) or set(item) != {"page_url", "field_label", "field_role", "text"}:
            raise ValueError("Each text_inputs entry requires exactly page_url, field_label, field_role, text")
        if any(not isinstance(value, str) for value in item.values()):
            raise ValueError("All text_inputs fields must be strings")
        url, label, role, text = (item[key] for key in ("page_url", "field_label", "field_role", "text"))
        if not 1 <= len(url) <= 4096 or any(ord(char) <= 32 or ord(char) == 127 for char in url):
            raise ValueError("Invalid text_inputs page_url")
        try:
            urlsplit(url).port  # Validate a supplied port as well as scheme and credentials.
            valid_origin = origin(url) in allowed_origins
        except ValueError:
            raise ValueError("Invalid text_inputs page_url") from None
        if not valid_origin:
            raise ValueError("text_inputs page_url must belong to an allowed origin")
        if not 1 <= len(label) <= 512 or role not in {"textbox", "searchbox", "combobox", "spinbutton"}:
            raise ValueError("Invalid text_inputs field_label or field_role")
        if not 1 <= len(text) <= 2000:
            raise ValueError("text_inputs text must contain 1 to 2000 characters")
        descriptor = (url, label, role)
        if descriptor in prepared:
            raise ValueError("Duplicate text_inputs field descriptor")
        prepared[descriptor] = text
    return prepared


def take_prepared_text(prepared, page, action):
    if type(page.get("omitted_actions")) is not int or page["omitted_actions"] != 0 or action.get("kind") != "fill":
        return None
    if not isinstance(action.get("label"), str) or not isinstance(action.get("role"), str):
        return None
    descriptor = (page["url"], action.get("label"), action.get("role"))
    if descriptor not in prepared:
        return None
    nodes = set()
    for candidate in page["actions"]:
        if candidate.get("kind") == "fill" and (
            candidate.get("label"), candidate.get("role")
        ) == descriptor[1:]:
            node = candidate.get("node")
            if type(node) is not int or node <= 0:
                return None
            nodes.add(node)
    if len(nodes) != 1 or type(action.get("node")) is not int or action["node"] not in nodes:
        return None
    # Consume before input: stale rejection or uncertain execution never restores it.
    return prepared.pop(descriptor)


def drive(
    goal,
    tab,
    allowed_origins,
    max_steps,
    max_seconds,
    act,
    capture_final=False,
    deadline=None,
    run_id=None,
    prepared_text=None,
):
    global run_active, active_run_id
    goal = goal.strip()  # Match Agent's goal when constructing supplied-text contexts.
    agent = None
    completion_status = None
    started = time.monotonic()
    budget_end = deadline if deadline is not None else started + max_seconds
    decisions = 0
    decision_count = 0
    unsettled_action = False
    prediction_failed = False
    prepared = dict(prepared_text or {})
    prepared_hits = 0
    host_requests = 0
    # Non-overlapping wall-clock phases, accumulated before rounding. Prediction
    # includes Agent's freshness check; browser includes startup, act+observation,
    # recovery/settlement waits, final reads and capture RPCs. Setup/close/file I/O
    # outside those calls are excluded. Never add reported model latency again.
    timing_ns = {"modelMs": 0, "hostWaitMs": 0, "browserMs": 0}

    def timing_fields():
        return {key: round(value / 1_000_000, 3) for key, value in timing_ns.items()}

    def timed(key, function, *args, **kwargs):
        phase_started = time.perf_counter_ns()
        try:
            return function(*args, **kwargs)
        finally:
            timing_ns[key] += time.perf_counter_ns() - phase_started
            publish(**timing_fields())

    publish(**timing_fields(), preparedTextHits=0, hostTextRequests=0)
    if run_id is None:
        run_id = public_state().get("runId")
    try:
        check_run_control(budget_end)
        provider_setup()
        check_run_control(budget_end)

        def check_page(page):
            if origin(page["url"]) not in allowed_origins:
                raise ValueError("Navigation left authorized origins; stopped before model request")

        browser = timed("browserMs", ExtensionBrowser, tab["url"], tab["tabId"], deadline=budget_end)
        check_run_control(budget_end)
        agent = timed(
            "browserMs",
            Agent,
            tab["url"],
            goal,
            screenshots=False,
            page_guard=check_page,
            browser=browser,
            run_guard=lambda: check_run_control(budget_end),
        )
        while True:
            if stopped.is_set():
                completion_status = "stopped"
                break
            if time.monotonic() >= budget_end:
                completion_status = "budget_reached"
                break
            if len(agent.state["history"]) >= max_steps or decisions >= max_steps * 2:
                completion_status = "budget_reached"
                break
            if origin(agent.state["page"]["url"]) not in allowed_origins:
                completion_status = "origin_blocked"
                break
            history_before = len(agent.state["history"])
            prepared_for_action = False
            try:
                prediction_failed = False
                try:
                    timed("modelMs", agent.command, "predict")
                except Exception:
                    prediction_failed = True
                    raise
                decisions += 1
                decision_count += 1
                publish(decisionCount=decision_count)
                if stopped.is_set():
                    completion_status = "stopped"
                    break
                if time.monotonic() >= budget_end:
                    completion_status = "budget_reached"
                    break
                page = agent.state["page"]
                if origin(page["url"]) not in allowed_origins:
                    completion_status = "origin_blocked"
                    break
                decision = agent.state["decision"]
                publish(
                    lastDecision={
                        k: decision[k] for k in ("operation", "confidence", "target_confidence", "model", "latency_ms")
                    },
                    decisionCount=decision_count,
                )
                if not act:
                    publish(proposedAction=decision["choice"])
                    completion_status = "dry_run"
                    break
                if decision["choice"] not in ("DONE", "BLOCKED"):
                    action = next(a for a in page["actions"] if a["id"] == decision["choice"])
                    if action["kind"] == "fill":
                        context = field_context(goal, action, page, agent.state["history"])
                        check_run_control(budget_end)
                        host_text = (
                            take_prepared_text(prepared, page, action)
                            if decision["operation"] == "TYPE_TEXT" else None
                        )
                        if host_text is None:
                            host_requests += 1
                            publish(hostTextRequests=host_requests)
                            host_text = timed("hostWaitMs", wait_for_host_text, context, budget_end)
                        else:
                            prepared_for_action = True
                            prepared_hits += 1
                            publish(preparedTextHits=prepared_hits)
                        if stopped.is_set():
                            completion_status = "stopped"
                            break
                        if time.monotonic() >= budget_end:
                            completion_status = "budget_reached"
                            break
                        if host_text is None:
                            break
                        agent.pending_text = (
                            context,
                            host_text,
                            {"model": "host-prepared" if prepared_for_action else "host-agent", "latency_ms": 0, "usage": {}},
                        )
                        publish(status="running", request=None)
                if stopped.is_set():
                    completion_status = "stopped"
                    break
                if time.monotonic() >= budget_end:
                    completion_status = "budget_reached"
                    break
                # The upstream act method consumes the decision once and checks
                # target freshness again after a host text handoff.
                timed("browserMs", agent.command, "act", {"fingerprint": page["fingerprint"]})
                publish(steps=len(agent.state["history"]), elapsedSeconds=round(time.monotonic() - started, 3))
                if len(agent.state["history"]) > history_before:
                    receipt = agent.state["history"][-1].get("receipt") or {}
                    if receipt.get("settled") is False:
                        unsettled_action = True
                        observed, read_error = timed("browserMs", verify_unsettled, agent.browser, budget_end)
                        reason = (
                            f"Action executed but settlement is unconfirmed ({receipt.get('settlement')}). "
                            f"{read_error or 'A bounded read-only wait and observation completed.'} "
                            "The action was not replayed."
                        )
                        if observed is not None:
                            agent.state["page"] = observed
                            if origin(observed["url"]) in allowed_origins:
                                publish(
                                    final={
                                        "url": observed["url"],
                                        "title": observed["title"],
                                        "text": observed["text"][:12000],
                                    }
                                )
                        completion_status = (
                            "stopped" if stopped.is_set() else "budget_reached" if time.monotonic() >= budget_end
                            else "needs_verification"
                        )
                        publish(
                            verificationReason=reason,
                            steps=len(agent.state["history"]),
                            history=agent.state["history"],
                        )
                        break
                if agent.state["status"] in ("done", "blocked"):
                    completion_status = "needs_verification" if agent.state["status"] == "done" else "blocked"
                    break
            except RunControlReached as exc:
                completion_status = exc.status
                break
            except (StalePage, ObservationUnavailable) as exc:
                if prepared_for_action:
                    agent.pending_text = None
                if len(agent.state["history"]) > history_before:
                    # A receipt proves the mutation ran. Re-observe only; never
                    # issue another action after its result became unavailable.
                    unsettled_action = True
                    publish(steps=len(agent.state["history"]), history=agent.state["history"])
                    receipt = agent.state["history"][-1].get("receipt") or {}
                    read_error = str(exc)[:300]
                    observed = None
                    if receipt.get("settled") is False:
                        if not isinstance(exc, ObservationUnavailable) or exc.retryable:
                            observed, read_error = timed("browserMs", verify_unsettled, agent.browser, budget_end)
                    elif not isinstance(exc, ObservationUnavailable) or exc.retryable:
                        try:
                            observed = timed("browserMs", reobserve, agent.browser, budget_end)
                            read_error = "A read-only observation recovered the current page."
                        except Exception as recovery_error:
                            read_error = str(recovery_error)[:300]
                    if observed is not None:
                        agent.state["page"] = observed
                        if origin(observed["url"]) in allowed_origins:
                            publish(
                                final={
                                    "url": observed["url"],
                                    "title": observed["title"],
                                    "text": observed["text"][:12000],
                                }
                            )
                    completion_status = (
                        "stopped" if stopped.is_set() else "budget_reached" if time.monotonic() >= budget_end
                        else "needs_verification"
                    )
                    publish(
                        verificationReason=(
                            f"Action executed but its result needs verification ({receipt.get('settlement', 'observation unavailable')}). "
                            f"{read_error} The action was not replayed."
                        ),
                        steps=len(agent.state["history"]),
                        history=agent.state["history"],
                    )
                    break
                if isinstance(exc, ObservationUnavailable) and not exc.retryable:
                    raise
                agent.state["page"] = timed("browserMs", reobserve, agent.browser, budget_end)
                agent.state["decision"] = None
                agent.state["status"] = "ready"
                if isinstance(exc, StalePage):
                    decisions += 1
        if stopped.is_set():
            completion_status = "stopped"
        elif completion_status is None:
            completion_status = "budget_reached"
        if not unsettled_action and not stopped.is_set() and time.monotonic() < budget_end:
            page = timed("browserMs", reobserve, agent.browser, budget_end)
            # Single final verification artifact, not a background recording.
            if origin(page["url"]) in allowed_origins:
                final = {"url": page["url"], "title": page["title"], "text": page["text"][:12000]}
                if capture_final and time.monotonic() < budget_end:
                    try:
                        screen = timed("browserMs", agent.browser.call, "Page.captureScreenshot", format="png")["data"]
                        proof = ROOT / "runs" / run_id / "final.png"
                        proof.parent.mkdir(parents=True, exist_ok=True)
                        proof.write_bytes(base64.b64decode(screen))
                        final["screenshotPath"] = str(proof)
                    except Exception as exc:
                        # Optional capture errors do not erase DOM evidence.
                        final["screenshotError"] = str(exc)[:300]
                publish(final=final, history=agent.state["history"])
            else:
                completion_status = "origin_blocked"
        publish(
            status=completion_status,
            request=None,
            elapsedSeconds=round(time.monotonic() - started, 3),
            goalVerified=False,
            decisionCount=decision_count,
            **timing_fields(),
        )
    except RunControlReached as exc:
        publish(
            status=exc.status,
            request=None,
            elapsedSeconds=round(time.monotonic() - started, 3),
            steps=len(agent.state["history"]) if agent is not None else 0,
            history=agent.state["history"] if agent is not None else [],
            goalVerified=False,
            decisionCount=decision_count,
            **timing_fields(),
        )
    except Exception as exc:
        if prediction_failed and agent is not None:
            # A failed DONE prediction must not hide a page reached by earlier
            # actions. One read at most, capped by both 1s and the run deadline.
            try:
                evidence_page = agent.state["page"]
                if origin(evidence_page["url"]) in allowed_origins:
                    freshness = "cached"
                    reason = "run stopped" if stopped.is_set() else "run deadline exhausted"
                    if not stopped.is_set() and time.monotonic() < budget_end:
                        browser = agent.browser
                        previous_deadline = getattr(browser, "deadline", None)
                        browser.deadline = min(budget_end, time.monotonic() + 1.0)
                        if previous_deadline is not None:
                            browser.deadline = min(browser.deadline, previous_deadline)
                        try:
                            evidence_page = timed("browserMs", browser.observe, screenshot=False)
                            freshness = "fresh"
                        except Exception:
                            reason = "read-only final observation failed"
                        finally:
                            browser.deadline = previous_deadline
                    if origin(evidence_page["url"]) in allowed_origins:
                        evidence = {
                            "url": evidence_page["url"],
                            "title": evidence_page["title"],
                            "text": evidence_page["text"][:12000],
                            "freshness": freshness,
                        }
                        if freshness == "cached":
                            evidence["reason"] = reason
                        publish(final=evidence)
                    else:
                        publish(finalEvidenceError="Final observation left authorized origins")
            except Exception:
                # Evidence collection must never replace the original error.
                publish(finalEvidenceError="Allowed-origin final evidence unavailable")
        # Provider errors are sanitized by the model adapter. Keep executed
        # history visible if a later observation or bridge operation failed.
        publish(
            status="error",
            error=str(exc)[:500],
            request=None,
            elapsedSeconds=round(time.monotonic() - started, 3),
            steps=len(agent.state["history"]) if agent is not None else 0,
            history=agent.state["history"] if agent is not None else [],
            goalVerified=False,
            decisionCount=decision_count,
            **timing_fields(),
        )
    finally:
        try:
            if agent is not None:
                try:
                    agent.close()  # Detaches; never closes the user's tab.
                except Exception:
                    pass
        finally:
            with state_changed:
                if active_run_id == run_id:
                    run_active = False
                    active_run_id = None
                state_changed.notify_all()


@server.tool()
def jev_browser_run(
    goal: str,
    tab_id: str,
    expected_url: str,
    allowed_origins: list[str],
    act: bool = False,
    max_steps: int = 30,
    max_seconds: int = 180,
    capture_final: bool = False,
    text_inputs: list[dict] | None = None,
) -> dict:
    """Run Jev on an existing tab returned by tabs. act defaults false. Host must authorize task scope and handle consequential steps separately. No browser/profile creation. Text requests return needs_host; verify final DOM evidence independently. capture_final=true activates the target tab for a screenshot.

    Cumulative wall times update at phase boundaries: modelMs measures the whole
    prediction command, including predecision freshness reads and page guards,
    not pure provider HTTP time; per-decision latency_ms remains model-adapter
    latency. hostWaitMs measures the text handoff. browserMs measures browser/
    agent startup, act commands with their observations, recovery and settlement
    waits/reads, final observation and optional capture RPCs. These phases do not
    overlap; provider setup, external cleanup, file I/O and other bookkeeping are
    excluded, so their sum need not equal elapsedSeconds * 1000.

    Optional text_inputs supplies exact authorized caller data, never actions.
    At most 16 entries, each with exactly page_url (HTTP(S), allowed origin,
    1..4096 characters), field_label (1..512), field_role (textbox, searchbox,
    combobox or spinbutton), and text (1..2000). Duplicate descriptors are rejected.
    Only after Jev selects TYPE_TEXT, match the exact observed URL/label/role and
    one distinct visible fill node. Misses, ambiguity, missing node/role or a
    incomplete snapshot (omitted_actions missing, invalid or nonzero) use needs_host. Entries are
    consumed once per run before the action attempt, including stale rejections.
    Unused entries are private. preparedTextHits counts consumed entries, not
    successful fills; hostTextRequests counts host handoffs. None preserves the
    usual host handoff. If a loaded tool schema lacks text_inputs, omit it and
    continue through needs_host and jev_browser_respond.
    """
    global worker, state, run_active, active_run_id, run_deadline, pending_reply
    prepared = validate_text_inputs(text_inputs, allowed_origins)
    with lock:
        if run_active or (worker is not None and worker.is_alive()):
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
        run_id = str(uuid.uuid4())
        run_deadline = time.monotonic() + max_seconds
        state = {
            "runId": run_id,
            "status": "running",
            "steps": 0,
            "decisionCount": 0,
            "preparedTextHits": 0,
            "hostTextRequests": 0,
            "modelMs": 0,
            "hostWaitMs": 0,
            "browserMs": 0,
            "goalVerified": False,
        }
        active_run_id = run_id
        run_active = True
        stopped.clear()
        responded.clear()
        pending_reply = None
        worker = threading.Thread(
            target=drive,
            args=(
                goal,
                dict(tab),
                list(allowed_origins),
                max_steps,
                max_seconds,
                act,
                capture_final,
                run_deadline,
                run_id,
                prepared,
            ),
            daemon=True,
        )
        try:
            worker.start()
        except Exception as exc:
            run_active = False
            active_run_id = None
            state.update(status="error", error=str(exc)[:500])
            state_changed.notify_all()
            raise
        state_changed.notify_all()
        return public_state()


@server.tool()
def jev_browser_respond(request_id: str, text: str) -> dict:
    """Supply only the free text requested by needs_host. The existing live target is rechecked before typing."""
    global pending_reply
    with state_changed:
        request = state.get("request") or {}
        if (
            state["status"] != "needs_host"
            or request.get("id") != request_id
            or responded.is_set()
            or stopped.is_set()
            or (run_deadline is not None and time.monotonic() >= run_deadline)
        ):
            raise ValueError("Stale or already answered host request")
        if not isinstance(text, str) or not text or len(text) > 2000:
            raise ValueError("Expected nonempty text, at most 2000 characters")
        pending_reply = text
        # A host may poll again immediately after an accepted reply. Clear the
        # request synchronously so it cannot submit the same handoff twice while
        # the worker thread is waking up.
        state.update(status="running", request=None)
        responded.set()
        state_changed.notify_all()
        return {"accepted": True}


@server.tool()
def jev_browser_wait(seconds: float = 5) -> dict:
    """Wait at most 10 seconds for a host request or final state."""
    timeout = min(10, max(0, seconds))
    with state_changed:
        state_changed.wait_for(lambda: state.get("status") != "running" or stopped.is_set(), timeout=timeout)
        return copy.deepcopy(state)


@server.tool()
def jev_browser_stop() -> dict:
    """Request a cooperative stop. In-flight model or bridge calls may finish first; no later decision starts."""
    with state_changed:
        stopped.set()
        responded.set()
        if run_active:
            state["stopRequested"] = True
            state["request"] = None
            if state.get("status") == "needs_host":
                state["status"] = "running"
        state_changed.notify_all()
    return {"stopRequested": True}


atexit.register(stopped.set)

if __name__ == "__main__":
    server.run()
