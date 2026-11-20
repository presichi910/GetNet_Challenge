"""OpenRouter provider (plan 9.6, M1): construction per role, host pinning, reasoning, the judge, the real cost. No network."""
import logging
import re
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables.fallbacks import RunnableWithFallbacks
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI

import app.llm as llm_module
from app.config import Settings, get_settings
from app.llm import OPENROUTER_BASE_URL, fallback_model_name, get_judge_llm, get_llm, openrouter_model_name

ROOT = Path(__file__).resolve().parent.parent
OR_120 = "openai/gpt-oss-120b"
FW_120 = "accounts/fireworks/models/gpt-oss-120b"
ENV_VARS = ("AGENT_PROVIDER", "JUDGE_PROVIDER", "FALLBACK_PROVIDER", "OPENROUTER_PROVIDER_ORDER", "OPENROUTER_ALLOW_FALLBACKS",
            "LLM_MODEL", "LIGHT_MODEL", "SUPPORT_MODEL", "KNOWLEDGE_MODEL", "SYNTH_MODEL", "JUDGE_MODEL", "REASONING_EFFORT")


@pytest.fixture
def env(monkeypatch):
    """Start from the code defaults (the developer's .env must not leak in), with fake keys, and a fresh set of one-time warnings."""
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-real")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_not_real")
    monkeypatch.setattr(llm_module, "_warned", set())

    def apply(**values):
        for k, v in values.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()

    apply()
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    yield apply
    get_settings.cache_clear()


def payload(llm, **kw):
    return llm._get_request_payload([HumanMessage(content="hi")], **kw)


# ---- defaults: code, .env.example and compose say the same thing ----

def test_the_code_defaults_are_openrouter_and_gpt_oss_120b():
    s = Settings(_env_file=None)
    assert (s.agent_provider, s.judge_provider) == ("openrouter", "openrouter")
    assert (s.llm_model, s.light_model, s.effective_support_model) == (OR_120, OR_120, OR_120)
    assert s.fallback_provider == "" and s.openrouter_allow_fallbacks is False
    assert s.openrouter_provider_order == {OR_120: ["deepinfra/bf16"], "qwen/qwen3.8-27b": ["alibaba"]}


def test_env_example_and_compose_agree_with_the_code_defaults():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for var in ("AGENT_PROVIDER", "JUDGE_PROVIDER"):
        assert re.search(rf"^{var}=openrouter\s*$", example, re.M), var
        assert f"{var}: ${{{var}:-openrouter}}" in compose, var
    for var in ("LLM_MODEL", "LIGHT_MODEL", "SUPPORT_MODEL"):
        assert re.search(rf"^{var}={OR_120}\s*$", example, re.M), var
        assert f"{var}: ${{{var}:-{OR_120}}}" in compose, var
    assert re.search(r"^OPENROUTER_API_KEY=\s*$", example, re.M)
    assert re.search(r"^KNOWLEDGE_MODEL=\s*$", example, re.M) and re.search(r"^SYNTH_MODEL=\s*$", example, re.M)


def test_no_agent_model_is_a_qwen_and_the_judge_is():
    """The judge is another family than the agents: no role of an agent may name a Qwen, in the code, .env.example or compose."""
    s = Settings(_env_file=None)
    for name in (s.llm_model, s.light_model, s.effective_support_model, s.effective_knowledge_model, s.effective_synth_model):
        assert "qwen" not in name.lower(), name
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for var in ("LLM_MODEL", "LIGHT_MODEL", "SUPPORT_MODEL", "KNOWLEDGE_MODEL", "SYNTH_MODEL"):
        for m in re.finditer(rf"^{var}=(.*)$", example, re.M):
            assert "qwen" not in m.group(1).lower(), (var, "in .env.example")
        for m in re.finditer(rf"^\s*{var}:\s*\$\{{{var}:-([^}}]*)\}}", compose, re.M):
            assert "qwen" not in m.group(1).lower(), (var, "in docker-compose.yml")
    assert "qwen" in s.judge_model.lower()


def test_empty_knowledge_and_synth_models_are_the_models_of_before():
    s = Settings(_env_file=None, llm_model="m-llm", light_model="m-light")
    assert (s.effective_knowledge_model, s.effective_synth_model) == ("m-llm", "m-light")
    s = Settings(_env_file=None, llm_model="m-llm", light_model="m-light", knowledge_model="m-k", synth_model="m-s")
    assert (s.effective_knowledge_model, s.effective_synth_model) == ("m-k", "m-s")


# ---- the agents ----

def test_an_agent_is_chatopenai_on_openrouter_pinned_to_its_host_with_no_fallback(env):
    llm = get_llm(model=OR_120, max_tokens=400)
    assert isinstance(llm, ChatOpenAI) and not isinstance(llm, ChatGroq)
    assert llm.openai_api_base == OPENROUTER_BASE_URL and llm.model_name == OR_120
    assert llm.openai_api_key.get_secret_value() == "sk-or-test-not-real"
    assert payload(llm)["extra_body"] == {"provider": {"allow_fallbacks": False, "order": ["deepinfra/bf16"]},
                                          "reasoning": {"effort": "low"}}
    assert llm.metadata["price_key"] == f"{OR_120}@deepinfra/bf16"


def test_reasoning_effort_and_fallback_flag_come_from_settings(env):
    env(REASONING_EFFORT="medium", OPENROUTER_ALLOW_FALLBACKS="true")
    assert payload(get_llm(model=OR_120))["extra_body"] == {"provider": {"allow_fallbacks": True, "order": ["deepinfra/bf16"]},
                                                            "reasoning": {"effort": "medium"}}


def test_the_host_map_can_be_replaced_and_a_model_missing_from_it_is_unpinned_with_one_warning(env, caplog):
    env(OPENROUTER_PROVIDER_ORDER='{"anthropic/claude-haiku-5.5": ["anthropic"]}')
    assert payload(get_llm(model="anthropic/claude-haiku-5.5"))["extra_body"]["provider"] == {"allow_fallbacks": False, "order": ["anthropic"]}
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        first = get_llm(model=OR_120)
        get_llm(model=OR_120)  # the second call must not repeat the warning
    assert "order" not in payload(first)["extra_body"]["provider"]
    warnings = [r for r in caplog.records if "no host" in r.getMessage()]
    assert len(warnings) == 1 and OR_120 in warnings[0].getMessage()
    assert first.metadata["price_key"] == f"{OR_120}@openrouter"


def test_a_fireworks_id_is_translated_and_the_translation_is_logged(env, caplog):
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        llm = get_llm(model=FW_120)
        get_llm(model=FW_120)
    assert llm.model_name == OR_120
    assert payload(llm)["extra_body"]["provider"]["order"] == ["deepinfra/bf16"]  # the translated id gets its host
    logged = [r.getMessage() for r in caplog.records if "Fireworks id" in r.getMessage()]
    assert len(logged) == 1 and FW_120 in logged[0] and OR_120 in logged[0]
    assert openrouter_model_name(OR_120) == OR_120  # an OpenRouter id is not touched and logs nothing


def test_an_unknown_agent_provider_is_still_an_error(env):
    env(AGENT_PROVIDER="openai")
    with pytest.raises(ValueError, match="AGENT_PROVIDER"):
        get_llm(model=OR_120)


# ---- the judge ----

def test_the_judge_is_qwen_on_openrouter_pinned_to_alibaba_with_reasoning_off(env):
    judge = get_judge_llm()
    assert isinstance(judge, ChatOpenAI) and judge.openai_api_base == OPENROUTER_BASE_URL
    assert judge.model_name == "qwen/qwen3.8-27b" and judge.max_tokens == 500 and judge.temperature == 0
    assert payload(judge)["extra_body"] == {"provider": {"allow_fallbacks": False, "order": ["alibaba"]}, "reasoning": {"effort": "none"}}
    assert judge.metadata["price_key"] == "qwen/qwen3.8-27b@alibaba"


def test_the_judge_does_not_depend_on_the_agent_provider(env):
    env(AGENT_PROVIDER="groq")
    assert isinstance(get_judge_llm(), ChatOpenAI)


def test_the_judge_can_still_run_on_groq(env):
    env(JUDGE_PROVIDER="groq")
    judge = get_judge_llm()
    assert isinstance(judge, ChatGroq) and judge.model_name == "qwen/qwen3.8-27b" and judge.max_tokens == 500


def test_an_unknown_judge_provider_is_an_error(env):
    env(JUDGE_PROVIDER="fireworks")
    with pytest.raises(ValueError, match="JUDGE_PROVIDER"):
        get_judge_llm()


def test_the_eval_judge_class_uses_the_openrouter_judge(env):
    from eval.judge import Judge

    assert isinstance(Judge().llm, ChatOpenAI)


# ---- the two roles that the model comparison changes ----

def test_the_knowledge_and_synthesizer_nodes_ask_for_their_own_models(env, monkeypatch):
    from app.agents import knowledge, synthesizer

    asked = {}

    class Stub:
        def invoke(self, *_a, **_k):
            return AIMessage(content="hello")

    monkeypatch.setattr(knowledge, "get_llm", lambda **kw: asked.setdefault("knowledge", kw.get("model")) or Stub())
    monkeypatch.setattr(knowledge, "run_tool_agent", lambda *a, **k: {"answer": "x"})
    monkeypatch.setattr(synthesizer, "get_llm", lambda **kw: (asked.setdefault("synth", kw.get("model")), Stub())[1])
    state = {"messages": [HumanMessage(content="hi")], "plan": [], "agent_outputs": {}}

    for k, s in (({}, ("openai/gpt-oss-120b", "openai/gpt-oss-120b")),
                 ({"KNOWLEDGE_MODEL": "m-know", "SYNTH_MODEL": "m-synth"}, ("m-know", "m-synth"))):
        asked.clear()
        env(**k)
        knowledge.make_knowledge_node()(state)
        synthesizer.make_synthesizer_node()(state)
        assert (asked["knowledge"], asked["synth"]) == s


# ---- failover with OpenRouter ----

def test_fallback_model_names_for_openrouter():
    assert fallback_model_name(FW_120, "openrouter") == OR_120
    assert fallback_model_name(OR_120, "openrouter") == OR_120
    assert fallback_model_name(OR_120, "fireworks") == FW_120


def test_openrouter_primary_with_groq_backup_is_wrapped_and_groq_gets_its_own_id(env):
    env(FALLBACK_PROVIDER="groq")
    model = get_llm(model=OR_120)
    assert isinstance(model, RunnableWithFallbacks)
    assert isinstance(model.runnable, ChatOpenAI) and isinstance(model.fallbacks[0], ChatGroq)
    assert model.fallbacks[0].model_name == OR_120  # Groq's id for gpt-oss-120b is the same string


def test_groq_primary_with_openrouter_backup_is_wrapped(env):
    env(AGENT_PROVIDER="groq", FALLBACK_PROVIDER="openrouter")
    model = get_llm(model=OR_120)
    assert isinstance(model, RunnableWithFallbacks) and isinstance(model.fallbacks[0], ChatOpenAI)
