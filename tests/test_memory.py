"""Persistent memory (SqliteSaver). Real graph and real SQLite file, stub agents: no network, no keys."""
import sqlite3

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.graph import build_graph
from app.memory import Memory
from app.service import ChatService


def make_graph(saver):
    """Direct-answer graph whose reply lists every user message the thread remembers.
    So a reply shows exactly which history the thread had."""

    def router(state):
        return {"plan": [], "step": 0, "language": "English", "routing_reasoning": "stub",
                "agent_outputs": {}, "final_answer": ""}

    def synthesizer(state):
        seen = [m.content for m in state["messages"] if isinstance(m, HumanMessage)]
        answer = "seen=" + "|".join(seen)
        return {"final_answer": answer, "messages": [AIMessage(content=answer)]}

    return build_graph(router, lambda s: {}, lambda s: {}, synthesizer, lambda s: {}, checkpointer=saver)


def cfg(thread_id):
    return {"configurable": {"thread_id": thread_id}}


def human_messages(graph, thread_id):
    values = graph.get_state(cfg(thread_id)).values
    return [m.content for m in values.get("messages", []) if isinstance(m, HumanMessage)]


@pytest.fixture
def db(tmp_path):
    return tmp_path / "data" / "checkpoints.sqlite"  # the parent folder does not exist yet


def test_the_file_and_its_folder_are_created(db):
    mem = Memory(db)
    try:
        assert db.exists()
        assert mem.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        mem.close()


def test_a_conversation_survives_rebuilding_the_graph_on_the_same_file(db):
    """Simulates a restart: close everything, open the same file, build a new graph."""
    m1 = Memory(db)
    ChatService(make_graph(m1.saver)).handle("remember this", "alice", conversation_id="c1")
    m1.close()

    m2 = Memory(db)
    try:
        svc2 = ChatService(make_graph(m2.saver))
        r = svc2.handle("and this", "alice", conversation_id="c1")
        assert r.response == "seen=remember this|and this"
        assert human_messages(svc2.graph, "alice:c1") == ["remember this", "and this"]
    finally:
        m2.close()


def test_the_memory_is_gone_on_a_different_file(db, tmp_path):
    m1 = Memory(db)
    ChatService(make_graph(m1.saver)).handle("remember this", "alice")
    m1.close()
    m2 = Memory(tmp_path / "other.sqlite")
    try:
        assert ChatService(make_graph(m2.saver)).handle("hi", "alice").response == "seen=hi"
    finally:
        m2.close()


def test_two_users_never_cross_threads_with_the_persistent_saver(db):
    m1 = Memory(db)
    svc = ChatService(make_graph(m1.saver))
    svc.handle("secret of A", "cliente1988", conversation_id="same")
    rb = svc.handle("hello from B", "cliente2024", conversation_id="same")  # the same conversation_id
    assert rb.response == "seen=hello from B"  # nothing of A
    rb_default = svc.handle("B default", "cliente2024")
    assert rb_default.response == "seen=B default"
    m1.close()

    # and after a restart too
    m2 = Memory(db)
    try:
        g = make_graph(m2.saver)
        assert human_messages(g, "cliente1988:same") == ["secret of A"]
        assert human_messages(g, "cliente2024:same") == ["hello from B"]
        assert human_messages(g, "cliente2024:default") == ["B default"]
        assert "secret of A" not in str(g.get_state(cfg("cliente2024:same")).values)
    finally:
        m2.close()


def test_conversations_of_one_user_stay_separate_after_a_restart(db):
    m1 = Memory(db)
    svc = ChatService(make_graph(m1.saver))
    svc.handle("one", "alice", conversation_id="c1")
    svc.handle("two", "alice", conversation_id="c2")
    m1.close()
    m2 = Memory(db)
    try:
        g = make_graph(m2.saver)
        assert human_messages(g, "alice:c1") == ["one"] and human_messages(g, "alice:c2") == ["two"]
    finally:
        m2.close()


def test_close_releases_the_file_and_is_idempotent(db):
    mem = Memory(db)
    mem.close()
    mem.close()  # no error
    with pytest.raises(sqlite3.ProgrammingError):
        mem.conn.execute("SELECT 1")
