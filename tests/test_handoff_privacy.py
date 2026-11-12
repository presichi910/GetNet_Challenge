"""Privacy of the human queue: deleting a conversation erases its handoffs, the JSONL keeps no excerpt,
and idle handoffs expire with THREAD_TTL_DAYS. Real SQLite, stub agents, no network."""
from pathlib import Path

import pytest

from tests.test_operator_api import (A, B, OP1, audit, chat, claim, client, m, op, open_case, parts,  # noqa: F401
                                     reply, store)

TOKEN = "ZEBRA-TOKEN-4711"
REPLY_TOKEN = "OTTER-REPLY-9021"


def raw_bytes(store):
    wal = Path(str(store.path) + "-wal")
    return store.path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")


def delete(client, user, conv):
    return client.delete(f"/conversations/{conv}", headers=m(user))


def test_deleting_a_conversation_erases_its_handoffs_messages_and_events_from_the_file_and_its_wal(client, store):
    hid = open_case(client, A, "c1", f"I need a human, my secret is {TOKEN}")
    claim(client, hid)
    reply(client, hid, message=f"Checked. {REPLY_TOKEN}")
    assert TOKEN.encode() in raw_bytes(store) and REPLY_TOKEN.encode() in raw_bytes(store)  # the test can see it

    assert delete(client, A, "c1").status_code == 204

    blob = raw_bytes(store)
    assert TOKEN.encode() not in blob and REPLY_TOKEN.encode() not in blob
    assert store.get(hid) is None and store.events(hid) == []
    assert store.messages_for_conversation(A, "c1") == []
    assert client.get(f"/operator/handoffs/{hid}", headers=op()).status_code == 404
    assert client.get("/conversations/c1/operator-messages", headers=m(A)).status_code == 404
    assert not Path(str(store.path) + "-wal").exists() or Path(str(store.path) + "-wal").stat().st_size == 0


def test_only_the_handoffs_of_that_conversation_and_that_merchant_go(client, store):
    mine_other = open_case(client, A, "c2", "human please, other conversation")
    theirs = open_case(client, B, "c1", "human please, same conversation id, other merchant")
    gone = open_case(client, A, "c1", "human please")
    assert delete(client, A, "c1").status_code == 204
    assert store.get(gone) is None
    assert store.get(mine_other) is not None and store.get(theirs) is not None
    assert [e["action"] for e in store.events(theirs)] == ["created"]  # the other merchant's trail is intact


def test_a_merchant_can_not_erase_somebody_elses_handoffs_by_guessing_the_conversation_id(client, store):
    hid = open_case(client, A, "c1")
    chat(client, B, "hello", "c1")  # B has a conversation with the same id, and no handoff in it
    assert delete(client, B, "c1").status_code == 204  # B deletes B's conversation
    assert store.get(hid) is not None
    assert delete(client, B, "never-existed").status_code == 404


def test_handoffs_left_without_their_conversation_are_still_erased_and_a_retry_finishes_the_job(client, store, parts):
    mem = parts[0]
    hid = open_case(client, A, "c1", f"human, {TOKEN}")
    assert mem.delete_thread_for(A, f"{A}:c1")  # the thread went (TTL purge, or a DELETE that failed half way)
    assert store.get(hid) is not None
    assert delete(client, A, "c1").status_code == 204  # nothing of the thread left, but the handoff is the caller's
    assert store.get(hid) is None and TOKEN.encode() not in raw_bytes(store)
    assert delete(client, A, "c1").status_code == 404  # now there is really nothing


def test_a_failure_while_erasing_is_a_500_not_a_204_with_a_copy_left_behind(client, store, monkeypatch):
    open_case(client, A, "c1")

    def boom(*a, **k):
        raise RuntimeError("disk")

    monkeypatch.setattr(store, "delete_conversation", boom)
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.delete("/conversations/c1", headers=m(A)).status_code == 500
