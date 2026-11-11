"""Graph tests: real graph, stub agents."""
from langchain_core.messages import AIMessage, HumanMessage

from app.graph import build_graph, next_node
from app.service import ChatService


def make_stubs(plan, calls):
    def router(state):
        calls.append("router")
        return {"plan": plan, "step": 0, "language": "English", "routing_reasoning": "stub",
                "agent_outputs": {}, "final_answer": ""}

    def agent(name):
        def node(state):
            calls.append(name)
            outs = state.get("agent_outputs", {})
            return {"agent_outputs": {**outs, name: {
                "agent": name, "answer": f"{name}-answer", "error": None,
                "sources": [{"url": f"http://x/{name}", "title": name, "type": "kb"}],
                "tool_calls": [{"name": f"{name}_tool", "args": {}}], "contexts": []}},
                "step": state.get("step", 0) + 1}
        return node

    def synthesizer(state):
        calls.append("synthesizer")
        outs = state.get("agent_outputs", {})
        answer = " + ".join(o["answer"] for o in outs.values()) or "direct"
        return {"final_answer": answer, "messages": [AIMessage(content=answer)]}

    def handoff(state):
        calls.append("handoff")
        return {"handoff": {"id": "HND-T", "reason": "stub", "priority": "normal", "ticket_id": None},
                "final_answer": "handed-off", "messages": [AIMessage(content="handed-off")]}

    return router, agent("knowledge"), agent("support"), synthesizer, handoff


def run(plan):
    calls = []
    graph = build_graph(*make_stubs(plan, calls))
    return ChatService(graph), calls


def test_next_node_follows_plan():
    assert next_node({"plan": ["support", "knowledge"], "step": 0}) == "support"
    assert next_node({"plan": ["support", "knowledge"], "step": 1}) == "knowledge"
    done = {"support": {"answer": "a", "error": None}, "knowledge": {"answer": "b", "error": None}}
    assert next_node({"plan": ["support", "knowledge"], "step": 2, "agent_outputs": done}) == "synthesizer"
    assert next_node({"plan": [], "step": 0}) == "synthesizer"


def test_single_agent_route():
    svc, calls = run(["knowledge"])
    r = svc.handle("q", "u1")
    assert calls == ["router", "knowledge", "synthesizer"]
    assert r.agents_used == ["knowledge"] and r.response == "knowledge-answer"


def test_multi_agent_order_is_respected():
    svc, calls = run(["support", "knowledge"])
    r = svc.handle("q", "u1")
    assert calls == ["router", "support", "knowledge", "synthesizer"]
    assert r.response == "support-answer + knowledge-answer"
    assert [t.agent for t in r.tool_calls] == ["support", "knowledge"]
    assert {s.url for s in r.sources} == {"http://x/support", "http://x/knowledge"}


def test_empty_plan_goes_straight_to_synthesizer():
    svc, calls = run([])
    r = svc.handle("hi", "u1")
    assert calls == ["router", "synthesizer"]
    assert r.agents_used == [] and r.response == "direct"


def test_memory_is_per_user_and_per_turn_state_resets():
    svc, _ = run(["knowledge"])
    svc.handle("first", "alice")
    svc.handle("second", "alice")
    svc.handle("other", "bob")
    alice = svc.graph.get_state({"configurable": {"thread_id": "alice:default"}}).values["messages"]
    bob = svc.graph.get_state({"configurable": {"thread_id": "bob:default"}}).values["messages"]
    assert [m.content for m in alice if isinstance(m, HumanMessage)] == ["first", "second"]
    assert [m.content for m in bob if isinstance(m, HumanMessage)] == ["other"]
    # turn 1 outputs must not leak into turn 2
    assert svc.handle("third", "alice").agents_used == ["knowledge"]


def test_degraded_flag_when_specialist_fails():
    calls = []
    router, knowledge, support, synth, handoff = make_stubs(["knowledge"], calls)

    def failing(state):
        outs = state.get("agent_outputs", {})
        return {"agent_outputs": {**outs, "knowledge": {"agent": "knowledge", "answer": "", "error": "boom",
                                                       "sources": [], "tool_calls": [], "contexts": []}},
                "step": state.get("step", 0) + 1}

    r = ChatService(build_graph(router, failing, support, synth, handoff)).handle("q", "u1")
    assert r.degraded is True
    assert r.handoff and calls[-1] == "handoff"  # all agents failed -> human


def test_conversations_of_the_same_user_are_separate_threads():
    svc, _ = run(["knowledge"])
    svc.handle("a1", "alice", conversation_id="c1")
    svc.handle("a2", "alice", conversation_id="c2")
    svc.handle("a3", "alice", conversation_id="c1")

    def human(thread):
        msgs = svc.graph.get_state({"configurable": {"thread_id": thread}}).values["messages"]
        return [m.content for m in msgs if isinstance(m, HumanMessage)]

    assert human("alice:c1") == ["a1", "a3"] and human("alice:c2") == ["a2"]
