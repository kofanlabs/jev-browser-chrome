"""Offline tests for the synthetic reliability benchmark. No Chrome or APIs."""

import json

import pytest

from jev_ultrafast.browser import StalePage
from scripts.benchmark_reliability import (
    FIXTURE,
    SyntheticBrowser,
    load_fixture,
    run_benchmark,
    write_report,
)


def _runs_by_scenario(runs):
    return {run["scenario"]: run for run in runs}


def test_fixture_scenarios_run_through_real_agent_loop_offline():
    summary, runs = run_benchmark(iterations=2)

    assert summary["evidence_type"] == "synthetic_offline_simulation"
    assert summary["real_chrome_evidence"] is False
    assert summary["provider_calls"] == summary["browser_launches"] == 0
    assert summary["total_runs"] == 16
    assert summary["started_runs"] == 16
    assert summary["not_started_runs"] == 0
    assert summary["passed_runs"] == 16
    assert summary["failed_runs"] == 0
    assert summary["run_status_matches_expected"] == 16
    assert summary["objective_passed_runs"] == 16
    assert all(run["passed"] for run in runs)


def test_delayed_text_form_select_and_no_op_metrics_are_actionable():
    _summary, runs = run_benchmark(
        iterations=1,
        scenario_names=["delayed_change", "text_form_select", "no_op"],
    )
    cases = _runs_by_scenario(runs)

    assert "wait" in cases["delayed_change"]["action_kinds"]
    form = cases["text_form_select"]
    assert form["action_kinds"] == ["fill", "select", "click"]
    assert form["text_helper_calls"] == 1
    no_op = cases["no_op"]
    assert no_op["run_outcome"] == "blocked"
    assert no_op["objective_evidence"]["passed"]
    assert no_op["action_kinds"] == ["click", "click", "click"]
    assert no_op["page_mutations"] == 0


def test_stale_rejection_retries_without_double_mutation():
    _summary, runs = run_benchmark(iterations=1, scenario_names=["stale_action"])
    run = runs[0]

    assert run["passed"]
    assert run["stale_rejections"] == 1
    assert run["action_attempts"] == 2
    assert run["executed_actions"] == run["history_count"] == 1
    assert run["page_mutations"] == 1


def test_cross_origin_and_connection_faults_are_reported_without_retry():
    _summary, runs = run_benchmark(iterations=1, scenario_names=["cross_origin", "connection_fault"])
    cases = _runs_by_scenario(runs)

    cross_origin = cases["cross_origin"]
    assert cross_origin["passed"]
    assert cross_origin["run_outcome"] == "origin_rejected"
    assert cross_origin["decision_calls"] == 1
    assert cross_origin["executed_actions"] == 1

    connection = cases["connection_fault"]
    assert connection["passed"]
    assert connection["run_outcome"] == "connection_error"
    assert connection["connection_faults"] == 1
    assert connection["executed_actions"] == connection["history_count"] == 1


def test_report_writes_only_to_explicit_directory_and_refuses_overwrite(tmp_path):
    summary, runs = run_benchmark(iterations=1, scenario_names=["immediate_click"])
    write_report(tmp_path, summary, runs)

    saved = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert saved["total_runs"] == 1
    assert len((tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        write_report(tmp_path, summary, runs)


def test_iteration_limit_fixture_and_scenario_names_are_checked():
    assert len(load_fixture(FIXTURE)) == 8
    with pytest.raises(ValueError, match="iterations"):
        run_benchmark(iterations=101)
    with pytest.raises(ValueError, match="Unknown scenario"):
        run_benchmark(iterations=1, scenario_names=["real_chrome"])


def test_uncertain_connection_action_replay_is_counted_as_a_violation():
    scenario = next(item for item in load_fixture(FIXTURE) if item.name == "connection_fault")
    browser = SyntheticBrowser(scenario)
    page = browser.observe()
    action = next(item for item in page["actions"] if item["kind"] == "click")

    browser.act(action, page)
    with pytest.raises(ConnectionError):
        browser.observe()
    with pytest.raises(StalePage):
        browser.act(action, page)

    assert browser.executed_actions == ["click"]
    assert browser.uncertain_action_replay_violations == 1
