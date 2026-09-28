import json
import threading
import time
from collections import deque
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import chrome_mcp
import jev_ultrafast.agent as agent_module

URL = "https://example.com/form"
ORIGINS = ["https://example.com"]
PRIVATE_UNUSED = "unused-private-value-7c8a"


def entry(**changes):
    return {"page_url": URL, "field_label": "Name", "field_role": "textbox", "text": "Exact caller value", **changes}


def observed_page():
    return {
        "url": URL, "title": "Form", "text": "Visible form", "fingerprint": "initial",
        "omitted_actions": 0,
        "actions": [{"id": "e1", "kind": "fill", "node": 7, "label": "Name", "role": "textbox", "value": ""}],
    }


@pytest.fixture(autouse=True)
def isolated_mcp(monkeypatch):
    for key, value in {
        "state": {"status": "idle"}, "worker": None, "run_active": False, "active_run_id": None,
        "run_deadline": None, "pending_reply": None, "listed": {"12": {"tabId": "12", "url": URL}},
        "stopped": threading.Event(), "responded": threading.Event(),
    }.items():
        monkeypatch.setattr(chrome_mcp, key, value)


@pytest.fixture
def runtime(monkeypatch):
    run = SimpleNamespace(
        page=observed_page(), choices=deque(["e1", "DONE"]), attempts=[], handoffs=[], requests=[],
        pending_at_prediction=[], fresh_calls=0, after_choose=None, on_fresh=None, act_error=None,
        agent=None, constructor_keys=None, host_text="Manual host value",
    )

    class Browser:
        def __init__(self, _url, _tab_id, deadline=None):
            self.deadline = deadline

        def observe(self, screenshot=False):
            assert not screenshot
            return deepcopy(run.page)

        def fresh(self, _page):
            run.fresh_calls += 1
            return run.on_fresh(run.fresh_calls) if run.on_fresh else True

        def act(self, action, _page, text=None):
            run.attempts.append({"id": action["id"], "node": action.get("node"), "text": text})
            if run.act_error:
                raise run.act_error
            for candidate in run.page["actions"]:
                if candidate.get("node") == action.get("node") and candidate["kind"] == "fill":
                    candidate["value"] = text
            run.page["fingerprint"] = f"after-{len(run.attempts)}"
            return {"executed": action["id"], "settled": True, "settlement": "visible-change"}

        def close(self):
            pass

    def make_agent(*args, **kwargs):
        run.constructor_keys = set(kwargs)
        run.agent = agent_module.Agent(*args, **kwargs)
        return run.agent

    def choose(page, goal, history):
        run.requests.append(deepcopy({"page": page, "goal": goal, "history": history}))
        run.pending_at_prediction.append(deepcopy(run.agent.pending_text))
        choice = run.choices.popleft()
        operation = choice if choice in {"DONE", "BLOCKED"} else {
            "fill": "TYPE_TEXT", "click": "CLICK",
        }[next(action["kind"] for action in page["actions"] if action["id"] == choice)]
        if run.after_choose:
            run.after_choose()
        return {
            "choice": choice, "operation": operation, "target": "1", "confidence": 1,
            "target_confidence": 1, "probabilities": {choice: 1}, "model": "offline",
            "latency_ms": 0, "usage": {},
        }

    def respond(_timeout):
        request = deepcopy(chrome_mcp.public_state()["request"])
        run.handoffs.append(request)
        chrome_mcp.jev_browser_respond(request["id"], run.host_text)
        return True

    def drive(inputs=None, *, act=True, goal="Fill the selected field"):
        chrome_mcp.state = {"runId": "prepared-test", "status": "running"}
        prepared = chrome_mcp.validate_text_inputs(inputs, ORIGINS)
        chrome_mcp.drive(goal, {"url": URL, "tabId": "12"}, ORIGINS, 8, 10, act,
                         prepared_text=prepared)
        return chrome_mcp.public_state()

    monkeypatch.setattr(chrome_mcp, "provider_setup", lambda: None)
    monkeypatch.setattr(chrome_mcp, "connection_ready", lambda: True)
    monkeypatch.setattr(chrome_mcp, "ExtensionBrowser", Browser)
    monkeypatch.setattr(chrome_mcp, "Agent", make_agent)
    monkeypatch.setattr(agent_module, "choose", choose)
    monkeypatch.setattr(agent_module, "field_text", lambda *_: pytest.fail("No paid or invented text helper call"))
    monkeypatch.setattr(chrome_mcp.responded, "wait", respond)
    run.drive = drive
    return run


@pytest.mark.parametrize("source", ["manual", "prepared"])
@pytest.mark.parametrize("goal", [
    " \tFill the selected field", "Fill the selected field\n ", " \tFill the selected field\n ",
])
def test_whitespace_goal_preserves_supplied_text_without_helper(runtime, monkeypatch, source, goal):
    supplied_text = " \tExact caller value  \n"
    runtime.host_text = supplied_text
    helper = Mock(side_effect=AssertionError("Unexpected field_text for supplied text"))
    monkeypatch.setattr(agent_module, "field_text", helper)

    result = runtime.drive([entry(text=supplied_text)] if source == "prepared" else None, goal=goal)

    helper.assert_not_called()
    assert result["status"] == "needs_verification"
    assert runtime.attempts == [{"id": "e1", "node": 7, "text": supplied_text}]
    assert result["history"][0]["text"] == supplied_text
    assert result["history"][0]["text_helper"] == ("host-prepared" if source == "prepared" else "host-agent")
    assert all(request["goal"] == goal.strip() for request in runtime.requests)
    assert all(request["context"]["goal"] == goal.strip() for request in runtime.handoffs)
    assert result["goalVerified"] is False


@pytest.mark.parametrize("inputs", [
    {}, "private-invalid-list", False, (entry(),), [None], ["private-invalid-entry"],
    [{key: value for key, value in entry().items() if key != "text"}],
    [entry(selector="private-selector")], [entry(node=7)], [entry(operation="TYPE_TEXT")],
    [entry(script="private-script")], [entry(**{PRIVATE_UNUSED: "value"})],
    [entry(page_url=None)], [entry(field_label=[])], [entry(field_role=True)], [entry(text=123)],
    [entry(page_url="")], [entry(page_url="https://example.com/" + "x" * 4096)],
    [entry(page_url=" https://example.com/form")], [entry(page_url="https://example.com/\nform")],
    [entry(page_url="javascript:private-value")], [entry(page_url="file:///private")],
    [entry(page_url="https://user:private-password@example.com/form")],
    [entry(page_url="https://outside.example/form")], [entry(page_url="https://[invalid/form")],
    [entry(page_url="https://example.com:invalid/form")],
    [entry(field_label="")], [entry(field_label="x" * 513)], [entry(field_role="button")],
    [entry(text="")], [entry(text="x" * 2001)],
    [entry(), entry(text="Different private value")],
    [entry(field_label=f"Field {index}") for index in range(17)],
])
def test_invalid_inputs_rejected_before_connection_or_worker(monkeypatch, inputs):
    monkeypatch.setattr(chrome_mcp, "connection_ready", lambda: pytest.fail("Validation must precede bridge access"))
    before = chrome_mcp.state
    with pytest.raises(ValueError) as error:
        chrome_mcp.jev_browser_run("Fill", "12", URL, ORIGINS, text_inputs=inputs)
    assert "private" not in str(error.value).lower()
    assert chrome_mcp.state is before
    assert chrome_mcp.worker is None


@pytest.mark.parametrize("role", ["textbox", "searchbox", "combobox", "spinbutton"])
def test_valid_bounds_and_exact_strings(role):
    inputs = [
        entry(field_label=str(index).zfill(12) + "x" * 500, field_role=role, text=" " * 2000) for index in range(16)
    ]
    prepared = chrome_mcp.validate_text_inputs(inputs, ORIGINS)
    assert len(prepared) == 16
    assert set(prepared.values()) == {" " * 2000}
    assert chrome_mcp.validate_text_inputs(None, ORIGINS) == chrome_mcp.validate_text_inputs([], ORIGINS) == {}


def test_exact_match_only_after_choice_keeps_unused_values_private(runtime, capsys):
    result = runtime.drive([entry(), entry(field_label="Unused", text=PRIVATE_UNUSED)])
    assert runtime.attempts == [{"id": "e1", "node": 7, "text": "Exact caller value"}]
    assert not runtime.handoffs
    assert result["preparedTextHits"] == 1 and result["hostTextRequests"] == 0
    assert result["hostWaitMs"] == 0
    assert result["history"][0]["text_helper"] == "host-prepared"
    assert "Exact caller value" not in json.dumps(runtime.requests[0])
    assert PRIVATE_UNUSED not in json.dumps([runtime.requests, result, runtime.handoffs])
    assert "prepared_text" not in runtime.constructor_keys and "text_inputs" not in runtime.constructor_keys
    output = capsys.readouterr()
    assert PRIVATE_UNUSED not in output.out + output.err
    assert result["goalVerified"] is False


@pytest.mark.parametrize("change", [
    {"page_url": URL + "/"}, {"page_url": URL + "?q=1"}, {"page_url": URL + "#form"},
    {"field_label": "name"}, {"field_role": "searchbox"},
])
def test_exact_descriptor_miss_uses_manual_handoff(runtime, change):
    result = runtime.drive([entry(**change)])
    assert runtime.attempts[0]["text"] == "Manual host value"
    assert result["preparedTextHits"] == 0 and result["hostTextRequests"] == 1
    assert "Exact caller value" not in json.dumps([runtime.requests, runtime.handoffs, result])


@pytest.mark.parametrize("selected_kind", ["fill", "click"])
def test_prepared_input_never_changes_jev_operation_or_target(runtime, selected_kind):
    runtime.page["actions"].append({
        "id": "e2", "kind": selected_kind, "node": 8 if selected_kind == "fill" else 7,
        "label": "Other" if selected_kind == "fill" else "Name", "role": "textbox", "value": "",
    })
    runtime.choices = deque(["e2", "DONE"])
    result = runtime.drive([entry()])
    assert [attempt["id"] for attempt in runtime.attempts] == ["e2"]
    assert result["preparedTextHits"] == 0
    assert result["hostTextRequests"] == int(selected_kind == "fill")


@pytest.mark.parametrize("inputs", [None, []])
def test_default_manual_handoff_unchanged(runtime, inputs):
    result = runtime.drive(inputs)
    assert runtime.attempts[0]["text"] == "Manual host value"
    assert result["preparedTextHits"] == 0 and result["hostTextRequests"] == 1


def test_provider_error_cannot_expose_unconsumed_inputs(runtime):
    def fail():
        raise RuntimeError("offline provider error")

    runtime.after_choose = fail
    result = runtime.drive([entry(text=PRIVATE_UNUSED)])
    assert result["status"] == "error" and not runtime.attempts
    assert PRIVATE_UNUSED not in json.dumps([runtime.requests, result])


@pytest.mark.parametrize("mode,hit", [
    ("two_fill_nodes", False), ("click_counterpart", True), ("same_fill_node", True),
    ("omitted", False), ("missing_count", False), ("invalid_count", False), ("bool_count", False),
    ("null_count", False), ("negative_count", False), ("float_count", False), ("missing_role", False),
])
def test_unique_visible_fill_requires_complete_snapshot(runtime, mode, hit):
    if mode in {"two_fill_nodes", "click_counterpart", "same_fill_node"}:
        candidate = {**runtime.page["actions"][0], "id": "e2"}
        if mode == "two_fill_nodes":
            candidate["node"] = 8
        if mode == "click_counterpart":
            candidate["kind"] = "click"
        runtime.page["actions"].append(candidate)
    elif mode == "missing_count":
        runtime.page.pop("omitted_actions")
    elif mode == "missing_role":
        runtime.page["actions"][0].pop("role")
    else:
        runtime.page["omitted_actions"] = {
            "omitted": 1, "invalid_count": "0", "bool_count": False, "null_count": None,
            "negative_count": -1, "float_count": 0.0,
        }[mode]
    result = runtime.drive([entry()])
    assert result["preparedTextHits"] == int(hit)
    assert result["hostTextRequests"] == int(not hit)
    assert runtime.attempts[0]["text"] == ("Exact caller value" if hit else "Manual host value")


@pytest.mark.parametrize("node", [None, True, "7", -1, 0, [], "missing"])
def test_missing_or_invalid_node_cannot_consume_entry(node):
    page = observed_page()
    if node == "missing":
        page["actions"][0].pop("node")
    else:
        page["actions"][0]["node"] = node
    prepared = chrome_mcp.validate_text_inputs([entry()], ORIGINS)
    assert chrome_mcp.take_prepared_text(prepared, page, page["actions"][0]) is None
    assert len(prepared) == 1


def test_entry_consumed_once_for_repeated_selected_field(runtime):
    runtime.choices = deque(["e1", "e1", "DONE"])
    result = runtime.drive([entry()])
    assert [attempt["text"] for attempt in runtime.attempts] == ["Exact caller value", "Manual host value"]
    assert result["preparedTextHits"] == result["hostTextRequests"] == 1


@pytest.mark.parametrize("replacement", ["same", "node", "path"])
def test_stale_prepared_cache_cleared_before_new_prediction(runtime, replacement):
    runtime.choices = deque(["e1", "e1", "DONE"])

    def fresh(count):
        if count == 2:  # Agent checks freshness after the prepared reservation.
            if replacement == "node":
                runtime.page["actions"][0]["node"] = 99
            elif replacement == "path":
                runtime.page["url"] = "https://example.com/replacement"
            runtime.page["fingerprint"] = "replacement"
            return False
        return True

    runtime.on_fresh = fresh
    result = runtime.drive([entry()])
    assert len(runtime.attempts) == 1 and runtime.attempts[0]["text"] == "Manual host value"
    assert runtime.pending_at_prediction == [None, None, None]
    assert result["preparedTextHits"] == result["hostTextRequests"] == 1
    assert "Exact caller value" not in json.dumps([runtime.requests, result])


@pytest.mark.parametrize("mode", ["dry_run", "stop", "deadline"])
def test_no_prepared_reservation_or_handoff_after_stop_budget_or_dry_run(runtime, monkeypatch, mode):
    expired = False

    def after_choose():
        nonlocal expired
        if mode == "stop":
            chrome_mcp.jev_browser_stop()
        if mode == "deadline":
            expired = True

    runtime.after_choose = after_choose
    monkeypatch.setattr(chrome_mcp, "time", SimpleNamespace(
        monotonic=lambda: 100 if expired else 0, perf_counter_ns=time.perf_counter_ns,
    ))
    monkeypatch.setattr(chrome_mcp, "take_prepared_text", lambda *_: pytest.fail("Must not reserve input"))
    result = runtime.drive([entry()], act=mode != "dry_run")
    assert result["status"] == {"dry_run": "dry_run", "stop": "stopped", "deadline": "budget_reached"}[mode]
    assert result["preparedTextHits"] == result["hostTextRequests"] == 0
    assert not runtime.attempts and not runtime.handoffs


def test_stop_during_action_freshness_prevents_prepared_typing(runtime):
    def fresh(count):
        if count == 2:
            chrome_mcp.jev_browser_stop()
        return True

    runtime.on_fresh = fresh
    result = runtime.drive([entry()])
    assert result["status"] == "stopped"
    assert result["preparedTextHits"] == 1 and not runtime.attempts


def test_uncertain_mutation_never_restores_or_replays_prepared_entry(runtime):
    runtime.act_error = TimeoutError("uncertain execution")
    result = runtime.drive([entry(), entry(field_label="Unused", text=PRIVATE_UNUSED)])
    assert result["status"] == "error"
    assert result["preparedTextHits"] == 1 and len(runtime.attempts) == 1
    assert len(runtime.requests) == 1 and not runtime.handoffs
    assert PRIVATE_UNUSED not in json.dumps([result, runtime.requests])


def test_api_copies_caller_inputs_and_next_default_run_still_requests_host(runtime, monkeypatch):
    class DeferredThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            pass

        def is_alive(self):
            return False

        def finish(self):
            self.target(*self.args)

    monkeypatch.setattr(chrome_mcp.threading, "Thread", DeferredThread)
    inputs = [entry(), entry(field_label="Unused", text=PRIVATE_UNUSED)]
    # Existing positional arguments remain in order; text_inputs is appended.
    initial = chrome_mcp.jev_browser_run("Fill", "12", URL, ORIGINS, True, 8, 10, False, inputs)
    assert "Exact caller value" not in json.dumps(initial) and PRIVATE_UNUSED not in json.dumps(initial)
    inputs[0]["text"] = "mutated after startup"
    inputs[0]["field_label"] = "Changed descriptor"
    inputs.clear()
    chrome_mcp.worker.finish()
    assert runtime.attempts[0]["text"] == "Exact caller value"

    runtime.choices = deque(["e1", "DONE"])
    chrome_mcp.jev_browser_run("New task", "12", URL, ORIGINS, True, 8, 10)
    chrome_mcp.worker.finish()
    result = chrome_mcp.public_state()
    assert runtime.attempts[-1]["text"] == "Manual host value"
    assert result["preparedTextHits"] == 0 and result["hostTextRequests"] == 1
    assert PRIVATE_UNUSED not in json.dumps([runtime.requests, runtime.handoffs, result])
