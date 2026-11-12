"""Rolling summary. Stub LLM and stub agents, real graph, real SQLite: no network, no keys."""
import json
import logging

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.common import SUMMARY_HEADER, recent_dialogue
from app.agents.summarizer import make_summarizer_node
from app.config import get_settings
from app.graph import build_graph
from app.handoff import FileQueueNotifier, make_handoff_node
from app.memory import Memory
from app.service import ChatService

THRESHOLD, WINDOW = 6, 2


@pytest.fixture(autouse=True)
def small_limits(monkeypatch):
    """Threshold 6 messages, window 2. Each turn adds 2 messages (customer + assistant)."""
    monkeypatch.setenv("SUMMARY_THRESHOLD", str(THRESHOLD))
    monkeypatch.setenv("HISTORY_WINDOW", str(WINDOW))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeSummaryLLM:
    """Numbers its summaries, records what it was asked, and can be made to fail."""

    def __init__(self):
        self.calls = []
        self.fail_with = None
        self.reply = None

    def invoke(self, messages):
        self.calls.append(messages)
        if self.fail_with:
            raise self.fail_with
        return AIMessage(content=self.reply if self.reply is not None else f"SUMMARY#{len(self.calls)}")


def make_graph(llm, saver=None):
    """Router/agents are stubs. The synthesizer reports exactly what an agent would receive."""

    def router(state):
        return {"plan": [], "step": 0, "language": "English", "routing_reasoning": "stub",
                "agent_outputs": {}, "final_answer": ""}

    def synthesizer(state):
        seen = recent_dialogue(state["messages"], get_settings().history_window, state.get("summary", ""))
        answer = " || ".join(f"{m.type}:{m.content}" for m in seen)
        return {"final_answer": answer, "messages": [AIMessage(content=f"ans{len(state['messages'])}")]}

    return build_graph(router, lambda s: {}, lambda s: {}, synthesizer, lambda s: {}, checkpointer=saver,
                       summarizer=make_summarizer_node(llm))


def state_of(svc, thread):
    return svc.graph.get_state({"configurable": {"thread_id": thread}}).values


def chat(svc, n, user="alice", conv="c1"):
    return [svc.handle(f"msg{i}", user, conversation_id=conv) for i in range(1, n + 1)]


def test_nothing_happens_below_the_threshold():
    llm = FakeSummaryLLM()
    svc = ChatService(make_graph(llm))
    chat(svc, 3)  # 6 messages after turn 3, the threshold is "more than 6" at the start of a turn
    assert llm.calls == []
    assert len(state_of(svc, "alice:c1")["messages"]) == 6
    assert not state_of(svc, "alice:c1").get("summary")


def test_the_summary_fires_above_the_threshold_and_keeps_the_window():
    llm = FakeSummaryLLM()
    svc = ChatService(make_graph(llm))
    chat(svc, 3)
    r4 = svc.handle("msg4", "alice", conversation_id="c1")  # 7 messages at the start: above 6

    assert len(llm.calls) == 1
    asked = llm.calls[0][1].content
    assert "msg1" in asked and "msg2" in asked and "msg3" in asked  # the old part goes in
    assert "msg4" not in asked  # the new message is part of the window, not summarised
    st = state_of(svc, "alice:c1")
    assert st["summary"] == "SUMMARY#1"
    # kept: the last 2 messages at that moment (answer of turn 3 and msg4), plus this turn's answer
    assert [m.content for m in st["messages"]][:2] == ["ans5", "msg4"] and len(st["messages"]) == 3
    # the agents saw summary + the window, not the removed messages
    assert r4.response.startswith(f"system:{SUMMARY_HEADER}SUMMARY#1")
    assert "msg1" not in r4.response and "human:msg4" in r4.response


def test_the_checkpoint_stays_bounded_over_a_long_conversation():
    llm = FakeSummaryLLM()
    svc = ChatService(make_graph(llm))
    chat(svc, 15)
    n = len(state_of(svc, "alice:c1")["messages"])
    assert n <= THRESHOLD + 2  # never grows past threshold + one turn


def test_a_second_summary_folds_in_the_previous_one():
    llm = FakeSummaryLLM()
    svc = ChatService(make_graph(llm))
    chat(svc, 9)
    assert len(llm.calls) >= 2
    assert "SUMMARY#1" in llm.calls[1][1].content  # the previous summary is an input of the next
    assert state_of(svc, "alice:c1")["summary"] == f"SUMMARY#{len(llm.calls)}"


@pytest.mark.parametrize("failure", [RuntimeError("boom"), TimeoutError("took too long")])
def test_a_failing_summary_never_breaks_the_turn_and_is_retried_next_turn(failure, caplog):
    llm = FakeSummaryLLM()
    svc = ChatService(make_graph(llm))
    chat(svc, 3)
    llm.fail_with = failure
    with caplog.at_level(logging.WARNING, logger="app.agents.summarizer"):
        r = svc.handle("msg4", "alice", conversation_id="c1")  # must not raise
    assert r.response.endswith("human:msg4")  # a normal answer with the plain window
    assert "summary skipped" in caplog.text and type(failure).__name__ in caplog.text
    st = state_of(svc, "alice:c1")
    assert not st.get("summary") and len(st["messages"]) == 8  # nothing was removed

    llm.fail_with = None  # the provider is back: the next turn catches up
    r5 = svc.handle("msg5", "alice", conversation_id="c1")
    assert state_of(svc, "alice:c1")["summary"] == f"SUMMARY#{len(llm.calls)}"
    assert "msg1" in llm.calls[-1][1].content and r5.response.startswith("system:")


def test_an_empty_summary_is_treated_as_a_failure():
    llm = FakeSummaryLLM()
    svc = ChatService(make_graph(llm))
    chat(svc, 3)
    llm.reply = "   "
    svc.handle("msg4", "alice", conversation_id="c1")
    st = state_of(svc, "alice:c1")
    assert not st.get("summary") and len(st["messages"]) == 8


def test_summary_and_trimmed_messages_survive_a_restart(tmp_path):
    db = tmp_path / "checkpoints.sqlite"
    llm = FakeSummaryLLM()
    m1 = Memory(db)
    chat(ChatService(make_graph(llm, m1.saver)), 4)
    m1.close()

    m2 = Memory(db)
    try:
        svc2 = ChatService(make_graph(llm, m2.saver))
        st = state_of(svc2, "alice:c1")
        assert st["summary"] == "SUMMARY#1" and len(st["messages"]) == 3
        r = svc2.handle("after restart", "alice", conversation_id="c1")
        assert r.response.startswith(f"system:{SUMMARY_HEADER}SUMMARY#1")
    finally:
        m2.close()


def test_two_users_have_separate_summaries(tmp_path):
    llm = FakeSummaryLLM()
    m = Memory(tmp_path / "c.sqlite")
    try:
        svc = ChatService(make_graph(llm, m.saver))
        chat(svc, 4, user="alice")
        rb = svc.handle("hello", "bob", conversation_id="c1")  # same conversation_id
        assert not state_of(svc, "bob:c1").get("summary")
        assert rb.response == "human:hello" and "SUMMARY" not in rb.response
    finally:
        m.close()


def test_without_a_summary_the_history_is_exactly_what_it_was():
    msgs = [HumanMessage(content="a"), AIMessage(content="b"), HumanMessage(content="c")]
    assert recent_dialogue(msgs, 2) == msgs[1:]
    assert recent_dialogue(msgs, 2, "") == msgs[1:]
    with_summary = recent_dialogue(msgs, 2, "S")
    assert isinstance(with_summary[0], SystemMessage) and with_summary[0].content.endswith("S")
    assert with_summary[1:] == msgs[1:]


def test_the_summary_call_uses_the_light_model_with_a_short_timeout_and_no_retries(monkeypatch):
    monkeypatch.setenv("LIGHT_MODEL", "m-light")
    monkeypatch.setenv("SUPPORT_MODEL", "m-support")
    monkeypatch.setenv("LLM_MODEL", "m-knowledge")
    monkeypatch.setenv("SUMMARY_TIMEOUT_SECONDS", "7")
    get_settings.cache_clear()
    seen = {}

    def spy(**kwargs):
        seen.update(kwargs)
        return FakeSummaryLLM()

    monkeypatch.setattr("app.agents.summarizer.get_llm", spy)
    node = make_summarizer_node()  # no llm injected: builds its own
    msgs = [m for i in range(5) for m in (HumanMessage(content=f"q{i}", id=f"h{i}"), AIMessage(content=f"a{i}", id=f"a{i}"))]
    out = node({"messages": msgs})
    assert seen["model"] == "m-light" and seen["max_retries"] == 0 and seen["timeout"] == 7.0
    assert out["summary"] == "SUMMARY#1" and len(out["messages"]) == len(msgs) - WINDOW


def test_the_handoff_record_carries_the_summary_only_when_there_is_one(repo, tmp_path):
    node = make_handoff_node(repo=repo, notifier=FileQueueNotifier(tmp_path / "handoffs.jsonl"))
    base = {"messages": [HumanMessage(content="I want a human")], "user_id": "cliente1988",
            "language": "English", "plan": [], "agent_outputs": {}, "wants_human": True}
    node(base)
    node({**base, "summary": "Customer asked about a declined payment, ticket TKT-1."})
    plain, with_summary = [json.loads(line) for line in (tmp_path / "handoffs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "earlier_summary" not in plain
    assert with_summary["earlier_summary"].startswith("Customer asked")
