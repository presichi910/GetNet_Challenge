"""One model per role: which agent uses which model."""
import re
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.router import RoutingDecision, make_router_node
from app.agents.support import make_support_node
from app.agents.synthesizer import make_synthesizer_node
from app.config import Settings, get_settings
from tests.conftest import scripted

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def models(monkeypatch):
    """Set the model variables for one test. An empty value still overrides whatever .env says."""
    def apply(**env):
        for name in ("LLM_MODEL", "LIGHT_MODEL", "SUPPORT_MODEL", "JUDGE_MODEL"):
            monkeypatch.setenv(name, env.get(name, {"LLM_MODEL": "m-knowledge", "LIGHT_MODEL": "m-light",
                                                    "SUPPORT_MODEL": "", "JUDGE_MODEL": "m-judge"}[name]))
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


def test_support_model_defaults_to_the_light_model_and_is_empty_in_the_code_default():
    s = Settings(_env_file=None, light_model="m-light")
    assert s.support_model == "" and s.effective_support_model == "m-light"


def test_support_model_overrides_only_the_support_role():
    s = Settings(_env_file=None, llm_model="m-knowledge", light_model="m-light", support_model="m-support")
    assert (s.llm_model, s.light_model, s.effective_support_model) == ("m-knowledge", "m-light", "m-support")


def run_support(monkeypatch, repo):
    """Run the real Support node with get_llm replaced by a recorder. Returns the models it asked for."""
    asked = []

    def fake_get_llm(**kwargs):
        asked.append(kwargs.get("model"))
        return scripted(AIMessage(content="ok"))

    monkeypatch.setattr("app.agents.support.get_llm", fake_get_llm)
    node = make_support_node(repo=repo)
    node({"messages": [HumanMessage(content="hi")], "user_id": "cliente1988", "language": "English",
          "agent_outputs": {}, "step": 0})
    return asked


def test_support_agent_uses_support_model(models, monkeypatch, repo):
    models(SUPPORT_MODEL="m-support")
    assert run_support(monkeypatch, repo) == ["m-support"]


@pytest.mark.parametrize("empty", [""])  # what docker-compose passes when SUPPORT_MODEL is not set
def test_empty_support_model_falls_back_to_the_light_model(models, monkeypatch, repo, empty):
    models(SUPPORT_MODEL=empty)
    assert run_support(monkeypatch, repo) == ["m-light"]


def test_router_and_synthesizer_keep_using_the_light_model(models, monkeypatch):
    models(SUPPORT_MODEL="m-support")
    asked = []

    class Stub:
        def with_structured_output(self, *a, **k):
            return self

        def invoke(self, _):
            return RoutingDecision(reasoning="r", agents=[], language="English")

    def fake_get_llm(**kwargs):
        asked.append(kwargs.get("model"))
        return Stub()

    monkeypatch.setattr("app.agents.router.get_llm", fake_get_llm)
    monkeypatch.setattr("app.agents.synthesizer.get_llm", lambda **kw: (asked.append(kw.get("model")), scripted(AIMessage(content="hi")))[1])
    make_router_node()({"messages": [HumanMessage(content="hi")]})
    make_synthesizer_node()({"messages": [HumanMessage(content="hi")], "plan": [], "agent_outputs": {}})
    assert asked == ["m-light", "m-light"]


def test_the_eval_report_lists_the_support_model_apart(models):
    from eval.run_eval import current_models

    models(SUPPORT_MODEL="m-support")
    assert current_models() == {"knowledge": "m-knowledge", "router / synthesizer": "m-light",
                                "support": "m-support", "judge": "m-judge"}


# ---- the files that configure the containers must not drift from the settings ----

MODEL_VARS = ("LLM_MODEL", "LIGHT_MODEL", "SUPPORT_MODEL", "JUDGE_MODEL")


def test_every_model_variable_is_in_docker_compose_and_env_example():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for var in MODEL_VARS:
        assert re.search(rf"^\s*{var}:\s*\$\{{{var}:-", compose, re.M), f"{var} missing in docker-compose.yml"
        assert re.search(rf"^{var}=", example, re.M), f"{var} missing in .env.example"


def test_env_example_delivers_support_on_the_bigger_model_and_the_code_default_stays_the_light_model():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^SUPPORT_MODEL=openai/gpt-oss-120b\s*$", example, re.M)
    assert Settings(_env_file=None).support_model == ""  # without the variable, Support = LIGHT_MODEL
