"""The eval records tokens and cost per case (agents and judge) and sums them per model. No network."""
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from eval import run_eval
from eval.run_eval import evaluate, render_report, summarize

CASE = {"id": "q1", "user_id": "u", "message": "hi", "expected_agents": ["support"], "expected_tools": []}


def emit(collector, node, model, i, o):
    rid = uuid4()
    collector.on_chat_model_start({}, [[]], run_id=rid,
                                  metadata={"langgraph_checkpoint_ns": f"{node}:x", "ls_model_name": model})
    msg = AIMessage(content="", usage_metadata={"input_tokens": i, "output_tokens": o, "total_tokens": i + o})
    collector.on_llm_end(LLMResult(generations=[[ChatGeneration(message=msg)]]), run_id=rid)


class Service:
    def run_raw(self, message, user_id, thread_id=None, callbacks=None, **kw):
        emit(callbacks[0], "router", "m-light", 100, 10)
        emit(callbacks[0], "support", "m-big", 1000, 100)
        state = {"plan": ["support"], "final_answer": "ok",
                 "agent_outputs": {"support": {"answer": "ok", "error": None, "tool_calls": [], "contexts": []}}}
        return state, 5


class Judge:
    def score(self, q, a, ev, criteria="", callbacks=None):
        emit(callbacks[0], "unknown", "m-judge", 500, 50)
        return {"relevance": 5, "groundedness": 5, "completeness": 5, "tone": 5, "verdict": "pass", "reason": ""}


@pytest.fixture(autouse=True)
def prices(monkeypatch):
    monkeypatch.setattr(run_eval, "_prices", lambda: {"m-light": (1.0, 2.0), "m-big": (2.0, 4.0)})


def test_each_row_has_its_tokens_and_the_summary_sums_them_per_model():
    rows = evaluate([CASE, {**CASE, "id": "q2"}], Service(), Judge(), pause=0)
    assert rows[0]["usage"]["total_tokens"] == 1210
    assert rows[0]["usage"]["cost_usd"] == pytest.approx((100 * 1 + 10 * 2 + 1000 * 2 + 100 * 4) / 1e6)
    assert rows[0]["judge_usage"]["total_tokens"] == 550
    s = summarize(rows)
    assert s["agent_tokens"] == 2420 and s["judge_tokens"] == 1100 and s["agent_tokens_per_case_mean"] == 1210
    assert s["agent_cost_usd"] == pytest.approx(2 * 2520 / 1e6)
    assert s["judge_cost_usd"] is None and s["total_cost_usd"] is None  # the judge model has no price here
    models = {(m["role"], m["model"]): m for m in s["tokens_by_model"]}
    assert models[("agents", "m-big")]["calls"] == 2 and models[("judge", "m-judge")]["input_tokens"] == 1000
    text = render_report(rows, s, "stamp")
    assert "## Tokens and estimated cost per model" in text
    assert "| judge | `m-judge` | 2 | 1000 | 100 | n/a |" in text
    assert "| 5 | 1210 | $0.0025 |" in text  # latency, tokens and cost of the case row


def test_a_service_and_judge_without_callbacks_still_work():
    class Old:
        def run_raw(self, message, user_id, thread_id=None):
            return {"plan": [], "final_answer": "ok", "agent_outputs": {}}, 5

    class OldJudge:
        def score(self, q, a, ev, criteria=""):
            return {"relevance": 5, "groundedness": 5, "completeness": 5, "tone": 5, "verdict": "pass", "reason": ""}

    rows = evaluate([CASE], Old(), OldJudge(), pause=0)
    assert "usage" not in rows[0] and "agent_tokens" not in summarize(rows)
