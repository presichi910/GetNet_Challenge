"""Tests for the evaluator."""
import pytest

from eval.judge import parse_judgement
from eval.run_eval import load_cases, pct, routing_ok, summarize, tools_ok


def test_dataset_covers_all_challenge_scenarios():
    ids = [c["id"] for c in load_cases(None)]
    assert len([i for i in ids if i.startswith("q")]) == 10
    assert len([i for i in ids if i.startswith("h")]) == 3  # handoff
    assert all({"id", "user_id", "message", "expected_agents"} <= set(c) for c in load_cases(None))


def test_routing_ok_allows_optional_but_not_missing_or_extra():
    case = {"expected_agents": ["support"], "optional_agents": ["knowledge"]}
    assert routing_ok(case, ["support"]) and routing_ok(case, ["support", "knowledge"])
    assert not routing_ok(case, ["knowledge"])
    assert not routing_ok({"expected_agents": [], "optional_agents": []}, ["knowledge"])


def test_tools_ok_is_subset_check():
    assert tools_ok({"expected_tools": ["a"]}, ["a", "b"]) and not tools_ok({"expected_tools": ["a"]}, ["b"])


def test_parse_judgement_handles_wrapped_json_and_clamps():
    j = parse_judgement('Sure! {"relevance": 9, "groundedness": 4, "completeness": 3, "tone": 5, "verdict": "pass", "reason": "ok"} done')
    assert j["relevance"] == 5 and j["verdict"] == "pass"
    with pytest.raises(ValueError):
        parse_judgement("no json")


def test_summarize_and_percentile():
    rows = [
        {"routing_ok": True, "tools_ok": True, "latency_ms": 100, "judge": {"relevance": 5, "groundedness": 4, "completeness": 4, "tone": 5, "verdict": "pass"}},
        {"routing_ok": False, "tools_ok": True, "latency_ms": 300, "degraded": True, "judge": {"relevance": 3, "groundedness": 2, "completeness": 3, "tone": 4, "verdict": "fail"}},
    ]
    s = summarize(rows)
    assert s["routing_accuracy"] == 0.5 and s["tool_use_accuracy"] == 1 and s["degraded_rate"] == 0.5
    assert s["avg_relevance"] == 4 and s["judge_pass_rate"] == 0.5
    assert pct([1, 2, 3, 4, 5], 0.5) == 3


def test_handoff_ok_and_summary_metrics():
    from eval.run_eval import handoff_ok

    assert handoff_ok({"expect_handoff": True}, "user_requested") is True
    assert handoff_ok({"expect_handoff": True}, None) is False     # missed
    assert handoff_ok({"expect_handoff": False}, "agent_unresolved") is False  # false alarm
    assert handoff_ok({}, "agent_unresolved") is None              # no expectation
    rows = [{"routing_ok": True, "tools_ok": True, "latency_ms": 1, "handoff": "user_requested", "handoff_ok": True},
            {"routing_ok": True, "tools_ok": True, "latency_ms": 1, "handoff": None, "handoff_ok": False},
            {"routing_ok": True, "tools_ok": True, "latency_ms": 1, "handoff": "x", "handoff_ok": None}]
    s = summarize(rows)
    assert s["handoff_accuracy"] == 0.5 and round(s["handoff_rate"], 2) == 0.67
