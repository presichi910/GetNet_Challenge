"""GET /conversations, GET /conversations/{id}, DELETE /conversations/{id}.
TestClient with the JWTs of two customers, real SQLite, stub agents: no network, no keys."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.memory import Memory
from app.service import ChatService
from tests.conftest import bearer, make_jwt
from tests.test_memory import make_graph

A, B = "cliente1988", "cliente2024"
TOKEN = "ZEBRA-TOKEN-4711"
NOT_FOUND = (404, '{"detail":"Conversation not found."}')


@pytest.fixture
def mem(tmp_path):
    m = Memory(tmp_path / "checkpoints.sqlite", ttl_days=30, max_threads_per_user=3)
    yield m
    m.close()


@pytest.fixture
def client(auth_env, mem):
    app.state.service = ChatService(make_graph(mem.saver), memory=mem)
    with TestClient(app) as c:
        yield c
    app.state.service = None


def h(user, **kw):
    return bearer(make_jwt(user, **kw))


def chat(client, user, message="hi", conv=None):
    body = {"message": message, **({"conversation_id": conv} if conv else {})}
    r = client.post("/chat", json=body, headers=h(user))
    assert r.status_code == 200, r.text
    return r


def raw_bytes(mem):
    wal = Path(str(mem.path) + "-wal")
    return mem.path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")


# ---------------- listing ----------------

def test_list_has_only_the_callers_conversations_without_content(client):
    chat(client, A, f"my word is {TOKEN}", "c1")
    chat(client, A, "again", "c1")
    chat(client, A, "x", "c2")
    chat(client, B, "B private", "c1")  # same conversation_id as one of A's

    r = client.get("/conversations", headers=h(A))
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2 and body["limit"] == 3
    by_id = {c["conversation_id"]: c for c in body["conversations"]}
    assert set(by_id) == {"c1", "c2"} and by_id["c1"]["turns"] == 2 and by_id["c2"]["turns"] == 1
    assert all(set(c) == {"conversation_id", "created_at", "last_active", "turns"} for c in body["conversations"])
    assert body["conversations"][0]["conversation_id"] == "c2"  # most recent first
    assert TOKEN not in r.text and "B private" not in r.text and "messages" not in r.text

    rb = client.get("/conversations", headers=h(B)).json()
    assert rb["count"] == 1 and rb["conversations"][0]["conversation_id"] == "c1" and rb["conversations"][0]["turns"] == 1


def test_list_is_empty_for_a_customer_with_no_conversations_and_the_default_one_is_listed(client):
    assert client.get("/conversations", headers=h(A)).json()["count"] == 0
    chat(client, A, "no conversation_id")
    listed = client.get("/conversations", headers=h(A)).json()["conversations"]
    assert [c["conversation_id"] for c in listed] == ["default"]


# ---------------- reading one ----------------

def test_get_returns_the_history_of_the_callers_thread(client):
    chat(client, A, "first question", "c1")
    chat(client, A, "second question", "c1")
    r = client.get("/conversations/c1", headers=h(A))
    assert r.status_code == 200
    body = r.json()
    assert body["conversation_id"] == "c1" and body["turns"] == 2 and body["summary"] is None
    assert [(m["role"], m["content"]) for m in body["messages"]] == [
        ("user", "first question"), ("assistant", "seen=first question"),
        ("user", "second question"), ("assistant", "seen=first question|second question")]


def test_get_shows_the_summary_and_only_the_window_once_it_exists(auth_env, mem, monkeypatch):
    from tests.test_summary import FakeSummaryLLM
    from tests.test_summary import make_graph as make_summary_graph

    monkeypatch.setenv("SUMMARY_THRESHOLD", "6")
    monkeypatch.setenv("HISTORY_WINDOW", "2")
    get_settings.cache_clear()
    app.state.service = ChatService(make_summary_graph(FakeSummaryLLM(), mem.saver), memory=mem)
    try:
        with TestClient(app) as c:
            for i in range(5):
                chat(c, A, f"msg{i}", "long")
            body = c.get("/conversations/long", headers=h(A)).json()
    finally:
        app.state.service = None
    assert body["turns"] == 5 and body["summary"].startswith("SUMMARY#")
    # the summary fired on turn 4 and turn 5 added two messages: the oldest turns are gone from the list
    texts = [m["content"] for m in body["messages"]]
    assert len(texts) == 5 and not {"msg0", "msg1", "msg2"} & set(texts)
    assert texts[1] == "msg3" and texts[3] == "msg4" and body["messages"][-1]["role"] == "assistant"


# ---------------- somebody else's, missing, malformed: the same 404 ----------------

def test_b_cannot_read_or_delete_the_thread_of_a_and_it_stays_intact(client, mem):
    chat(client, A, f"private {TOKEN}", "shared-id")
    before = client.get("/conversations/shared-id", headers=h(A)).json()

    rg = client.get("/conversations/shared-id", headers=h(B))
    rd = client.delete("/conversations/shared-id", headers=h(B))
    assert rg.status_code == 404 and rd.status_code == 404
    assert TOKEN not in rg.text + rd.text

    assert client.get("/conversations/shared-id", headers=h(A)).json() == before  # untouched
    assert mem.owner_of(f"{A}:shared-id") == A and TOKEN.encode() in raw_bytes(mem)
    assert client.get("/conversations", headers=h(B)).json()["count"] == 0  # B did not gain a thread


def test_somebody_elses_missing_and_malformed_ids_are_indistinguishable(client):
    chat(client, A, "x", "a-only")
    seen = set()
    # somebody else's, missing, and ids with a bad character or length: one and the same answer
    for conv in ("a-only", "does-not-exist", "bad%20id", "x" * 65, "a%3Ab"):
        for method in ("get", "delete"):
            r = getattr(client, method)(f"/conversations/{conv}", headers=h(B))
            seen.add((r.status_code, r.text))
    assert seen == {NOT_FOUND}
    # an encoded "/" never reaches the handler (FastAPI's generic 404): no id can contain it, so it
    # says nothing about any conversation
    for method in ("get", "delete"):
        assert getattr(client, method)("/conversations/..%2F..%2Fetc", headers=h(B)).status_code == 404


def test_the_owner_comes_from_the_token_never_from_the_path_query_or_body(client):
    chat(client, A, f"private {TOKEN}", "c1")
    # B tries to say "I am A" in every place the request has
    r = client.get(f"/conversations/c1?user_id={A}&owner={A}", headers={**h(B), "X-User-Id": A})
    assert r.status_code == 404
    r = client.request("DELETE", "/conversations/c1", json={"user_id": A}, headers={**h(B), "X-User-Id": A})
    assert r.status_code == 404
    assert client.get("/conversations/c1", headers=h(A)).status_code == 200  # still there
    # there is no route that takes a user in the path
    assert client.get(f"/conversations/{A}/c1", headers=h(B)).status_code == 404
    assert client.get(f"/users/{A}/conversations", headers=h(B)).status_code == 404


# ---------------- authentication ----------------

@pytest.mark.parametrize("method,path", [("get", "/conversations"), ("get", "/conversations/c1"),
                                         ("delete", "/conversations/c1")])
def test_all_three_need_a_valid_token_with_the_chat_scope(client, method, path):
    chat(client, A, "x", "c1")
    call = getattr(client, method)
    assert call(path).status_code == 401  # no token
    assert call(path, headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401
    assert call(path, headers=h(A, key="another-secret-another-secret-another-1")).status_code == 401  # forged
    assert call(path, headers=h(A, minutes=-5)).status_code == 401  # expired
    assert call(path, headers=h(A, scope="other")).status_code == 403  # valid token, wrong scope
    assert call(path, headers=h(A, scope=None)).status_code == 403  # no scope claim
    assert client.get("/conversations/c1", headers=h(A)).status_code == 200  # nothing above changed anything


# ---------------- delete ----------------

def test_delete_answers_204_and_removes_every_trace_from_the_database(client, mem):
    chat(client, A, f"remember {TOKEN} please", "secret")
    chat(client, A, "something else", "keep")
    assert TOKEN.encode() in raw_bytes(mem)  # control

    r = client.delete("/conversations/secret", headers=h(A))
    assert r.status_code == 204 and r.content == b""

    assert TOKEN.encode() not in raw_bytes(mem)  # not in the file, not in the WAL
    for (table,) in mem.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        for row in mem.conn.execute(f'SELECT * FROM "{table}"'):
            assert TOKEN not in repr(row)
    assert client.get("/conversations/secret", headers=h(A)).status_code == 404
    listed = client.get("/conversations", headers=h(A)).json()["conversations"]
    assert [c["conversation_id"] for c in listed] == ["keep"]
    assert client.get("/conversations/keep", headers=h(A)).status_code == 200
    assert client.delete("/conversations/secret", headers=h(A)).status_code == 404  # second time: gone


def test_after_a_delete_the_customer_can_start_a_new_conversation_even_if_at_the_limit(client):
    for conv in ("c1", "c2", "c3"):
        chat(client, A, "x", conv)
    full = client.post("/chat", json={"message": "one more", "conversation_id": "c4"}, headers=h(A))
    assert full.status_code == 429  # at the limit (3)

    assert client.delete("/conversations/c2", headers=h(A)).status_code == 204
    assert chat(client, A, "one more", "c4").json()["conversation_id"] == "c4"
    assert client.get("/conversations", headers=h(A)).json()["count"] == 3


def test_a_deleted_conversation_starts_from_nothing_when_the_id_is_reused(client):
    chat(client, A, f"old {TOKEN}", "c1")
    client.delete("/conversations/c1", headers=h(A))
    r = chat(client, A, "new start", "c1")
    assert r.json()["response"] == "seen=new start" and TOKEN not in r.text


# ---------------- a service without a registry ----------------

def test_without_a_registry_there_is_nothing_to_list_read_or_delete(auth_env):
    app.state.service = ChatService(make_graph(None))
    try:
        with TestClient(app) as c:
            assert c.get("/conversations", headers=h(A)).json() == {"conversations": [], "count": 0, "limit": None}
            assert c.get("/conversations/c1", headers=h(A)).status_code == 404
            assert c.delete("/conversations/c1", headers=h(A)).status_code == 404
    finally:
        app.state.service = None
