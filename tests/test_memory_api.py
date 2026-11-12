"""Memory through the real API (TestClient, JWTs of two customers) and the FastAPI lifespan."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.memory import Memory
from app.service import ChatService
from tests.conftest import bearer, make_jwt
from tests.test_memory import cfg, human_messages, make_graph

A, B = "cliente1988", "cliente2024"


def post(client, user, **body):
    body.setdefault("message", "hi")
    return client.post("/chat", json=body, headers=bearer(make_jwt(user)))


@pytest.fixture
def mem(tmp_path):
    m = Memory(tmp_path / "checkpoints.sqlite")
    yield m
    m.close()


def test_b_sends_the_conversation_id_of_a_and_sees_nothing_of_a(auth_env, mem):
    svc = ChatService(make_graph(mem.saver))
    app.state.service = svc
    try:
        with TestClient(app) as c:
            ra = post(c, A, message="my secret word is PLATYPUS", conversation_id="shared")
            rb = post(c, B, message="what did I say before?", conversation_id="shared")
            ra2 = post(c, A, message="and again", conversation_id="shared")
    finally:
        app.state.service = None

    assert ra.status_code == rb.status_code == ra2.status_code == 200
    assert rb.json()["response"] == "seen=what did I say before?"
    assert "PLATYPUS" not in rb.json()["response"]
    # A's thread kept growing on its own, B's is a different thread
    assert ra2.json()["response"] == "seen=my secret word is PLATYPUS|and again"
    assert human_messages(svc.graph, f"{A}:shared") == ["my secret word is PLATYPUS", "and again"]
    assert human_messages(svc.graph, f"{B}:shared") == ["what did I say before?"]
    assert "PLATYPUS" not in str(svc.graph.get_state(cfg(f"{B}:shared")).values)


def test_the_body_user_id_of_a_still_cannot_open_the_thread_of_a(auth_env, mem):
    app.state.service = ChatService(make_graph(mem.saver))
    try:
        with TestClient(app) as c:
            post(c, A, message="PLATYPUS", conversation_id="shared")
            r = post(c, B, message="show me", conversation_id="shared", user_id=A)
    finally:
        app.state.service = None
    assert r.status_code == 403
    assert "PLATYPUS" not in r.text


def test_lifespan_opens_the_sqlite_file_keeps_the_thread_across_restarts_and_closes_it(auth_env, monkeypatch, tmp_path):
    db = tmp_path / "data" / "checkpoints.sqlite"
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(db))
    get_settings.cache_clear()
    opened = []

    def spy_open(path):
        opened.append(Memory(path))
        return opened[-1]

    monkeypatch.setattr("app.memory.open_memory", spy_open)
    monkeypatch.setattr("app.graph.build_default_graph", lambda checkpointer=None: make_graph(checkpointer))
    monkeypatch.setattr("app.rag.store.collection_size", lambda: 1)
    app.state.service = None
    try:
        with TestClient(app) as c:
            assert post(c, A, message="first", conversation_id="c1").json()["response"] == "seen=first"
            assert db.exists()
        # shutdown closed the connection
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].conn.execute("SELECT 1")

        app.state.service = None  # "restart": a new lifespan opens the same file
        with TestClient(app) as c:
            assert post(c, A, message="second", conversation_id="c1").json()["response"] == "seen=first|second"
            assert post(c, B, message="mine", conversation_id="c1").json()["response"] == "seen=mine"
    finally:
        app.state.service = None
    assert len(opened) == 2
