import base64

import pytest

import chrome_mcp


def test_open_tab_returns_observed_tab_without_replacing_existing(monkeypatch):
    monkeypatch.setattr(chrome_mcp, "worker", None)
    monkeypatch.setattr(chrome_mcp, "connection_ready", lambda: True)
    monkeypatch.setattr(chrome_mcp, "listed", {"old": {"url": "https://example.org/"}})
    calls = []
    tab = {"tabId": "new", "url": "https://example.com/", "active": True}

    def call(method, params):
        calls.append((method, params))
        return tab

    monkeypatch.setattr(chrome_mcp, "extension_call", call)
    assert chrome_mcp.jev_browser_open_tab(tab["url"]) == {"status": "opened", "tab": tab}
    assert "old" in chrome_mcp.listed and chrome_mcp.listed["new"] == tab
    assert calls == [("create_tab", {"url": tab["url"], "active": True})]


@pytest.mark.parametrize("url", ["file:///secret", "javascript:alert(1)", "https://user:pass@example.com"])
def test_open_tab_rejects_non_web_or_credential_urls(url):
    with pytest.raises(ValueError):
        chrome_mcp.jev_browser_open_tab(url)


def test_open_tab_timeout_is_not_retried(monkeypatch):
    monkeypatch.setattr(chrome_mcp, "worker", None)
    monkeypatch.setattr(chrome_mcp, "connection_ready", lambda: True)
    calls = []

    def call(*args):
        calls.append(args)
        raise TimeoutError("response lost")

    monkeypatch.setattr(chrome_mcp, "extension_call", call)
    with pytest.raises(TimeoutError):
        chrome_mcp.jev_browser_open_tab("https://example.com/")
    assert len(calls) == 1


class FakeBrowser:
    def __init__(self, _url, _tab_id):
        self.page = {
            "url": "https://example.com/",
            "title": "Complete",
            "text": "SUCCESS",
            "actions": [],
            "fingerprint": "page-1",
        }

    def observe(self, screenshot=False):
        return dict(self.page)

    def call(self, method, **_params):
        assert method == "Page.captureScreenshot"
        return {"data": base64.b64encode(b"png-proof").decode("ascii")}

    def close(self):
        return None


class FakeAgent:
    def __init__(self, _url, _goal, *, screenshots, page_guard, browser):
        assert not screenshots
        self.browser = browser
        self.state = {"page": browser.observe(), "history": [], "status": "ready", "decision": None}

    def command(self, command, _params=None):
        if command == "predict":
            self.state["decision"] = {
                "choice": "DONE",
                "operation": "DONE",
                "confidence": 0.99,
                "target_confidence": None,
                "model": "fake-jev",
                "latency_ms": 1,
            }
        elif command == "act":
            self.state["history"].append({"action": "DONE"})
            self.state["status"] = "done"

    def close(self):
        self.browser.close()


def test_terminal_status_is_published_after_final_proof(tmp_path, monkeypatch):
    events = []
    original_publish = chrome_mcp.publish

    def recording_publish(**values):
        events.append(dict(values))
        original_publish(**values)

    monkeypatch.setattr(chrome_mcp, "ROOT", tmp_path)
    monkeypatch.setattr(chrome_mcp, "provider_setup", lambda: None)
    monkeypatch.setattr(chrome_mcp, "ExtensionBrowser", FakeBrowser)
    monkeypatch.setattr(chrome_mcp, "Agent", FakeAgent)
    monkeypatch.setattr(chrome_mcp, "publish", recording_publish)
    chrome_mcp.stopped.clear()
    chrome_mcp.state = {"runId": "test-run", "status": "running", "steps": 0, "goalVerified": False}

    chrome_mcp.drive(
        "Verify success",
        {"tabId": "12", "url": "https://example.com/"},
        ["https://example.com"],
        max_steps=3,
        max_seconds=10,
        act=True,
    )

    final_index = next(i for i, event in enumerate(events) if "final" in event)
    terminal_index = next(i for i, event in enumerate(events) if event.get("status") == "needs_verification")
    assert final_index < terminal_index
    assert chrome_mcp.public_state()["final"]["text"] == "SUCCESS"
    assert (tmp_path / "runs/test-run/final.png").read_bytes() == b"png-proof"


def test_host_reply_clears_request_before_worker_resumes():
    request_id = "3ba78a31-3fb8-440e-9f03-b0ed9b6e5432"
    chrome_mcp.responded.clear()
    chrome_mcp.state = {
        "runId": "host-test",
        "status": "needs_host",
        "request": {"id": request_id},
    }

    assert chrome_mcp.jev_browser_respond(request_id=request_id, text="Field value") == {"accepted": True}
    assert chrome_mcp.public_state()["status"] == "running"
    assert chrome_mcp.public_state()["request"] is None
    with pytest.raises(ValueError, match="Stale or already answered"):
        chrome_mcp.jev_browser_respond(request_id=request_id, text="Field value")
