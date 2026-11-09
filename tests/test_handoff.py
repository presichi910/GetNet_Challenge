"""Human handoff: trigger logic, the handoff node's side effects, and graph routing."""
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.common import run_tool_agent
from app.agents.router import RoutingDecision, make_router_node
from app.graph import build_graph, next_node
from app.handoff import FileQueueNotifier, compose_notice, handoff_reason, make_handoff_node
from app.service import ChatService
from app.tools.handoff_tools import escalate_to_human
from tests.conftest import scripted

OK = {"answer": "here you go", "error": None, "unresolved": False, "tool_calls": []}
UNRESOLVED = {"answer": "I could not confirm it", "error": None, "unresolved": True, "tool_calls": []}
FAILED = {"answer": "", "error": "boom", "unresolved": False, "tool_calls": []}


# ------------------------------------------------------------------ trigger logic
@pytest.mark.parametrize("state, expected", [
    ({"wants_human": True, "plan": []}, "user_requested"),
    ({"plan": []}, None),  # greeting: nothing to escalate
    ({"plan": ["knowledge"], "agent_outputs": {"knowledge": OK}}, None),
    ({"plan": ["knowledge"], "agent_outputs": {"knowledge": FAILED}}, "technical_failure"),
    ({"plan": ["support", "knowledge"], "agent_outputs": {"support": FAILED, "knowledge": FAILED}}, "technical_failure"),
    ({"plan": ["knowledge"], "agent_outputs": {"knowledge": UNRESOLVED}}, "agent_unresolved"),
    # mixed outcomes are NOT escalated: the customer got something useful
    ({"plan": ["support", "knowledge"], "agent_outputs": {"support": OK, "knowledge": UNRESOLVED}}, None),
    ({"plan": ["support", "knowledge"], "agent_outputs": {"support": FAILED, "knowledge": OK}}, None),
    ({"plan": ["support", "knowledge"], "agent_outputs": {"support": UNRESOLVED, "knowledge": UNRESOLVED}}, "agent_unresolved"),
])
def test_handoff_reason(state, expected):
    assert handoff_reason(state) == expected


def test_next_node_routes_to_handoff():
    assert next_node({"wants_human": True, "plan": ["knowledge"], "step": 0}) == "handoff"  # skips specialists
    assert next_node({"plan": ["knowledge"], "step": 1, "agent_outputs": {"knowledge": UNRESOLVED}}) == "handoff"
    assert next_node({"plan": ["knowledge"], "step": 1, "agent_outputs": {"knowledge": OK}}) == "synthesizer"


# ------------------------------------------------------------------ the marker never reaches the user
def _tools(repo):
    from app.tools.customer_tools import make_support_tools

    return [*make_support_tools("cliente1988", repo), escalate_to_human]


def test_calling_the_escalate_tool_marks_the_agent_unresolved_and_keeps_the_reason(repo):
    llm = scripted(
        AIMessage(content="", tool_calls=[{"name": "escalate_to_human", "args": {"reason": "no loan data"}, "id": "c1"}]),
        AIMessage(content="I could not resolve this myself."),
    )
    res = run_tool_agent("support", llm, _tools(repo), "sys", [HumanMessage(content="q")])
    assert res["unresolved"] is True and res["error"] is None
    assert res["tool_calls"][0] == {"name": "escalate_to_human", "args": {"reason": "no loan data"}}  # the human sees why


def test_escalating_without_any_final_text_is_not_a_technical_failure(repo):
    llm = scripted(
        AIMessage(content="", tool_calls=[{"name": "escalate_to_human", "args": {"reason": "nothing found"}, "id": "c1"}]),
        AIMessage(content=""),
    )
    res = run_tool_agent("support", llm, _tools(repo), "sys", [HumanMessage(content="q")])
    assert res["unresolved"] is True and res["error"] is None and res["answer"] == "nothing found"


def test_a_normal_answer_is_not_unresolved(repo):
    res = run_tool_agent("support", scripted(AIMessage(content="All good.")), _tools(repo), "sys",
                         [HumanMessage(content="q")])
    assert res["unresolved"] is False and res["answer"] == "All good."


# ------------------------------------------------------------------ router
class _Structured:
    def __init__(self, decision):
        self.decision = decision

    def with_structured_output(self, *a, **k):
        return self

    def invoke(self, _):
        return self.decision


def test_router_forces_empty_plan_when_user_wants_human():
    node = make_router_node(_Structured(RoutingDecision(reasoning="asks for a person", agents=["knowledge"],
                                                        language="English", wants_human=True)))
    out = node({"messages": [HumanMessage(content="I want a human")]})
    assert out["wants_human"] is True and out["plan"] == [] and out["handoff"] is None


# ------------------------------------------------------------------ the handoff node
def make_node(repo, tmp_path):
    return make_handoff_node(repo=repo, notifier=FileQueueNotifier(tmp_path / "handoffs.jsonl"))


def base_state(**over):
    state = {"messages": [HumanMessage(content="My terminal is dead and nothing works")], "user_id": "cliente1988",
             "language": "English", "plan": ["support"], "agent_outputs": {}, "wants_human": False}
    state.update(over)
    return state


def test_handoff_creates_ticket_queue_record_and_notice(repo, tmp_path):
    out = make_node(repo, tmp_path)(base_state(wants_human=True, plan=[]))
    info = out["handoff"]
    assert info["reason"] == "user_requested" and info["id"].startswith("HND-") and info["ticket_id"].startswith("TKT-")
    assert info["id"] in out["final_answer"] and info["ticket_id"] in out["final_answer"]
    assert isinstance(out["messages"][0], AIMessage)
    record = json.loads((tmp_path / "handoffs.jsonl").read_text(encoding="utf-8").strip())
    assert record["user_id"] == "cliente1988" and record["user_message"].startswith("My terminal")
    assert record["conversation"][0]["role"] == "human"
    assert repo.get_tickets("cliente1988")[-1]["ticket_id"] == info["ticket_id"]  # visible to the Support agent too


def test_handoff_for_unknown_customer_has_no_ticket_but_still_queues(repo, tmp_path):
    out = make_node(repo, tmp_path)(base_state(user_id="ghost", wants_human=True, plan=[]))
    assert out["handoff"]["ticket_id"] is None and out["handoff"]["id"] in out["final_answer"]
    assert (tmp_path / "handoffs.jsonl").exists()


def test_unresolved_findings_go_to_the_human_not_to_the_customer(repo, tmp_path):
    outs = {"knowledge": {**UNRESOLVED, "tool_calls": [{"name": "search_getnet_kb", "args": {"query": "x"}}]}}
    out = make_node(repo, tmp_path)(base_state(plan=["knowledge"], agent_outputs=outs))
    assert "I could not confirm it" not in out["final_answer"]  # low-confidence content is never shown
    assert out["handoff"]["id"] in out["final_answer"]
    assert out["handoff"]["reason"] == "agent_unresolved" and out["handoff"]["priority"] == "normal"
    record = json.loads((tmp_path / "handoffs.jsonl").read_text(encoding="utf-8").strip())
    assert record["agent_findings"]["knowledge"] == "I could not confirm it"  # ...but the operator gets it
    assert record["tool_calls"] == [{"agent": "knowledge", "name": "search_getnet_kb", "args": {"query": "x"}}]


def test_technical_failure_is_high_priority_and_drops_the_empty_answer(repo, tmp_path):
    out = make_node(repo, tmp_path)(base_state(plan=["knowledge"], agent_outputs={"knowledge": FAILED}))
    assert out["handoff"]["priority"] == "high" and "technical" in out["final_answer"].lower()


def test_notifier_failure_does_not_break_the_reply(repo):
    class Broken:
        def notify(self, record):
            raise OSError("disk full")

    out = make_handoff_node(repo=repo, notifier=Broken())(base_state(wants_human=True, plan=[]))
    assert out["handoff"]["id"] in out["final_answer"]


@pytest.mark.parametrize("language, snippet", [("Portuguese", "atendente"), ("Spanish", "agente humano"),
                                                ("English", "human agent"), ("Klingon", "human agent")])
def test_notice_is_localised_with_english_fallback(language, snippet):
    assert snippet in compose_notice("user_requested", language, "HND-1")


# ------------------------------------------------------------------ end to end through the real graph
def graph_with(plan, outputs, repo, tmp_path, wants_human=False):
    calls = []

    def router(state):
        return {"plan": plan, "step": 0, "language": "English", "routing_reasoning": "stub", "agent_outputs": {},
                "final_answer": "", "wants_human": wants_human, "handoff": None}

    def agent(name):
        def node(state):
            calls.append(name)
            return {"agent_outputs": {**state.get("agent_outputs", {}), name: outputs[name]}, "step": state.get("step", 0) + 1}
        return node

    def synth(state):
        calls.append("synthesizer")
        return {"final_answer": "normal answer", "messages": [AIMessage(content="normal answer")]}

    graph = build_graph(router, agent("knowledge"), agent("support"), synth, make_node(repo, tmp_path))
    return ChatService(graph), calls


def test_user_request_skips_all_specialists(repo, tmp_path):
    svc, calls = graph_with([], {}, repo, tmp_path, wants_human=True)
    r = svc.handle("human please", "cliente1988")
    assert calls == [] and r.handoff.reason == "user_requested" and r.agents_used == []


def test_unresolved_agent_ends_in_handoff_not_synthesizer(repo, tmp_path):
    svc, calls = graph_with(["knowledge"], {"knowledge": UNRESOLVED}, repo, tmp_path)
    r = svc.handle("obscure question", "cliente1988")
    assert calls == ["knowledge"] and r.handoff.reason == "agent_unresolved"


def test_resolved_agent_has_no_handoff(repo, tmp_path):
    svc, calls = graph_with(["knowledge"], {"knowledge": OK}, repo, tmp_path)
    r = svc.handle("easy question", "cliente1988")
    assert calls == ["knowledge", "synthesizer"] and r.handoff is None and r.response == "normal answer"


def test_handoff_does_not_leak_into_the_next_turn_of_the_same_user(repo, tmp_path):
    """Same thread, two turns: the router resets wants_human/handoff, so turn 2 is a normal answer."""
    flags = iter([True, False])
    calls = []

    def router(state):
        wants = next(flags)
        return {"plan": [] if wants else ["knowledge"], "step": 0, "language": "English", "routing_reasoning": "",
                "agent_outputs": {}, "final_answer": "", "wants_human": wants, "handoff": None}

    def knowledge(state):
        calls.append("knowledge")
        return {"agent_outputs": {"knowledge": OK}, "step": 1}

    def synth(state):
        return {"final_answer": "normal answer", "messages": [AIMessage(content="normal answer")]}

    svc = ChatService(build_graph(router, knowledge, knowledge, synth, make_node(repo, tmp_path)))
    assert svc.handle("human please", "cliente1988").handoff is not None
    second = svc.handle("and how do I use Pix?", "cliente1988")
    assert second.handoff is None and second.response == "normal answer"
