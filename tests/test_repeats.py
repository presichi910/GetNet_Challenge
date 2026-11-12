"""--repeats: spread of the metrics over N runs, pass count per case, unstable cases. No network."""
import pytest

from eval import repeats
from eval import run_eval
from eval.repeats import aggregate, case_passes, render, spread

OK = {"routing_ok": True, "tools_ok": True, "handoff_ok": True, "latency_ms": 10,
      "judge": {"verdict": "pass", "relevance": 5, "groundedness": 5, "completeness": 5, "tone": 5}}


def test_case_passes_needs_every_check():
    assert case_passes(OK) is True
    assert case_passes({**OK, "routing_ok": False}) is False
    assert case_passes({**OK, "handoff_ok": False}) is False
    assert case_passes({**OK, "handoff_ok": None}) is True  # no expectation
    assert case_passes({**OK, "judge": {"verdict": "fail"}}) is False
    assert case_passes({**OK, "judge": {"verdict": "error"}}) is True  # a judge outage is not the system's fault
    assert case_passes({"id": "x", "provider_error": "router: RateLimitError"}) is None
    assert case_passes({"id": "x", "error": "boom"}) is False


def test_spread_is_mean_min_max_per_metric():
    s = spread([{"cases": 3, "routing_accuracy": 1.0, "avg_tone": 5}, {"cases": 3, "routing_accuracy": 0.5, "avg_tone": 4},
                {"cases": 3, "routing_accuracy": 0.75, "avg_tone": 3, "tokens_by_model": []}])
    assert s["routing_accuracy"] == {"mean": pytest.approx(0.75), "min": 0.5, "max": 1.0, "n": 3}
    assert s["avg_tone"]["mean"] == pytest.approx(4) and "cases" not in s and "tokens_by_model" not in s


def test_aggregate_counts_passes_and_finds_unstable_and_always_failing_cases():
    run1 = [{**OK, "id": "a"}, {**OK, "id": "b"}, {**OK, "id": "c", "routing_ok": False}, {**OK, "id": "h", "holdout": True}]
    run2 = [{**OK, "id": "a"}, {**OK, "id": "b", "judge": {**OK["judge"], "verdict": "fail"}}, {**OK, "id": "c", "routing_ok": False},
            {"id": "h", "holdout": True, "provider_error": "x"}]
    run3 = [{**OK, "id": "a"}, {**OK, "id": "b"}, {**OK, "id": "c", "tools_ok": False}, {**OK, "id": "h", "holdout": True}]
    runs = [run1, run2, run3]
    agg = aggregate(runs, [run_eval.summarize_all(r) for r in runs])
    assert agg["cases"]["a"] == {"passed": 3, "ran": 3, "provider_errors": 0, "holdout": False}
    assert agg["cases"]["b"]["passed"] == 2 and agg["unstable"] == ["b"]
    assert agg["always_fail"] == ["c"]
    assert agg["cases"]["h"] == {"passed": 2, "ran": 2, "provider_errors": 1, "holdout": True}
    assert agg["provider_errors"] == 1
    assert agg["regular"]["routing_accuracy"]["min"] == pytest.approx(2 / 3)
    assert agg["holdout"]["routing_accuracy"]["n"] == 2  # the run where h hit a provider error has no holdout metrics
    text = render(agg, "stamp")
    assert "| b | 2/3 | 0 |" in text and "| h (holdout) | 2/2 | 1 |" in text
    assert "**Unstable (pass in some runs, not all):** b" in text


def test_run_repeats_writes_one_report_per_run_and_only_the_first_may_become_latest(tmp_path, monkeypatch):
    calls = []

    def fake_write_report(rows, summary, models=None, update_latest=True):
        calls.append(update_latest)
        return tmp_path / f"run{len(calls)}.md"

    monkeypatch.setattr(run_eval, "write_report", fake_write_report)
    monkeypatch.setattr(repeats, "ROOT", tmp_path)

    class Service:
        def run_raw(self, message, user_id, thread_id=None):
            return {"plan": ["support"], "final_answer": "ok",
                    "agent_outputs": {"support": {"answer": "ok", "error": None, "tool_calls": [], "contexts": []}}}, 5

    case = {"id": "q1", "user_id": "u", "message": "hi", "expected_agents": ["support"]}
    done, path = run_eval.run_repeats([case], Service(), None, 3, pause=0)
    assert len(done) == 3 and calls == [True, False, False]
    assert path is not None and path.name.startswith("REPEATS-") and "| q1 | 3/3 | 0 |" in path.read_text(encoding="utf-8")
    calls.clear()
    done, path = run_eval.run_repeats([case], Service(), None, 1, pause=0, update_latest=False)
    assert calls == [False] and path is None
