"""Agent tests with a fake LLM (real tools, no network)."""
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.router import DEFAULT_PLAN, RoutingDecision, make_router_node, normalize_plan
from app.agents.support import make_support_node
from app.agents.synthesizer import make_synthesizer_node
from app.prompts import FALLBACK_MESSAGE
from tests.conftest import scripted


def test_normalize_plan_dedupes_orders_and_filters():
    assert normalize_plan(["knowledge", "support", "knowledge"]) == ["support", "knowledge"]
    assert normalize_plan(["bogus", "knowledge"]) == ["knowledge"]
    assert normalize_plan([]) == []


class _StructuredStub:
    def __init__(self, decision=None, exc=None):
        self.decision, self.exc = decision, exc

    def with_structured_output(self, *a, **k):
        return self

    def invoke(self, _):
        if self.exc:
            raise self.exc
        return self.decision


def test_router_node_resets_turn_state_and_orders_plan():
    node = make_router_node(_StructuredStub(RoutingDecision(reasoning="r", agents=["knowledge", "support"], language="Portuguese")))
    out = node({"messages": [HumanMessage(content="oi")]})
    assert out["plan"] == ["support", "knowledge"] and out["language"] == "Portuguese"
    assert out["step"] == 0 and out["agent_outputs"] == {}


def test_router_falls_back_to_knowledge_on_failure():
    node = make_router_node(_StructuredStub(exc=RuntimeError("groq down")))
    out = node({"messages": [HumanMessage(content="x")]})
    assert out["plan"] == DEFAULT_PLAN and out["routing_reasoning"] == "router_error_fallback"


def test_support_agent_calls_tool_and_captures_evidence(repo):
    llm = scripted(
        AIMessage(content="", tool_calls=[{"name": "get_payout_schedule", "args": {}, "id": "c1"}]),
        AIMessage(content="Your money arrives tomorrow."),
    )
    node = make_support_node(llm=llm, repo=repo)
    out = node({"messages": [HumanMessage(content="when do I get paid?")], "user_id": "cliente1988",
                "language": "English", "agent_outputs": {}, "step": 0})
    res = out["agent_outputs"]["support"]
    assert res["answer"] == "Your money arrives tomorrow." and res["error"] is None
    assert [t["name"] for t in res["tool_calls"]] == ["get_payout_schedule"]
    assert "scheduled" in res["contexts"][0]
    assert out["step"] == 1


def test_agent_failure_is_contained(repo):
    class Boom:
        def bind_tools(self, *a, **k):
            return self

        def invoke(self, *a, **k):
            raise RuntimeError("llm exploded")

    node = make_support_node(llm=Boom(), repo=repo)
    out = node({"messages": [HumanMessage(content="x")], "user_id": "cliente1988", "agent_outputs": {}, "step": 0})
    assert out["agent_outputs"]["support"]["error"] and out["step"] == 1


def test_synthesizer_passthrough_merge_and_fallback():
    ok = lambda n: {"answer": f"{n} says hi", "error": None}  # noqa: E731
    base = {"messages": [HumanMessage(content="q")], "language": "English"}

    # one agent: passthrough, no LLM call
    node = make_synthesizer_node(llm=scripted())
    assert node({**base, "plan": ["knowledge"], "agent_outputs": {"knowledge": ok("knowledge")}})["final_answer"] == "knowledge says hi"

    # two agents: merged
    node = make_synthesizer_node(llm=scripted(AIMessage(content="merged answer")))
    two = {"support": ok("support"), "knowledge": ok("knowledge")}
    assert node({**base, "plan": ["support", "knowledge"], "agent_outputs": two})["final_answer"] == "merged answer"

    # all failed: fallback
    failed = {"knowledge": {"answer": "", "error": "x"}}
    assert node({**base, "plan": ["knowledge"], "agent_outputs": failed})["final_answer"] == FALLBACK_MESSAGE


def test_knowledge_is_forced_to_use_a_tool(repo):
    """If the model skips the tools, it gets nudged once."""
    from app.agents.common import run_tool_agent
    from app.tools.customer_tools import make_support_tools

    llm = scripted(
        AIMessage(content="Here is an answer from memory."),  # no tool used
        AIMessage(content="", tool_calls=[{"name": "get_terminal_status", "args": {}, "id": "c1"}]),
        AIMessage(content="Grounded answer."),
    )
    res = run_tool_agent("t", llm, make_support_tools("cliente1988", repo), "sys",
                         [HumanMessage(content="q")], require_tool=True)
    assert res["answer"] == "Grounded answer." and [t["name"] for t in res["tool_calls"]] == ["get_terminal_status"]
