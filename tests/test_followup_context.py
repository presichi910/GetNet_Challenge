"""Second turn of a conversation: what does each node actually receive?

Real graph, real nodes (router, support, knowledge, synthesizer), real SQLite file. Only the LLMs are
fakes, and they record the messages they are given. The plan of each turn is forced by the scripted
router, so these tests do not depend on a model's routing choice.

They separate two questions that look alike from the outside:
  - "the memory works": the history of turn 1 is stored and reaches the prompt of every node on turn 2;
  - "the Router routes badly": a different thing, measured by the multi-turn cases of the eval.
"""
from datetime import date

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agents.common import SUMMARY_HEADER
from app.agents.knowledge import make_knowledge_node
from app.agents.router import RoutingDecision, make_router_node
from app.agents.support import make_support_node
from app.agents.synthesizer import make_synthesizer_node
from app.config import get_settings
from app.data.repository import CustomerRepository
from app.graph import build_graph
from app.memory import Memory
from app.service import ChatService
from tests.conftest import ToolFakeModel

USER, THREAD = "cliente1988", "cliente1988:c1"
FIRST = "My favorite color is green, remember it."
FIRST_ANSWER = "Got it, green."
SECOND = "Which color did I tell you?"


class RecordingRouter:
    """Stands in for the Router's LLM: records every input and returns the plans in order."""

    def __init__(self, *plans):
        self.plans, self.inputs = list(plans), []

    def with_structured_output(self, *a, **k):
        return self

    def invoke(self, messages):
        self.inputs.append(messages)
        return RoutingDecision(reasoning="scripted", agents=self.plans.pop(0), language="English")


class RecordingModel(ToolFakeModel):
    """A fake chat model that keeps the messages of every call."""

    seen: list = []

    def _generate(self, messages, *a, **k):
        self.seen.append(list(messages))
        return super()._generate(messages, *a, **k)


def recording(*answers: str) -> RecordingModel:
    return RecordingModel(messages=iter([AIMessage(content=a) for a in answers]), seen=[])


def texts(messages) -> list[str]:
    return [str(m.content) for m in messages]


def build(saver, router, *, support=None, knowledge=None, synth=None, repo=None):
    repo = repo or CustomerRepository(tickets_file=None, today=date(2026, 1, 15))
    return build_graph(make_router_node(router), make_knowledge_node(knowledge or recording("k", "k")),
                       make_support_node(llm=support or recording("s"), repo=repo),
                       make_synthesizer_node(synth or recording("direct")), lambda s: {}, checkpointer=saver)


def turn(svc, message):
    state, _ = svc.run_raw(message, USER, thread_id=THREAD)
    return state


@pytest.fixture
def db(tmp_path):
    return tmp_path / "data" / "checkpoints.sqlite"


@pytest.fixture(autouse=True)
def default_window(monkeypatch):
    monkeypatch.delenv("HISTORY_WINDOW", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def first_turn_then_second(db, router, **nodes):
    """Turn 1 is a plain greeting-style turn answered directly; turn 2 is the one under test."""
    mem = Memory(db)
    try:
        svc = ChatService(build(mem.saver, router, **{**nodes, "synth": nodes.get("synth") or recording(FIRST_ANSWER, "direct")}))
        turn(svc, FIRST)
        return svc, turn(svc, SECOND)
    finally:
        mem.close()


def test_the_router_sees_turn_one_on_turn_two(db):
    router = RecordingRouter([], [])
    first_turn_then_second(db, router)
    second_input = router.inputs[1]
    assert second_input[0].type == "system"  # the Router prompt
    assert texts(second_input[1:]) == [FIRST, FIRST_ANSWER, SECOND]  # turn 1 (both sides), then the new question


def test_the_router_sees_turn_one_after_a_restart(db):
    """New process: new graph, same file. The Router still gets the earlier turn."""
    m1 = Memory(db)
    svc1 = ChatService(build(m1.saver, RecordingRouter([]), synth=recording(FIRST_ANSWER)))
    turn(svc1, FIRST)
    m1.close()

    router = RecordingRouter([])
    m2 = Memory(db)
    try:
        turn(ChatService(build(m2.saver, router)), SECOND)
    finally:
        m2.close()
    assert texts(router.inputs[0][1:]) == [FIRST, FIRST_ANSWER, SECOND]


def test_support_gets_the_history_on_turn_two_when_the_plan_is_forced_to_support(db):
    support = recording("s1", "s2")
    router = RecordingRouter(["support"], ["support"])
    mem = Memory(db)
    try:
        svc = ChatService(build(mem.saver, router, support=support))
        turn(svc, FIRST)
        turn(svc, SECOND)
    finally:
        mem.close()
    second = support.seen[-1]
    system = next(str(m.content) for m in second if m.type == "system")
    assert USER in system  # the verified id is in the prompt, as always
    assert texts(m for m in second if m.type != "system") == [FIRST, "s1", SECOND]


def test_knowledge_gets_the_history_on_turn_two_when_the_plan_is_forced_to_knowledge(db):
    knowledge = recording("k1", "k1b", "k2", "k2b")  # each turn: an answer plus the nudge retry (no tool used)
    router = RecordingRouter(["knowledge"], ["knowledge"])
    mem = Memory(db)
    try:
        svc = ChatService(build(mem.saver, router, knowledge=knowledge))
        turn(svc, FIRST)
        turn(svc, SECOND)
    finally:
        mem.close()
    first_call_of_turn_two = next(c for c in knowledge.seen if texts(c)[-1] == SECOND)
    assert texts(m for m in first_call_of_turn_two if m.type != "system")[:3] == [FIRST, "k1b", SECOND]


def test_the_direct_answer_path_gets_the_history_on_turn_two(db):
    synth = recording(FIRST_ANSWER, "green")
    router = RecordingRouter([], [])
    mem = Memory(db)
    try:
        svc = ChatService(build(mem.saver, router, synth=synth))
        turn(svc, FIRST)
        state = turn(svc, SECOND)
    finally:
        mem.close()
    assert state["final_answer"] == "green"
    assert texts(m for m in synth.seen[-1] if m.type != "system") == [FIRST, FIRST_ANSWER, SECOND]


def test_tool_chatter_of_turn_one_is_not_in_the_history_of_turn_two(db):
    """Support called a tool on turn 1. Turn 2 sees the customer's words and the final answer, not the tool call."""
    support = RecordingModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "get_payout_schedule", "args": {}, "id": "c1"}]),
        AIMessage(content="Your money arrives tomorrow."), AIMessage(content="s2")]), seen=[])
    router = RecordingRouter(["support"], ["support"])
    repo = CustomerRepository(tickets_file=None, today=date(2026, 1, 15))
    mem = Memory(db)
    try:
        svc = ChatService(build(mem.saver, router, support=support, repo=repo))
        turn(svc, "When do I get paid?")
        turn(svc, "And the one after that?")
    finally:
        mem.close()
    second = support.seen[-1]
    assert not any(m.type == "tool" for m in second)
    assert texts(m for m in second if m.type != "system") == ["When do I get paid?", "Your money arrives tomorrow.", "And the one after that?"]


def test_the_window_keeps_the_last_messages_and_the_summary_goes_first():
    """Above the window the Router gets the last `history_window` messages; the rolling summary, if any, goes first."""
    window = get_settings().history_window
    messages = []
    for i in range(1, window + 3):  # more turns than the window holds
        messages += [HumanMessage(content=f"u{i}"), AIMessage(content=f"a{i}")]
    router = RecordingRouter([])
    make_router_node(router)({"messages": messages, "summary": "Customer likes green."})
    sent = router.inputs[0]
    assert sent[1].type == "system" and sent[1].content == SUMMARY_HEADER + "Customer likes green."
    assert texts(sent[2:]) == [str(m.content) for m in messages][-window:]
    assert isinstance(sent[0], SystemMessage)
