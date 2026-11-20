"""The host that ANSWERED each call (OpenRouter's `provider` field), kept per span and per node, and what was ASKED for in the run's config (M1).
The real langchain_openai path runs; only the HTTP call is replaced. No network."""
from types import SimpleNamespace

import openai
import pytest
from langchain_core.messages import HumanMessage

import app.llm as llm_module
from app.config import Settings, get_settings
from app.llm import get_judge_llm, get_llm
from app.observability import TurnCollector, price_table

ENV_VARS = ("AGENT_PROVIDER", "JUDGE_PROVIDER", "FALLBACK_PROVIDER", "OPENROUTER_PROVIDER_ORDER", "OPENROUTER_ALLOW_FALLBACKS", "JUDGE_MODEL")


@pytest.fixture
def env(monkeypatch):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-real")
    monkeypatch.setattr(llm_module, "_warned", set())
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def raw_response(host="Alibaba", **extra):
    body = {"id": "gen-1", "object": "chat.completion", "created": 1, "model": "qwen/qwen3.8-27b",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.0001},
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]}
    if host is not None:
        body["provider"] = host
    return {**body, **extra}


def call(llm, response, collector=None):
    raw = SimpleNamespace(parse=lambda: response, headers={})
    llm.client = SimpleNamespace(with_raw_response=SimpleNamespace(create=lambda **_kw: raw))
    collector = collector or TurnCollector(prices=price_table())
    result = llm.invoke([HumanMessage(content="hi")], config={"callbacks": [collector]})
    return collector, result


def test_the_host_of_a_dict_response_reaches_the_message_and_the_span(env):
    collector, message = call(get_judge_llm(), raw_response("Alibaba"))
    assert message.response_metadata["host"] == "Alibaba"
    (span,) = collector.llm_spans()
    assert span["host"] == "Alibaba"


def test_the_host_of_a_typed_openai_response_is_kept_too(env):
    typed = openai.types.chat.ChatCompletion.model_validate(raw_response("DeepInfra"))  # `provider` is an extra field of the model
    collector, message = call(get_llm(model="openai/gpt-oss-120b"), typed)
    assert message.response_metadata["host"] == "DeepInfra" and collector.llm_spans()[0]["host"] == "DeepInfra"


def test_a_response_without_provider_has_no_host_and_nothing_is_invented(env):
    collector, message = call(get_judge_llm(), raw_response(host=None))
    assert "host" not in message.response_metadata and collector.llm_spans()[0]["host"] is None
    assert collector.usage()["by_node"][0]["host"] is None


@pytest.mark.parametrize("bad", [123, "", ["x"]])
def test_a_malformed_provider_field_is_ignored(env, bad):
    collector, _ = call(get_judge_llm(), raw_response(host=None, provider=bad))
    assert collector.llm_spans()[0]["host"] is None


def test_the_host_does_not_change_what_was_already_recorded(env):
    collector, _ = call(get_judge_llm(), raw_response("Alibaba"))
    (span,) = collector.llm_spans()
    assert span["cost_usd"] == pytest.approx(0.0001) and span["cost_source"] == "provider"
    assert (span["input_tokens"], span["output_tokens"]) == (10, 5)


def span(node, host, ms, model="m"):
    return {"kind": "llm", "node": node, "name": model, "host": host, "latency_ms": ms, "input_tokens": 1, "output_tokens": 1,
            "cost_usd": 0.1, "cost_source": "provider", "ok": True}


def test_by_node_has_one_group_per_node_model_and_host_with_its_latency():
    c = TurnCollector(prices=price_table())
    c.spans = [span("knowledge", "DeepInfra", 1500), span("knowledge", "DeepInfra", 500), span("knowledge", "Groq", 300), span("router", None, 100)]
    groups = {(g["node"], g["host"]): g for g in c.usage()["by_node"]}
    assert set(groups) == {("knowledge", "DeepInfra"), ("knowledge", "Groq"), ("router", None)}
    assert groups[("knowledge", "DeepInfra")]["calls"] == 2 and groups[("knowledge", "DeepInfra")]["latency_ms"] == 2000
    assert groups[("knowledge", "Groq")]["latency_ms"] == 300 and groups[("router", None)]["host"] is None


def test_the_eval_summary_lists_the_hosts_that_answered_per_model():
    from eval.run_eval import usage_summary

    rows = [{"usage": {"total_tokens": 4, "cost_usd": 0.2, "by_node": [{"node": "router", "model": "m", "host": "DeepInfra", "calls": 1, "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.1},
                                   {"node": "knowledge", "model": "m", "host": "Groq", "calls": 1, "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.1}]},
             "judge_usage": {"by_node": [{"node": "judge", "model": "j", "host": None, "calls": 1, "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.1}]}}]
    models = {(m["role"], m["model"]): m for m in usage_summary(rows)["tokens_by_model"]}
    assert models[("agents", "m")]["hosts"] == ["DeepInfra", "Groq"] and models[("judge", "j")]["hosts"] == []


def test_the_run_config_says_what_was_asked_for_as_plain_values(env, monkeypatch):
    from eval.run_eval import run_config

    monkeypatch.setenv("OPENROUTER_PROVIDER_ORDER", '{"openai/gpt-oss-120b": ["groq"], "qwen/qwen3.8-27b": ["alibaba"]}')
    get_settings.cache_clear()
    cfg = run_config()
    assert cfg["agent_provider"] == "openrouter" and cfg["judge_provider"] == "openrouter" and cfg["openrouter_allow_fallbacks"] is False
    assert cfg["openrouter_provider_order"] == "openai/gpt-oss-120b=groq; qwen/qwen3.8-27b=alibaba"
    assert all(isinstance(v, (str, bool, int)) for v in cfg.values())  # flat values, like the keys it already had (no dict or list)


def test_the_report_has_a_hosts_column():
    from eval.run_eval import render_report

    summary = {"cases": 1, "tokens_by_model": [{"role": "agents", "model": "m", "calls": 1, "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.1,
                                                "hosts": ["DeepInfra"]},
                                               {"role": "judge", "model": "j", "calls": 1, "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.1, "hosts": []}]}
    text = render_report([], summary, "stamp")
    assert "| hosts that answered |" in text and "| DeepInfra |" in text and "| - |" in text
