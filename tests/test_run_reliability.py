import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import chrome_mcp
from jev_ultrafast.agent import Agent
from jev_ultrafast.browser import StalePage
from jev_ultrafast.extension_browser import ExtensionBrowser, ObservationUnavailable


@pytest.mark.parametrize(
    "scenario, status, model_ms, host_ms, browser_ms",
    [
        ("success", "needs_verification", 2520, 300, 380),
        ("setup_error", "error", 0, 0, 0),
        ("model_error", "error", 110, 0, 120),
        ("later_model_error", "error", 2520, 300, 370),
        ("near_budget_model_error", "error", 2520, 300, 370),
        ("budget_later_model_error", "error", 2520, 300, 320),
        ("stop_later_model_error", "error", 2520, 300, 320),
        ("evidence_read_error", "error", 2520, 300, 370),
        ("evidence_disallowed", "error", 2520, 300, 370),
        ("act_error", "error", 110, 300, 270),
        ("post_observation_error", "needs_verification", 110, 300, 370),
        ("stale", "needs_verification", 2520, 300, 380),
        ("unsettled", "needs_verification", 110, 300, 1170),
        ("budget_prediction", "budget_reached", 110, 0, 70),
        ("budget_host", "budget_reached", 110, 300, 70),
        ("stop_host", "stopped", 110, 300, 70),
        ("final_error", "error", 2520, 300, 380),
        ("capture_error", "needs_verification", 2520, 300, 460),
    ],
)
def test_drive_cumulative_wall_timings(monkeypatch, scenario, status, model_ms, host_ms, browser_ms):
    # Each fake advances one shared clock: exact totals catch both missing phases
    # and double-counting nested observations or the provider's latency metadata.
    now_ns = 0

    def advance(milliseconds):
        nonlocal now_ns
        now_ns += milliseconds * 1_000_000

    clock = SimpleNamespace(monotonic=lambda: now_ns / 1_000_000_000, perf_counter_ns=lambda: now_ns)
    monkeypatch.setattr(chrome_mcp, "time", clock)
    deadline = {
        "budget_prediction": 4.1, "budget_host": 4.3,
        "budget_later_model_error": 5, "near_budget_model_error": 7.6,
    }.get(scenario, 100)
    monkeypatch.setattr(chrome_mcp, "run_deadline", deadline)
    chrome_mcp.state = {"runId": "timing-test", "status": "running", "modelMs": 999, "browserMs": 999}
    terminal_updates = []
    browser_reads = []
    late_prediction_failures = {
        "later_model_error", "near_budget_model_error", "budget_later_model_error", "stop_later_model_error",
        "evidence_read_error", "evidence_disallowed",
    }
    original_publish = chrome_mcp.publish

    def publish(**values):
        if values.get("status") in {"needs_verification", "error", "budget_reached", "stopped"}:
            terminal_updates.append(dict(values))
        original_publish(**values)

    def setup():
        advance(4000)  # Provider setup is outside the measured phases.
        if scenario == "setup_error":
            raise RuntimeError("setup failed")

    def host_reply(_timeout):
        advance(300)
        if scenario == "stop_host":
            chrome_mcp.jev_browser_stop()
        elif clock.monotonic() < deadline:
            chrome_mcp.jev_browser_respond(chrome_mcp.state["request"]["id"], "query")
        return chrome_mcp.responded.is_set()

    def settle_wait(seconds):
        advance(round(seconds * 1000))
        return chrome_mcp.stopped.is_set()

    class FakeBrowser:
        def __init__(self, _url, _tab_id, deadline=None):
            advance(20)
            self.deadline = deadline
            self.reads = 0
            self.current_page = page()

        def fresh(self, _page):
            advance(10)

        def observe(self, screenshot=False):
            self.reads += 1
            browser_reads.append(self.reads)
            if (scenario in late_prediction_failures and self.reads == 3) or (
                scenario == "model_error" and self.reads == 2
            ):
                assert self.deadline == min(deadline, clock.monotonic() + 1)
            advance(50)
            if scenario == "post_observation_error" and self.reads == 2:
                raise ObservationUnavailable("navigation interrupted observation", retryable=True)
            if scenario == "final_error" and self.reads == 3:
                raise RuntimeError("final read failed")
            if scenario == "evidence_read_error" and self.reads == 3:
                raise ObservationUnavailable("read timed out", retryable=True)
            if scenario == "evidence_disallowed" and self.reads == 3:
                return {**self.current_page, "url": "https://outside.example/"}
            return dict(self.current_page)

        def act(self):
            advance(200)
            if scenario == "act_error":
                raise TimeoutError("uncertain action")
            if scenario == "stale":
                raise StalePage("rejected before input")
            self.current_page = {
                **self.current_page,
                "url": "https://example.com/Ada_Lovelace",
                "title": "Ada Lovelace",
                "text": "Article reached by the confirmed action",
            }

        def call(self, method, **_params):
            assert method == "Page.captureScreenshot"
            advance(80)
            raise RuntimeError("capture failed")

    class FakeAgent:
        def __init__(self, _url, _goal, *, browser, **_kwargs):
            self.browser = browser
            self.predictions = 0
            self.state = {"page": browser.observe(), "history": [], "status": "ready", "decision": None}

        def command(self, name, _body=None):
            if name == "predict":
                self.predictions += 1
                self.browser.fresh(self.state["page"])
                advance(100 if self.predictions == 1 else 2400)
                if scenario == "model_error" or (scenario in late_prediction_failures and self.predictions == 2):
                    if scenario == "stop_later_model_error":
                        chrome_mcp.jev_browser_stop()
                    raise RuntimeError("provider failed")
                self.state["decision"] = decision() if self.predictions == 1 else decision("DONE", "DONE")
                self.state["decision"]["latency_ms"] = 999_999  # Never used for wall-time accounting.
            elif name == "act" and self.state["decision"]["choice"] == "DONE":
                self.browser.fresh(self.state["page"])
                self.state["status"] = "done"
            elif name == "act":
                assert self.pending_text[1] == "query"
                self.browser.act()
                receipt = {"executed": "e1", "settled": scenario != "unsettled", "settlement": "no-visible-change"}
                self.state["history"].append({"receipt": receipt, "latency_ms": 999_999})
                self.state["page"] = self.browser.observe()

        def close(self):
            assert self.browser.deadline == deadline
            advance(5000)  # Cleanup is also excluded from the published phase totals.

    monkeypatch.setattr(chrome_mcp, "publish", publish)
    monkeypatch.setattr(chrome_mcp, "provider_setup", setup)
    monkeypatch.setattr(chrome_mcp, "ExtensionBrowser", FakeBrowser)
    monkeypatch.setattr(chrome_mcp, "Agent", FakeAgent)
    monkeypatch.setattr(chrome_mcp.responded, "wait", host_reply)
    monkeypatch.setattr(chrome_mcp.stopped, "wait", settle_wait)
    chrome_mcp.drive(
        "Search", {"url": page()["url"], "tabId": "12"}, ["https://example.com"], 3, 100, True,
        deadline=deadline, capture_final=scenario == "capture_error",
    )

    expected = {"modelMs": model_ms, "hostWaitMs": host_ms, "browserMs": browser_ms}
    result = chrome_mcp.public_state()
    assert result["status"] == status
    assert {key: result[key] for key in expected} == expected
    assert {key: terminal_updates[-1][key] for key in expected} == expected
    assert result["goalVerified"] is False
    if scenario in late_prediction_failures:
        assert result["error"] == "provider failed"
        assert result["steps"] == 1
        if scenario == "evidence_disallowed":
            assert "final" not in result
            assert "authorized origins" in result["finalEvidenceError"]
        else:
            assert result["final"]["url"] == "https://example.com/Ada_Lovelace"
            assert result["final"]["title"] == "Ada Lovelace"
            assert result["final"]["text"] == "Article reached by the confirmed action"
            cached = scenario in {"budget_later_model_error", "stop_later_model_error", "evidence_read_error"}
            assert result["final"]["freshness"] == ("cached" if cached else "fresh")
        assert len(browser_reads) == (2 if scenario in {"budget_later_model_error", "stop_later_model_error"} else 3)


@pytest.fixture(autouse=True)
def reset_run_state(monkeypatch):
    monkeypatch.setattr(chrome_mcp, "state", {"status": "idle"})
    monkeypatch.setattr(chrome_mcp, "worker", None)
    monkeypatch.setattr(chrome_mcp, "run_active", False)
    monkeypatch.setattr(chrome_mcp, "active_run_id", None)
    monkeypatch.setattr(chrome_mcp, "run_deadline", None)
    monkeypatch.setattr(chrome_mcp, "pending_reply", None)
    monkeypatch.setattr(chrome_mcp, "listed", {})
    chrome_mcp.stopped.clear()
    chrome_mcp.responded.clear()
    yield
    chrome_mcp.stopped.clear()
    chrome_mcp.responded.clear()


def page():
    return {
        "url": "https://example.com/",
        "title": "Example",
        "text": "Ready",
        "fingerprint": "before",
        "actions": [
            {"id": "e1", "kind": "fill", "node": 7, "label": "Search", "role": "textbox", "value": ""}
        ],
    }


def click_page():
    result = page()
    result["actions"][0].update(kind="click", label="Search", role="button")
    return result


def decision(choice="e1", operation="TYPE_TEXT"):
    return {
        "choice": choice,
        "operation": operation,
        "confidence": 0.9,
        "target_confidence": 0.9,
        "model": "offline-fake",
        "latency_ms": 1,
    }


def install_fake_runtime(monkeypatch, fake_agent, fake_browser=None):
    class FakeBrowser:
        def __init__(self, _url, _tab_id, deadline=None):
            self.deadline = deadline
            self.page = page()
            self.observe_calls = 0

        def observe(self, screenshot=False):
            self.observe_calls += 1
            return dict(self.page)

        def close(self):
            return None

    monkeypatch.setattr(chrome_mcp, "provider_setup", lambda: None)
    monkeypatch.setattr(chrome_mcp, "ExtensionBrowser", fake_browser or FakeBrowser)
    monkeypatch.setattr(chrome_mcp, "Agent", fake_agent)
    return fake_browser or FakeBrowser


def test_host_reply_before_worker_wait_is_received_once():
    real_event = chrome_mcp.responded

    class ReplyBeforeWait:
        def clear(self):
            real_event.clear()

        def set(self):
            real_event.set()

        def is_set(self):
            return real_event.is_set()

        def wait(self, timeout=None):
            request = chrome_mcp.public_state().get("request")
            if request:
                chrome_mcp.jev_browser_respond(request["id"], "text supplied in the race window")
            return real_event.wait(timeout)

    chrome_mcp.run_deadline = time.monotonic() + 2
    with patch.object(chrome_mcp, "responded", ReplyBeforeWait()):
        text = chrome_mcp.wait_for_host_text({"field": "Search"}, chrome_mcp.run_deadline)
    assert text == "text supplied in the race window"
    assert chrome_mcp.public_state()["request"] is None
    with pytest.raises(ValueError, match="Stale or already answered"):
        chrome_mcp.jev_browser_respond("old-id", "duplicate")


def test_wait_uses_state_change_notification_without_public_state_polling(monkeypatch):
    chrome_mcp.state = {"status": "running"}
    monkeypatch.setattr(chrome_mcp, "public_state", lambda: pytest.fail("wait must not poll deep-copied state"))
    result = []
    started = threading.Event()

    def wait_for_update():
        started.set()
        result.append(chrome_mcp.jev_browser_wait(seconds=2))

    thread = threading.Thread(target=wait_for_update)
    thread.start()
    assert started.wait(timeout=1)
    chrome_mcp.publish(status="needs_host", request={"id": "wait-test"})
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert result[0]["status"] == "needs_host"


def test_stop_wakes_host_handoff_without_acting(monkeypatch):
    class FakeAgent:
        instance = None

        def __init__(self, _url, _goal, *, screenshots, page_guard, browser, run_guard=None):
            self.browser = browser
            self.act_calls = 0
            self.state = {"page": page(), "history": [], "status": "ready", "decision": None}
            FakeAgent.instance = self

        def command(self, name, _body=None):
            if name == "predict":
                self.state["decision"] = decision()
            elif name == "act":
                self.act_calls += 1

        def close(self):
            return None

    install_fake_runtime(monkeypatch, FakeAgent)
    chrome_mcp.state = {"runId": "stop-test", "status": "running"}
    thread = threading.Thread(
        target=chrome_mcp.drive,
        args=("Search", {"url": page()["url"], "tabId": "12"}, ["https://example.com"], 3, 10, True),
    )
    thread.start()
    with chrome_mcp.state_changed:
        assert chrome_mcp.state_changed.wait_for(lambda: chrome_mcp.state.get("status") == "needs_host", timeout=2)
    chrome_mcp.jev_browser_stop()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert chrome_mcp.public_state()["status"] == "stopped"
    assert FakeAgent.instance.act_calls == 0


def test_deadline_after_inflight_prediction_prevents_following_action(monkeypatch):
    class FakeAgent:
        instance = None

        def __init__(self, _url, _goal, *, screenshots, page_guard, browser, run_guard=None):
            self.browser = browser
            self.act_calls = 0
            self.state = {"page": click_page(), "history": [], "status": "ready", "decision": None}
            FakeAgent.instance = self

        def command(self, name, _body=None):
            if name == "predict":
                time.sleep(0.3)
                self.state["decision"] = decision()
            elif name == "act":
                self.act_calls += 1

        def close(self):
            return None

    install_fake_runtime(monkeypatch, FakeAgent)
    chrome_mcp.state = {"runId": "deadline-test", "status": "running"}
    deadline = time.monotonic() + 0.2
    chrome_mcp.drive(
        "Search",
        {"url": page()["url"], "tabId": "12"},
        ["https://example.com"],
        3,
        10,
        True,
        deadline=deadline,
    )
    assert chrome_mcp.public_state()["status"] == "budget_reached"
    assert FakeAgent.instance.act_calls == 0


def test_known_action_receipt_survives_connection_loss_without_replay(monkeypatch):
    class FakeAgent:
        instance = None

        def __init__(self, _url, _goal, *, screenshots, page_guard, browser, run_guard=None):
            self.browser = browser
            self.predict_calls = 0
            self.act_calls = 0
            self.state = {"page": click_page(), "history": [], "status": "ready", "decision": None}
            FakeAgent.instance = self

        def command(self, name, _body=None):
            if name == "predict":
                self.predict_calls += 1
                self.state["decision"] = decision(operation="CLICK")
            elif name == "act":
                self.act_calls += 1
                self.state["history"].append({"action": "Search", "receipt": {"executed": "e1", "settled": True}})
                raise ObservationUnavailable("Chrome extension disconnected", retryable=False)

        def close(self):
            return None

    install_fake_runtime(monkeypatch, FakeAgent)
    chrome_mcp.state = {"runId": "disconnect-test", "status": "running"}
    chrome_mcp.drive(
        "Search",
        {"url": page()["url"], "tabId": "12"},
        ["https://example.com"],
        3,
        10,
        True,
    )
    result = chrome_mcp.public_state()
    assert result["status"] == "needs_verification"
    assert result["steps"] == 1
    assert result["history"][0]["receipt"]["executed"] == "e1"
    assert "not replayed" in result["verificationReason"]
    assert FakeAgent.instance.predict_calls == FakeAgent.instance.act_calls == 1


def test_uncertain_action_timeout_is_terminal_and_never_reissued(monkeypatch):
    class FakeAgent:
        instance = None

        def __init__(self, _url, _goal, *, screenshots, page_guard, browser, run_guard=None):
            self.browser = browser
            self.predict_calls = 0
            self.act_calls = 0
            self.state = {"page": click_page(), "history": [], "status": "ready", "decision": None}
            FakeAgent.instance = self

        def command(self, name, _body=None):
            if name == "predict":
                self.predict_calls += 1
                self.state["decision"] = decision(operation="CLICK")
            elif name == "act":
                self.act_calls += 1
                raise TimeoutError("Chrome extension timed out during act")

        def close(self):
            return None

    install_fake_runtime(monkeypatch, FakeAgent)
    chrome_mcp.state = {"runId": "unknown-outcome", "status": "running"}
    chrome_mcp.drive(
        "Search",
        {"url": page()["url"], "tabId": "12"},
        ["https://example.com"],
        3,
        10,
        True,
    )
    result = chrome_mcp.public_state()
    assert result["status"] == "error"
    assert "timed out" in result["error"]
    assert FakeAgent.instance.predict_calls == FakeAgent.instance.act_calls == 1


def test_explicit_stale_rejection_reobserves_before_a_new_decision(monkeypatch):
    class FakeAgent:
        instance = None

        def __init__(self, _url, _goal, *, screenshots, page_guard, browser, run_guard=None):
            self.browser = browser
            self.predict_calls = 0
            self.click_attempts = 0
            self.state = {"page": click_page(), "history": [], "status": "ready", "decision": None}
            FakeAgent.instance = self

        def command(self, name, _body=None):
            if name == "predict":
                self.predict_calls += 1
                self.state["decision"] = (
                    decision("e1", "CLICK") if self.predict_calls == 1 else decision("DONE", "DONE")
                )
            elif name == "act" and self.state["decision"]["choice"] == "e1":
                self.click_attempts += 1
                raise StalePage("STALE_PAGE")
            elif name == "act":
                self.state["status"] = "done"

        def close(self):
            return None

    install_fake_runtime(monkeypatch, FakeAgent)
    chrome_mcp.state = {"runId": "stale-test", "status": "running"}
    chrome_mcp.drive(
        "Search",
        {"url": page()["url"], "tabId": "12"},
        ["https://example.com"],
        3,
        10,
        True,
    )
    assert FakeAgent.instance.click_attempts == 1
    assert FakeAgent.instance.predict_calls == 2
    assert FakeAgent.instance.browser.observe_calls >= 1
    assert chrome_mcp.public_state()["status"] == "needs_verification"


@pytest.mark.parametrize(
    "settlement",
    ["navigation-timeout", "tab-unavailable", "busy-timeout", "dom-change-inconclusive"],
)
def test_unsettled_delayed_action_waits_read_only_then_requires_verification(monkeypatch, settlement):
    start = time.monotonic()

    class FakeBrowser:
        def __init__(self, _url, _tab_id, deadline=None):
            self.deadline = deadline
            self.page = click_page()
            self.observe_calls = 0

        def observe(self, screenshot=False):
            self.observe_calls += 1
            if self.observe_calls == 1:
                return dict(self.page)
            assert time.monotonic() - start >= 0.7
            self.page = {**self.page, "text": "Search submitted", "fingerprint": "after"}
            return dict(self.page)

        def close(self):
            return None

    class FakeAgent:
        instance = None

        def __init__(self, _url, _goal, *, screenshots, page_guard, browser, run_guard=None):
            self.browser = browser
            self.predict_calls = 0
            self.act_calls = 0
            self.state = {"page": click_page(), "history": [], "status": "ready", "decision": None}
            FakeAgent.instance = self

        def command(self, name, _body=None):
            if name == "predict":
                self.predict_calls += 1
                self.state["decision"] = decision(operation="CLICK")
            elif name == "act":
                self.act_calls += 1
                self.state["history"].append(
                    {
                        "action": "Search",
                        "receipt": {"executed": "e1", "settled": False, "settlement": settlement},
                    }
                )
                self.state["page"] = self.browser.observe()

        def close(self):
            return None

    install_fake_runtime(monkeypatch, FakeAgent, FakeBrowser)
    chrome_mcp.state = {"runId": "unsettled-test", "status": "running"}
    chrome_mcp.drive(
        "Search",
        {"url": page()["url"], "tabId": "12"},
        ["https://example.com"],
        3,
        10,
        True,
    )
    result = chrome_mcp.public_state()
    assert result["status"] == "needs_verification"
    assert result["final"]["text"] == "Search submitted"
    assert result["history"][0]["receipt"]["settled"] is False
    assert FakeAgent.instance.predict_calls == FakeAgent.instance.act_calls == 1


def test_next_run_waits_until_previous_agent_detaches(monkeypatch):
    closing = threading.Event()
    release_close = threading.Event()

    class FakeBrowser:
        def __init__(self, _url, _tab_id, deadline=None):
            self.deadline = deadline
            self.page = {**page(), "actions": []}

        def observe(self, screenshot=False):
            return dict(self.page)

        def close(self):
            return None

    class FakeAgent:
        created = 0

        def __init__(self, _url, _goal, *, screenshots, page_guard, browser, run_guard=None):
            type(self).created += 1
            self.browser = browser
            self.state = {"page": browser.page, "history": [], "status": "ready", "decision": None}

        def command(self, name, _body=None):
            if name == "predict":
                self.state["decision"] = decision("DONE", "DONE")
            elif name == "act":
                self.state["history"].append({"action": "DONE"})
                self.state["status"] = "done"

        def close(self):
            if type(self).created == 1:
                closing.set()
                release_close.wait(timeout=2)

    monkeypatch.setattr(chrome_mcp, "provider_setup", lambda: None)
    monkeypatch.setattr(chrome_mcp, "connection_ready", lambda: True)
    monkeypatch.setattr(chrome_mcp, "ExtensionBrowser", FakeBrowser)
    monkeypatch.setattr(chrome_mcp, "Agent", FakeAgent)
    chrome_mcp.listed = {"12": {"tabId": "12", "url": "https://example.com/"}}

    args = {
        "goal": "Verify page",
        "tab_id": "12",
        "expected_url": "https://example.com/",
        "allowed_origins": ["https://example.com"],
        "act": True,
        "max_steps": 3,
        "max_seconds": 10,
    }
    chrome_mcp.jev_browser_run(**args)
    assert closing.wait(timeout=2)
    old_worker = chrome_mcp.worker
    with pytest.raises(ValueError, match="already owns"):
        chrome_mcp.jev_browser_run(**args)
    assert FakeAgent.created == 1

    release_close.set()
    old_worker.join(timeout=2)
    assert not old_worker.is_alive()
    chrome_mcp.jev_browser_run(**args)
    new_worker = chrome_mcp.worker
    new_worker.join(timeout=2)
    assert not new_worker.is_alive()
    assert FakeAgent.created == 2


def test_no_current_window_returns_actionable_open_tab_failure(monkeypatch):
    monkeypatch.setattr(chrome_mcp, "extension_status", lambda: {"connected": True, "extensionVersion": "1.0.2"})
    calls = []

    def fail(method, params):
        calls.append((method, params))
        raise RuntimeError("No current window")

    monkeypatch.setattr(chrome_mcp, "extension_call", fail)
    result = chrome_mcp.jev_browser_open_tab("https://example.com/")
    assert result["status"] == "needs_chrome_window"
    assert result["usableWindow"] is False
    assert "will not launch Chrome" in result["instruction"]
    assert len(calls) == 1
    assert calls[0][1] == {"url": "https://example.com/", "active": False}


def test_status_is_connection_only_and_empty_tab_list_does_not_claim_no_window(monkeypatch):
    monkeypatch.setattr(chrome_mcp, "extension_status", lambda: {"connected": True, "extensionVersion": "1.0.2"})
    monkeypatch.setattr(chrome_mcp, "extension_call", lambda *_: pytest.fail("status must not probe tabs"))
    assert chrome_mcp.jev_browser_status()["connectionStatus"] == "connected"

    monkeypatch.setattr(chrome_mcp, "connection_ready", lambda: True)
    monkeypatch.setattr(chrome_mcp, "extension_call", lambda *_: {"tabs": []})
    result = chrome_mcp.jev_browser_tabs()
    assert result["status"] == "connected_no_accessible_http_tabs"
    assert result["windowAvailability"] == "unknown"
    assert "usableWindow" not in result


def test_extension_observation_disconnect_is_not_stale():
    def bridge(method, _params, timeout=20):
        if method == "get_tab":
            return {"url": "https://example.com/"}
        if method == "observe":
            raise RuntimeError("Jev Chrome extension is not connected")
        raise AssertionError(method)

    with patch("jev_ultrafast.extension_browser.call", side_effect=bridge):
        browser = ExtensionBrowser("https://example.com/", "12")
        with pytest.raises(ObservationUnavailable) as error:
            browser.observe()
        assert not error.value.retryable
        assert not isinstance(error.value, StalePage)


@pytest.mark.parametrize("settlement", ["tab-unavailable", "busy-timeout", "dom-change-inconclusive"])
def test_new_settlement_reasons_reach_compact_agent_history(settlement):
    digest = "a" * 64
    observed = {
        "url": "https://example.com/",
        "title": "Example",
        "text": "Ready",
        "actions": [{"id": "e1", "kind": "click", "node": 7, "label": "Search", "role": "button"}],
        "scroll": {"y": 0, "height": 800},
        "marker": [1],
        "page_key": [1],
        "guards": {"7": [7, "button", "Search"]},
    }
    calls = []

    def bridge(method, _params, timeout=20):
        calls.append(method)
        if method == "get_tab":
            return {"url": "https://example.com/"}
        if method == "observe":
            return dict(observed)
        if method == "act":
            return {
                "executed": "e1",
                "settled": False,
                "settlement": settlement,
                "baseline": digest,
                "immediate": digest,
                "snapshot": "large page snapshot must not be retained",
            }
        raise AssertionError(method)

    with patch("jev_ultrafast.extension_browser.call", side_effect=bridge):
        browser = ExtensionBrowser("https://example.com/", "12")
        agent = Agent("https://example.com/", "Search", browser=browser)
        agent.state["started_at"] = time.perf_counter()
        agent.state["decision"] = {
            "choice": "e1",
            "operation": "CLICK",
            "target": "1",
            "probabilities": {"e1": 1.0},
            "confidence": 0.9,
            "latency_ms": 1,
            "usage": {},
        }
        agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]})
        agent.close()

    receipt = agent.state["history"][0]["receipt"]
    assert receipt == {
        "executed": "e1",
        "settled": False,
        "settlement": settlement,
        "baseline": digest,
        "immediate": digest,
    }
    assert "snapshot" not in receipt
    assert calls.count("act") == 1
