"""AGENT_PROVIDER picks the agents' client; the judge always stays on Groq. No network."""
import pytest
from langchain_core.messages import HumanMessage
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI

from app.config import Settings, get_settings
from app.llm import FIREWORKS_BASE_URL, get_judge_llm, get_llm

FW_120 = "accounts/fireworks/models/gpt-oss-120b"


@pytest.fixture
def provider(monkeypatch):
    def apply(name: str | None, **env):
        if name is None:
            monkeypatch.delenv("AGENT_PROVIDER", raising=False)
        else:
            monkeypatch.setenv("AGENT_PROVIDER", name)
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_not_real")
        monkeypatch.setenv("FIREWORKS_API_KEY", "fw_test_not_real")
        monkeypatch.setenv("REASONING_EFFORT", "low")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


def test_the_code_default_provider_is_groq():
    assert Settings(_env_file=None).agent_provider == "groq"


def test_groq_provider_gives_chatgroq_with_reasoning_effort(provider):
    provider("groq")
    llm = get_llm(model="openai/gpt-oss-120b")
    assert isinstance(llm, ChatGroq)
    assert llm.model_name == "openai/gpt-oss-120b" and llm.reasoning_effort == "low"


def test_fireworks_provider_gives_chatopenai_on_the_fireworks_endpoint(provider):
    provider("fireworks", LIGHT_MODEL="accounts/fireworks/models/gpt-oss-20b")
    llm = get_llm(model=get_settings().light_model)
    assert isinstance(llm, ChatOpenAI) and not isinstance(llm, ChatGroq)
    assert llm.openai_api_base == FIREWORKS_BASE_URL
    assert llm.model_name == "accounts/fireworks/models/gpt-oss-20b"
    assert llm.openai_api_key.get_secret_value() == "fw_test_not_real"


def test_fireworks_sends_reasoning_effort_as_a_top_level_body_field(provider):
    provider("fireworks")
    payload = get_llm(model=FW_120, max_tokens=400)._get_request_payload([HumanMessage(content="hi")])
    assert payload["model"] == FW_120
    assert payload["extra_body"] == {"reasoning_effort": "low"}  # the OpenAI client merges it into the body


def test_fireworks_non_gpt_oss_model_gets_no_reasoning_effort(provider):
    provider("fireworks")
    payload = get_llm(model="accounts/fireworks/models/other")._get_request_payload([HumanMessage(content="hi")])
    assert not payload.get("extra_body")


def test_provider_name_is_case_insensitive_and_unknown_is_an_error(provider):
    provider("Fireworks")
    assert isinstance(get_llm(model=FW_120), ChatOpenAI)
    provider("openai")
    with pytest.raises(ValueError, match="AGENT_PROVIDER"):
        get_llm(model=FW_120)


@pytest.mark.parametrize("name", ["groq", "fireworks", None])
def test_the_judge_is_always_groq(provider, name):
    provider(name, JUDGE_MODEL="qwen/qwen3.8-27b")
    judge = get_judge_llm()
    assert isinstance(judge, ChatGroq)
    assert judge.model_name == "qwen/qwen3.8-27b" and judge.max_tokens == 500


def test_the_eval_judge_class_uses_the_groq_judge(provider):
    from eval.judge import Judge

    provider("fireworks", JUDGE_MODEL="qwen/qwen3.8-27b")
    assert isinstance(Judge().llm, ChatGroq)


@pytest.mark.parametrize("node, setting", [("app.agents.router", "light_model"), ("app.agents.support", None),
                                           ("app.agents.knowledge", "llm_model"),
                                           ("app.agents.synthesizer", "light_model"),
                                           ("app.agents.summarizer", "light_model")])
def test_every_agent_builds_its_model_through_get_llm(node, setting):
    """The agents only ever ask app.llm.get_llm, so AGENT_PROVIDER reaches all of them."""
    import importlib

    mod = importlib.import_module(node)
    assert mod.get_llm is get_llm
