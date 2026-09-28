#!/usr/bin/env python3
"""Bounded offline reliability benchmark for Jev's agent loop.

This harness runs the shipped Agent and model.action_space against a synthetic
fixture adapter. It never starts a browser or calls a model provider. Its
timings are local Python simulation timings, not Chrome or real Jev evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

from jev_ultrafast import agent as agent_module
from jev_ultrafast import model
from jev_ultrafast.browser import StalePage

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "jev_reliability.html"
SUPPORTED_CONTROLS = {"a", "button", "input", "select"}
MAX_ITERATIONS = 100
EXPECTED_OUTCOMES = {"done", "blocked", "origin_rejected", "connection_error"}


@dataclass
class FixtureControl:
    node: int
    tag: str
    attrs: dict[str, str]
    label: str
    text: str = ""
    options: list[dict[str, str | bool]] = field(default_factory=list)


@dataclass
class FixtureScenario:
    name: str
    attrs: dict[str, str]
    controls: list[FixtureControl] = field(default_factory=list)


class _FixtureParser(HTMLParser):
    """Read only the small, explicit scenario/control subset in the fixture."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scenarios: list[FixtureScenario] = []
        self.current: FixtureScenario | None = None
        self.control: FixtureControl | None = None
        self.option: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, raw_attrs: list[tuple[str, str | None]]) -> None:
        attrs = {key: value or "" for key, value in raw_attrs}
        if tag == "main" and attrs.get("data-scenario"):
            name = attrs["data-scenario"]
            if any(scenario.name == name for scenario in self.scenarios):
                raise ValueError(f"Duplicate fixture scenario: {name}")
            self.current = FixtureScenario(name, attrs)
            self.scenarios.append(self.current)
            return
        if self.current is None:
            return
        if tag == "option" and self.control and self.control.tag == "select":
            self.option = {
                "value": attrs.get("value", ""),
                "selected": "selected" in attrs,
                "disabled": "disabled" in attrs,
                "label": "",
            }
        elif tag in SUPPORTED_CONTROLS and self.control is None:
            label = attrs.get("aria-label") or attrs.get("placeholder") or attrs.get("title") or ""
            control = FixtureControl(len(self.current.controls) + 1, tag, attrs, label)
            if tag == "input":
                self.current.controls.append(control)
            else:
                self.control = control

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.option is not None:
            self.option["label"] = str(self.option["label"]) + data.strip()
        elif self.control is not None and self.control.tag in {"a", "button"}:
            self.control.text += data.strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self.option is not None and self.control is not None:
            self.control.options.append(self.option)
            self.option = None
        elif self.control is not None and tag == self.control.tag:
            if not self.control.label:
                self.control.label = self.control.text or self.control.tag
            assert self.current is not None
            self.current.controls.append(self.control)
            self.control = None
        elif tag == "main":
            self.current = None


def load_fixture(path: Path = FIXTURE) -> list[FixtureScenario]:
    parser = _FixtureParser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    if not parser.scenarios:
        raise ValueError(f"No synthetic scenarios found in {path}")
    for scenario in parser.scenarios:
        if scenario.attrs.get("data-expected-outcome") not in EXPECTED_OUTCOMES:
            raise ValueError(f"Invalid expected outcome in scenario {scenario.name}")
        if not scenario.attrs.get("data-url"):
            raise ValueError(f"Missing data-url in scenario {scenario.name}")
        for control in scenario.controls:
            if control.tag != "input" and not control.label:
                raise ValueError(f"Missing accessible fixture label in {scenario.name}")
    return parser.scenarios


class OriginViolation(ValueError):
    """The synthetic task crossed its explicitly allowed origin boundary."""


class SyntheticBrowser:
    """Small deterministic adapter used only by this offline benchmark."""

    def __init__(self, scenario: FixtureScenario):
        self.scenario = scenario
        self.url = scenario.attrs["data-url"]
        self.text = scenario.attrs.get("data-initial-text", "Synthetic page")
        self.title = f"Synthetic {scenario.name}"
        self.values = {
            control.node: control.attrs.get("value", "")
            for control in scenario.controls
            if control.tag == "input"
        }
        self.selected = {
            control.node: next(
                (str(option["value"]) for option in control.options if option["selected"]),
                next((str(option["value"]) for option in control.options), ""),
            )
            for control in scenario.controls
            if control.tag == "select"
        }
        self.observation_calls = 0
        self.action_attempts = 0
        self.executed_actions: list[str] = []
        self.page_mutations = 0
        self.stale_rejections = 0
        self.connection_faults = 0
        self.pending_observations = 0
        self.connection_pending = False
        self.uncertain_action_signature: tuple[Any, ...] | None = None
        self.uncertain_action_replay_violations = 0
        self.stale_pending = scenario.attrs.get("data-fault") == "stale-once"
        self.closed = False

    def _marker(self) -> list[Any]:
        return [
            self.url,
            self.text,
            sorted(self.values.items()),
            sorted(self.selected.items()),
        ]

    def _control_actions(self) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        next_id = 1
        for control in self.scenario.controls:
            base = {
                "node": control.node,
                "role": {"input": "textbox", "select": "combobox", "button": "button", "a": "link"}[control.tag],
                "label": control.label,
                "value": "",
            }
            if control.tag == "input":
                value = self.values[control.node]
                actions.append({**base, "id": f"e{next_id}", "kind": "fill", "value": value})
                next_id += 1
                actions.append(
                    {
                        **base,
                        "id": f"e{next_id}",
                        "kind": "click",
                        "label": f"Open {control.label}",
                        "value": value,
                    }
                )
                next_id += 1
            elif control.tag == "select":
                current_value = self.selected[control.node]
                selected_option = next(
                    (option for option in control.options if option["value"] == current_value),
                    None,
                )
                current_label = str(selected_option["label"]) if selected_option else current_value
                for option in control.options:
                    if option["value"] == current_value or option["disabled"]:
                        continue
                    actions.append(
                        {
                            **base,
                            "id": f"e{next_id}",
                            "kind": "select",
                            "value": str(option["value"]),
                            "current_value": current_label,
                            "label": f"{control.label} → {option['label']}",
                        }
                    )
                    next_id += 1
            else:
                action = {**base, "id": f"e{next_id}", "kind": "click", "label": control.label}
                if control.tag == "a":
                    action["href"] = control.attrs.get("href", "")
                actions.append(action)
                next_id += 1
        actions.append({"id": "wait", "kind": "wait", "label": "Wait for the page to update"})
        return actions

    def _observe_state(self) -> dict[str, Any]:
        actions = self._control_actions()
        marker = self._marker()
        fingerprint = hashlib.sha256(
            json.dumps(
                {"url": self.url, "text": self.text, "actions": actions, "marker": marker},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "scroll": {"y": 0, "height": 800},
            "actions": actions,
            "marker": marker,
            "page_key": marker,
            "guards": {},
            "fingerprint": fingerprint,
        }

    def observe(self, screenshot: bool = False) -> dict[str, Any]:
        del screenshot
        self.observation_calls += 1
        if self.connection_pending:
            self.connection_pending = False
            self.connection_faults += 1
            raise ConnectionError("synthetic bridge connection fault")
        before = self._marker()
        if self.pending_observations:
            self.pending_observations -= 1
            if self.pending_observations == 0:
                self._complete()
        if self._marker() != before:
            self.page_mutations += 1
        return self._observe_state()

    def fresh(self, page: dict[str, Any], action: dict[str, Any] | None = None) -> bool:
        if action is not None and self.stale_pending:
            self.stale_pending = False
            self.stale_rejections += 1
            return False
        return page.get("marker") == self._marker()

    def act(self, action: dict[str, Any], page: dict[str, Any], text: str | None = None) -> dict[str, str]:
        self.action_attempts += 1
        signature = (action.get("kind"), action.get("node"), action.get("value"))
        if self.uncertain_action_signature == signature:
            self.uncertain_action_replay_violations += 1
        if not self.fresh(page, action):
            raise StalePage("Synthetic stale-target guard rejected the action")
        self.executed_actions.append(action["kind"])
        before = self._marker()
        if action["kind"] == "fill":
            self.values[action["node"]] = text or ""
        elif action["kind"] == "select":
            self.selected[action["node"]] = action["value"]
        elif action["kind"] == "click":
            self._click(action)
        elif action["kind"] == "wait":
            pass
        if self._marker() != before:
            self.page_mutations += 1
        if self.scenario.attrs.get("data-fault") == "connection-after-action":
            self.connection_pending = True
            self.uncertain_action_signature = signature
        return {"executed": action["id"]}

    def _click(self, action: dict[str, Any]) -> None:
        control = next(item for item in self.scenario.controls if item.node == action["node"])
        transition = control.attrs.get("data-transition", "no-op")
        if transition == "complete":
            self._complete()
        elif transition == "delay":
            self.text = self.scenario.attrs.get("data-loading-text", "Loading")
            self.pending_observations = int(self.scenario.attrs.get("data-delay-observations", "2"))
        elif transition == "delay-navigation":
            self.text = self.scenario.attrs.get("data-loading-text", "Preparing navigation")
            self.pending_observations = int(self.scenario.attrs.get("data-delay-observations", "2"))
        elif transition == "submit":
            expected_text = self.scenario.attrs.get("data-expected-text", "")
            expected_select = self.scenario.attrs.get("data-expected-select", "")
            actual_text = next(iter(self.values.values()), "")
            actual_select = next(iter(self.selected.values()), "")
            if actual_text == expected_text and actual_select == expected_select:
                self._complete()
            else:
                self.text = "Synthetic form validation failed"
        elif transition == "cross-origin":
            self.url = action.get("href", "https://outside.invalid/")
            self.text = "Synthetic external origin"

    def _complete(self) -> None:
        self.text = self.scenario.attrs.get("data-success-text", "Synthetic task complete")
        self.url = self.scenario.attrs.get("data-result-url", self.url)

    def close(self) -> None:
        self.closed = True


def _choose_for_scenario(
    scenario: FixtureScenario,
    page: dict[str, Any],
    calls: dict[str, int],
) -> dict[str, Any]:
    """Deterministic local policy that selects only real model.action_space targets."""
    calls["decision_calls"] += 1
    elements, targets, controls = model.action_space(page["actions"])
    del elements

    def selected(action: dict[str, Any], operation: str, target: str | None = None) -> dict[str, Any]:
        operation_name = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT", "wait": "WAIT"}.get(
            action["kind"], action["kind"].upper()
        )
        return {
            "choice": action["id"],
            "operation": operation_name,
            "target": target,
            "confidence": 1.0,
            "probabilities": {action["id"]: 1.0},
            "latency_ms": 0,
            "usage": {},
        }

    success = scenario.attrs.get("data-success-text")
    if success and success in page["text"]:
        return {
            "choice": "DONE",
            "operation": "DONE",
            "target": None,
            "confidence": 1.0,
            "probabilities": {"DONE": 1.0},
            "latency_ms": 0,
            "usage": {},
        }

    delayed = scenario.name in {"delayed_change", "delayed_navigation"}
    if delayed and scenario.attrs.get("data-loading-text") in page["text"]:
        action = controls["WAIT"]
        return selected(action, "WAIT")

    if scenario.name == "text_form_select":
        expected_text = scenario.attrs["data-expected-text"]
        fill_targets = targets.get("TYPE_TEXT", {})
        pending_fill = next((action for action in fill_targets.values() if action["value"] != expected_text), None)
        if pending_fill:
            return selected(pending_fill, "TYPE_TEXT", next(k for k, v in fill_targets.items() if v is pending_fill))
        expected_value = scenario.attrs["data-expected-select"]
        select_targets = targets.get("SELECT", {})
        pending_select = next((action for action in select_targets.values() if action["value"] == expected_value), None)
        if pending_select:
            return selected(pending_select, "SELECT", next(k for k, v in select_targets.items() if v is pending_select))
        submit_control = next(
            control for control in scenario.controls if control.attrs.get("data-transition") == "submit"
        )
        submit = next(action for action in targets["CLICK"].values() if action["node"] == submit_control.node)
        submit_target = next(k for k, value in targets["CLICK"].items() if value is submit)
        return selected(submit, "CLICK", submit_target)

    transition = {
        "immediate_click": "complete",
        "delayed_change": "delay",
        "delayed_navigation": "delay-navigation",
        "no_op": "no-op",
        "cross_origin": "cross-origin",
        "stale_action": "complete",
        "connection_fault": "complete",
    }.get(scenario.name)
    target_control = next(
        (control for control in scenario.controls if control.attrs.get("data-transition") == transition),
        None,
    )
    if target_control is None:
        raise ValueError(f"No scripted synthetic control for {scenario.name}")
    action = next(item for item in targets["CLICK"].values() if item["node"] == target_control.node)
    target = next(key for key, value in targets["CLICK"].items() if value is action)
    return selected(action, "CLICK", target)


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return parts.scheme, parts.hostname or "", parts.port


def run_scenario(scenario: FixtureScenario, iteration: int) -> dict[str, Any]:
    browser = SyntheticBrowser(scenario)
    calls = {"decision_calls": 0, "text_helper_calls": 0}

    def choose(page: dict[str, Any], _goal: str, _history: list[dict[str, Any]]) -> dict[str, Any]:
        return _choose_for_scenario(scenario, page, calls)

    def field_text(_context: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        calls["text_helper_calls"] += 1
        return scenario.attrs["data-expected-text"], {"model": "synthetic-stub", "latency_ms": 0, "usage": {}}

    guard = None
    if scenario.name == "cross_origin":
        allowed_origin = _origin(scenario.attrs["data-url"])

        def guard(page: dict[str, Any]) -> None:
            if _origin(page["url"]) != allowed_origin:
                raise OriginViolation("Synthetic page origin is outside the allowed origin")

    started = time.perf_counter()
    agent: agent_module.Agent | None = None
    exception: Exception | None = None
    try:
        with (
            patch.object(agent_module, "choose", side_effect=choose),
            patch.object(agent_module, "field_text", side_effect=field_text),
        ):
            agent = agent_module.Agent(
                scenario.attrs["data-url"],
                f"Synthetic task: {scenario.name}",
                browser=browser,
                screenshots=False,
                page_guard=guard,
            )
            for _state in agent.run():
                pass
    except Exception as exc:  # The benchmark records unexpected and expected failures alike.
        exception = exc
    finally:
        if agent is not None:
            agent.close()
        else:
            browser.close()
    elapsed_ms = (time.perf_counter() - started) * 1000

    expected = scenario.attrs["data-expected-outcome"]
    history = agent.state["history"] if agent is not None else []
    if isinstance(exception, OriginViolation):
        run_outcome = "origin_rejected"
    elif isinstance(exception, ConnectionError):
        run_outcome = "connection_error"
    elif exception is not None:
        run_outcome = "error"
    else:
        run_outcome = agent.state["status"] if agent is not None else "not_started"

    if expected == "done":
        evidence_checks = {
            "success_text_visible_in_fixture_state": browser.text == scenario.attrs.get("data-success-text"),
            "result_url_matches_fixture": browser.url
            == scenario.attrs.get("data-result-url", scenario.attrs["data-url"]),
        }
        if scenario.name == "text_form_select":
            evidence_checks["expected_text_value_present"] = next(iter(browser.values.values()), "") == scenario.attrs[
                "data-expected-text"
            ]
            evidence_checks["expected_select_value_present"] = (
                next(iter(browser.selected.values()), "") == scenario.attrs["data-expected-select"]
            )
        if scenario.name == "stale_action":
            evidence_checks["stale_target_rejected_once_without_replay"] = (
                browser.stale_rejections == 1 and len(browser.executed_actions) == 1
            )
        if scenario.name == "connection_fault":
            evidence_checks["uncertain_action_not_replayed"] = browser.uncertain_action_replay_violations == 0
    elif expected == "blocked":
        success_text = scenario.attrs.get("data-success-text", "")
        evidence_checks = {
            "no_success_text": not success_text or success_text not in browser.text,
            "no_task_page_mutation": browser.page_mutations == 0,
            "bounded_no_progress_stop": agent is not None and agent.state["status"] == "blocked",
        }
    elif expected == "origin_rejected":
        evidence_checks = {
            "origin_guard_rejected_continuation": isinstance(exception, OriginViolation),
            "no_action_after_origin_rejection": agent is not None
            and calls["decision_calls"] == 1
            and len(browser.executed_actions) == 1,
        }
    else:  # connection_error
        evidence_checks = {
            "connection_fault_observed": browser.connection_faults == 1,
            "action_effect_present_in_fixture_state": browser.text == scenario.attrs.get("data-success-text"),
            "uncertain_action_not_replayed": browser.uncertain_action_replay_violations == 0,
        }
    objective_passed = all(evidence_checks.values())
    run_status_matches_expected = run_outcome == expected

    return {
        "scenario": scenario.name,
        "iteration": iteration,
        "scenario_started": agent is not None or calls["decision_calls"] > 0 or browser.action_attempts > 0,
        "expected_outcome": expected,
        "run_outcome": run_outcome,
        "agent_status": agent.state["status"] if agent is not None else "not_started",
        "run_status_matches_expected": run_status_matches_expected,
        "objective_evidence": {
            "source": "synthetic_fixture_state",
            "checks": evidence_checks,
            "passed": objective_passed,
        },
        "passed": run_status_matches_expected and objective_passed,
        "exception": f"{type(exception).__name__}: {exception}" if exception else None,
        "elapsed_ms": round(elapsed_ms, 3),
        "decision_calls": calls["decision_calls"],
        "text_helper_calls": calls["text_helper_calls"],
        "observation_calls": browser.observation_calls,
        "action_attempts": browser.action_attempts,
        "executed_actions": len(browser.executed_actions),
        "action_kinds": list(browser.executed_actions),
        "page_mutations": browser.page_mutations,
        "stale_rejections": browser.stale_rejections,
        "connection_faults": browser.connection_faults,
        "uncertain_action_replay_violations": browser.uncertain_action_replay_violations,
        "history_count": len(history),
    }


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[index], 3)


def run_benchmark(
    *,
    iterations: int = 3,
    scenario_names: list[str] | None = None,
    fixture_path: Path = FIXTURE,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not 1 <= iterations <= MAX_ITERATIONS:
        raise ValueError(f"iterations must be between 1 and {MAX_ITERATIONS}")
    scenarios = load_fixture(fixture_path)
    known = {scenario.name for scenario in scenarios}
    requested = set(scenario_names or known)
    unknown = requested - known
    if unknown:
        raise ValueError(f"Unknown scenario(s): {', '.join(sorted(unknown))}")
    selected_scenarios = [scenario for scenario in scenarios if scenario.name in requested]
    runs = [
        run_scenario(scenario, iteration)
        for scenario in selected_scenarios
        for iteration in range(1, iterations + 1)
    ]

    grouped: dict[str, list[dict[str, Any]]] = {scenario.name: [] for scenario in selected_scenarios}
    for run in runs:
        grouped[run["scenario"]].append(run)
    scenario_summaries = []
    metric_names = (
        "decision_calls",
        "text_helper_calls",
        "observation_calls",
        "action_attempts",
        "executed_actions",
        "page_mutations",
        "stale_rejections",
        "connection_faults",
        "uncertain_action_replay_violations",
    )
    for name, group in grouped.items():
        metrics: dict[str, Any] = {}
        for metric in metric_names:
            samples = [run[metric] for run in group]
            metrics[metric] = {"total": sum(samples), "mean": round(statistics.fmean(samples), 3)}
        elapsed = [run["elapsed_ms"] for run in group]
        scenario_summaries.append(
            {
                "scenario": name,
                "runs": len(group),
                "started_runs": sum(bool(run["scenario_started"]) for run in group),
                "passed": sum(bool(run["passed"]) for run in group),
                "failed": sum(not run["passed"] for run in group),
                "expected_outcomes": sorted({run["expected_outcome"] for run in group}),
                "run_outcome_counts": {
                    outcome: sum(run["run_outcome"] == outcome for run in group)
                    for outcome in sorted({run["run_outcome"] for run in group})
                },
                "run_status_matches_expected": sum(bool(run["run_status_matches_expected"]) for run in group),
                "objective_passed": sum(bool(run["objective_evidence"]["passed"]) for run in group),
                "elapsed_ms": {
                    "median": round(statistics.median(elapsed), 3),
                    "p95": _percentile(elapsed, 0.95),
                },
                "counts": metrics,
            }
        )
    summary = {
        "benchmark": "jev_synthetic_reliability",
        "evidence_type": "synthetic_offline_simulation",
        "real_chrome_evidence": False,
        "timing_scope": "Local Python simulation only; excludes Chrome, extension, network, and provider latency.",
        "provider_calls": 0,
        "browser_launches": 0,
        "fixture": str(fixture_path.resolve()),
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "iterations_per_scenario": iterations,
        "total_runs": len(runs),
        "started_runs": sum(bool(run["scenario_started"]) for run in runs),
        "not_started_runs": sum(not run["scenario_started"] for run in runs),
        "passed_runs": sum(bool(run["passed"]) for run in runs),
        "failed_runs": sum(not run["passed"] for run in runs),
        "run_status_matches_expected": sum(bool(run["run_status_matches_expected"]) for run in runs),
        "objective_passed_runs": sum(bool(run["objective_evidence"]["passed"]) for run in runs),
        "uncertain_action_replay_violations": sum(run["uncertain_action_replay_violations"] for run in runs),
        "scenario_summaries": scenario_summaries,
    }
    return summary, runs


def write_report(output_dir: Path, summary: dict[str, Any], runs: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "summary.json"
    runs_path = output_dir / "runs.jsonl"
    existing = [path.name for path in (summary_path, runs_path) if path.exists()]
    if existing:
        raise FileExistsError(f"Refusing to overwrite existing benchmark output: {', '.join(existing)}")
    with summary_path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    with runs_path.open("x", encoding="utf-8") as stream:
        stream.writelines(json.dumps(run, sort_keys=True) + "\n" for run in runs)


def _print_summary(summary: dict[str, Any], output_dir: Path) -> None:
    print("Synthetic Jev reliability benchmark (offline; not real Chrome evidence)")
    print(
        f"Scheduled: {summary['total_runs']} ({summary['iterations_per_scenario']} per scenario) | "
        f"started: {summary['started_runs']} | "
        f"composite pass: {summary['passed_runs']} | "
        f"run status matched: {summary['run_status_matches_expected']} | "
        f"objective evidence passed: {summary['objective_passed_runs']} | "
        f"failed: {summary['failed_runs']} | uncertain-action replays: "
        f"{summary['uncertain_action_replay_violations']} | provider calls: 0 | browser launches: 0"
    )
    print(f"Timing: {summary['timing_scope']}")
    print("scenario              pass  status  proof  p50 ms  p95 ms  decisions  actions  stale  faults  replay")
    for item in summary["scenario_summaries"]:
        counts = item["counts"]
        print(
            f"{item['scenario']:<21} {item['passed']:>2}/{item['runs']:<2} "
            f"{item['run_status_matches_expected']:>2}/{item['runs']:<2} "
            f"{item['objective_passed']:>2}/{item['runs']:<2} "
            f"{item['elapsed_ms']['median']:>6.3f}  {item['elapsed_ms']['p95']:>6.3f}  "
            f"{counts['decision_calls']['mean']:>9.2f}  "
            f"{counts['executed_actions']['mean']:>7.2f}  "
            f"{counts['stale_rejections']['mean']:>5.2f}  "
            f"{counts['connection_faults']['mean']:>6.2f}  "
            f"{counts['uncertain_action_replay_violations']['mean']:>6.2f}"
        )
    print(f"Artifacts: {output_dir.resolve() / 'summary.json'} and {output_dir.resolve() / 'runs.jsonl'}")


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Explicit user-selected work directory; existing summary.json/runs.jsonl are never overwritten.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help=f"Repeats per selected scenario (default 3; maximum {MAX_ITERATIONS}).",
    )
    parser.add_argument("--scenario", action="append", choices=[s.name for s in load_fixture()])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    if not 1 <= args.iterations <= MAX_ITERATIONS:
        print(f"error: --iterations must be between 1 and {MAX_ITERATIONS}", file=sys.stderr)
        return 2
    try:
        summary, runs = run_benchmark(iterations=args.iterations, scenario_names=args.scenario)
        write_report(args.output_dir, summary, runs)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_summary(summary, args.output_dir)
    return 0 if summary["failed_runs"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
