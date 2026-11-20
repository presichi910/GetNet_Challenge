"""The cost of a call on OpenRouter (plan 9.6, M1): what the provider charged beats every table, and an estimate uses the price of the
pinned HOST, not the listed price of the model. The real langchain_openai path runs (payload, result, callbacks); only the HTTP call is replaced."""
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

import app.llm as llm_module
from app.config import Settings, get_settings
from app.llm import get_judge_llm
from app.observability import TurnCollector, price_table

OR_120 = "openai/gpt-oss-120b"
ENV_VARS = ("AGENT_PROVIDER", "JUDGE_PROVIDER", "FALLBACK_PROVIDER", "OPENROUTER_PROVIDER_ORDER", "OPENROUTER_ALLOW_FALLBACKS",
            "LLM_MODEL", "LIGHT_MODEL", "SUPPORT_MODEL", "KNOWLEDGE_MODEL", "SYNTH_MODEL", "JUDGE_MODEL", "REASONING_EFFORT")


@pytest.fixture
def env(monkeypatch):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-real")
    monkeypatch.setattr(llm_module, "_warned", set())

    def apply(**values):
        for k, v in values.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()

    apply()
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    yield apply
    get_settings.cache_clear()


def fake_response(cost=None, prompt=1500, completion=100):
    usage = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
    if cost is not None:
        usage["cost"] = cost
    return {"id": "gen-1", "object": "chat.completion", "model": "qwen/qwen3.8-27b", "usage": usage,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}]}


def run_judge_call(env, response):
    """The REAL langchain_openai path (payload, result, callbacks) with only the HTTP call replaced."""
    judge = get_judge_llm()
    raw = SimpleNamespace(parse=lambda: response, headers={})
    judge.client = SimpleNamespace(with_raw_response=SimpleNamespace(create=lambda **_kw: raw))
    collector = TurnCollector(prices=price_table())
    judge.invoke([HumanMessage(content="hi")], config={"callbacks": [collector]})
    return collector


def test_the_cost_the_provider_reports_is_recorded_as_it_is(env):
    c = run_judge_call(env, fake_response(cost=0.0012345))
    (span,) = c.llm_spans()
    assert span["cost_usd"] == pytest.approx(0.0012345) and span["cost_source"] == "provider"
    assert (span["input_tokens"], span["output_tokens"]) == (1500, 100)
    usage = c.usage()
    assert usage["cost_usd"] == pytest.approx(0.0012345) and usage["cost_source"] == "provider"
    assert usage["by_node"][0]["cost_source"] == "provider" and "sources" not in usage["by_node"][0]


def test_without_a_reported_cost_the_estimate_uses_the_price_of_the_pinned_host_not_the_listed_or_groqs(env):
    c = run_judge_call(env, fake_response(cost=None))
    (span,) = c.llm_spans()
    alibaba = (1500 * 0.425 + 100 * 2.55) / 1e6
    groq = (1500 * 0.80 + 100 * 4.00) / 1e6  # the price the old table had for this model id
    assert span["cost_usd"] == pytest.approx(alibaba) and span["cost_usd"] != pytest.approx(groq)
    assert span["cost_source"] == "estimated" and c.usage()["cost_source"] == "estimated"


def test_a_model_with_no_price_and_no_reported_cost_is_null_never_zero(env):
    env(OPENROUTER_PROVIDER_ORDER='{"x/unknown": ["somewhere"]}', JUDGE_MODEL="x/unknown")
    c = run_judge_call(env, fake_response(cost=None))
    (span,) = c.llm_spans()
    assert span["cost_usd"] is None and span["cost_source"] == "unpriced" and c.usage()["cost_usd"] is None


@pytest.mark.parametrize("bad", [True, -1, "0.5", None])
def test_a_malformed_cost_is_ignored(env, bad):
    resp = fake_response(cost=None)
    resp["usage"]["cost"] = bad
    (span,) = run_judge_call(env, resp).llm_spans()
    assert span["cost_source"] == "estimated"


def test_cost_source_is_mixed_when_only_some_calls_report_a_cost():
    c = TurnCollector(prices=price_table())
    c.spans = [{"kind": "llm", "node": "router", "name": "a", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.1, "cost_source": "provider", "ok": True},
               {"kind": "llm", "node": "router", "name": "a", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.2, "cost_source": "estimated", "ok": True}]
    usage = c.usage()
    assert usage["cost_source"] == "mixed" and usage["by_node"][0]["cost_source"] == "mixed"
    assert usage["cost_usd"] == pytest.approx(0.3)


def test_the_price_table_prices_the_two_pinned_hosts():
    table = price_table()
    assert table[f"{OR_120}@deepinfra/bf16"] == (0.037, 0.17) and table["qwen/qwen3.8-27b@alibaba"] == (0.425, 2.55)
