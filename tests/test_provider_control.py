"""Provider retries stop cooperatively when an MCP run is stopped or out of time."""

import time
from types import SimpleNamespace

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import model


class GuardStopped(RuntimeError):
    pass


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.is_error = status_code >= 400

    def json(self):
        return {"ok": True}


class FakeClient:
    def __init__(self, response, on_post=None):
        self.response = response
        self.on_post = on_post
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.on_post:
            self.on_post()
        return self.response


@pytest.mark.parametrize("status_code", [503, 429])
@pytest.mark.parametrize("stop_phase", ["before_wait", "after_wait"])
def test_provider_retry_does_not_issue_another_request_after_guard(
    monkeypatch, status_code, stop_phase
):
    stopped = False
    sleep_calls = []

    def stop_during_post():
        nonlocal stopped
        if stop_phase == "before_wait":
            stopped = True

    def fake_sleep(_seconds):
        nonlocal stopped
        sleep_calls.append(_seconds)
        if stop_phase == "after_wait":
            stopped = True

    def guard():
        if stopped:
            raise GuardStopped("run stopped")

    client = FakeClient(FakeResponse(status_code), on_post=stop_during_post)
    monkeypatch.setattr(model, "CLIENT", client)
    monkeypatch.setattr(model.time, "sleep", fake_sleep)

    with model.run_guard_scope(guard), pytest.raises(GuardStopped, match="run stopped"):
        model.post_json("https://provider.test", "test-key", {})

    assert len(client.calls) == 1
    assert len(sleep_calls) == (0 if stop_phase == "before_wait" else 1)


def test_post_json_without_run_scope_keeps_default_behavior(monkeypatch):
    client = FakeClient(FakeResponse(200))
    monkeypatch.setattr(model, "CLIENT", client)

    assert model.post_json("https://provider.test", "test-key", {}) == {"ok": True}
    assert len(client.calls) == 1


def test_run_guard_scope_resets_after_exception(monkeypatch):
    client = FakeClient(FakeResponse(200))
    monkeypatch.setattr(model, "CLIENT", client)

    def guard():
        raise GuardStopped("run stopped")

    with pytest.raises(GuardStopped, match="run stopped"):
        with model.run_guard_scope(guard):
            model.post_json("https://provider.test", "test-key", {})

    assert model.post_json("https://provider.test", "test-key", {}) == {"ok": True}
    assert len(client.calls) == 1


@pytest.mark.parametrize("model_call", ["prediction", "text"])
def test_agent_propagates_run_guard_without_changing_model_call_signatures(monkeypatch, model_call):
    stopped = False

    def guard():
        if stopped:
            raise GuardStopped("run stopped")

    def stop_after_request():
        nonlocal stopped
        stopped = True

    client = FakeClient(FakeResponse(503), on_post=stop_after_request)
    monkeypatch.setattr(model, "CLIENT", client)
    monkeypatch.setattr(model.time, "sleep", lambda _seconds: None)

    page = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "fingerprint": "observed-page",
        "actions": [{"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": ""}],
    }
    browser = SimpleNamespace(fresh=lambda _page: True, act=lambda *_args, **_kwargs: pytest.fail("must not act"))
    agent = loop.Agent.__new__(loop.Agent)
    agent.run_guard = guard
    agent.page_guard = None
    agent.pending_text = None
    agent.screenshots = False
    agent.state = {
        "browser": browser,
        "page": page,
        "goal": "Search",
        "history": [],
        "decision": None,
        "decisions": [],
        "status": "ready",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
    }

    if model_call == "prediction":
        def choose_three_args(_state, _goal, _history):
            return model.post_json("https://provider.test", "test-key", {})

        monkeypatch.setattr(loop, "choose", choose_three_args)

        def run():
            agent.command("predict")
    else:
        agent.state["decision"] = {
            "choice": "e1",
            "probabilities": {"e1": 1.0},
            "confidence": 1.0,
            "operation": "TYPE_TEXT",
            "target": "1",
            "latency_ms": 1,
            "usage": {},
        }

        def generate_text(_context):
            return model.post_json("https://provider.test", "test-key", {})

        monkeypatch.setattr(loop, "field_text", generate_text)

        def run():
            agent.command("act", {"fingerprint": page["fingerprint"]})

    with pytest.raises(GuardStopped, match="run stopped"):
        run()

    assert len(client.calls) == 1
