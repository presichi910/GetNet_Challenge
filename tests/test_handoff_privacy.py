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


# ---------------- the JSONL audit copy keeps no excerpt ----------------

def test_the_audit_file_has_no_conversation_text_but_keeps_who_when_and_why(client, store, audit):
    hid = open_case(client, A, "c1", f"I need a human, my secret is {TOKEN}")
    text = audit.read_text(encoding="utf-8")
    assert TOKEN not in text and "conversation" not in text.replace("conversation_id", "")
    rec = __import__("json").loads(text.strip())
    assert (rec["handoff_id"], rec["user_id"], rec["conversation_id"], rec["reason"], rec["redacted"]) == (
        hid, A, "c1", "user_requested", True)
    assert set(rec) == {"handoff_id", "created_at", "user_id", "conversation_id", "reason", "priority", "ticket_id",
                        "language", "tools", "redacted"}
    assert TOKEN.encode() in raw_bytes(store)  # the case itself is only in the queue, where it can be erased


def test_tool_arguments_do_not_reach_the_audit_file():
    from app.handoff import audit_record

    rec = audit_record({"handoff_id": "HND-1", "user_id": "u", "reason": "agent_unresolved", "user_message": "secret",
                        "tool_calls": [{"agent": "support", "name": "escalate_to_human", "args": {"reason": "secret"}}]})
    assert rec["tools"] == ["support:escalate_to_human"] and "secret" not in str(rec)


def test_the_default_notifier_of_the_node_is_the_redacted_one(repo, tmp_path, monkeypatch):
    from langchain_core.messages import HumanMessage

    from app.config import get_settings
    from app.handoff import make_handoff_node

    monkeypatch.setenv("HANDOFFS_FILE", str(tmp_path / "audit.jsonl"))
    get_settings.cache_clear()
    try:
        make_handoff_node(repo=repo)({"messages": [HumanMessage(content=f"human, {TOKEN}")], "user_id": A,
                                      "language": "English", "plan": [], "agent_outputs": {}, "wants_human": True})
    finally:
        get_settings.cache_clear()
    assert TOKEN not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


def test_the_importer_skips_audit_lines_it_wrote_itself(store, audit):
    from app.handoff import AuditNotifier

    AuditNotifier(audit).notify({"handoff_id": "HND-AAAAAA", "created_at": "2026-01-01T00:00:00+00:00", "user_id": A,
                                 "reason": "user_requested", "user_message": "x"})
    assert store.import_jsonl(audit) == {"imported": 0, "already_there": 0, "invalid": 0, "redacted": 1}
    assert store.list_queue() == []


# ---------------- retention: handoffs expire with THREAD_TTL_DAYS ----------------

DAY = 86400
T0 = 1_800_000_000.0  # a fixed "now" for the store tests


def seeded_old(store, hid, age_days, user=A, text="old case", base=T0):
    """A handoff created `age_days` before T0 (through the importer, which keeps the record's date)."""
    from datetime import datetime, timezone

    created = datetime.fromtimestamp(base - age_days * DAY, timezone.utc).isoformat()
    path = Path(str(store.path) + f".{hid}.jsonl")
    path.write_text(__import__("json").dumps({"handoff_id": hid, "created_at": created, "user_id": user,
                                              "reason": "user_requested", "user_message": text}) + "\n", encoding="utf-8")
    store.import_jsonl(path)
    path.unlink()


def test_idle_handoffs_expire_in_any_status_and_take_their_messages_and_events_with_them(store):
    seeded_old(store, "HND-OLDPEN", 40, text=f"never answered {TOKEN}")
    seeded_old(store, "HND-OLDRES", 40)
    store.claim("HND-OLDRES", "ops1", now=T0 - 39 * DAY)
    store.reply("HND-OLDRES", "ops1", "approve", f"old reply {REPLY_TOKEN}", "key-00000001", now=T0 - 39 * DAY)
    seeded_old(store, "HND-FRESH", 5)
    assert TOKEN.encode() in raw_bytes(store)

    assert store.purge_expired(30, now=T0) == 2

    assert [h["id"] for h in store.list_queue()] == ["HND-FRESH"]
    assert store.messages_for_conversation(A, "default") == []
    assert store.events("HND-OLDPEN") == [] and store.events("HND-OLDRES") == []
    assert [e["action"] for e in store.events("HND-FRESH")] == ["imported"]  # the fresh one keeps its trail
    blob = raw_bytes(store)
    assert TOKEN.encode() not in blob and REPLY_TOKEN.encode() not in blob


def test_recent_operator_activity_keeps_an_old_handoff_alive(store):
    seeded_old(store, "HND-OLD", 40)
    store.claim("HND-OLD", "ops1", now=T0 - 2 * DAY)  # an operator touched it two days ago
    assert store.purge_expired(30, now=T0) == 0
    assert store.get("HND-OLD") is not None


def test_ttl_zero_never_purges_handoffs_and_a_closed_store_is_a_quiet_noop(store, tmp_path):
    seeded_old(store, "HND-OLD", 4000)
    assert store.purge_expired(0, now=T0) == 0 and store.get("HND-OLD") is not None
    from app.handoff_store import HandoffStore

    s2 = HandoffStore(tmp_path / "other.sqlite")
    s2.close()
    assert s2.purge_expired(30) == 0


def test_the_lifespan_purges_expired_handoffs_at_startup(auth_env, monkeypatch, tmp_path):
    import time

    from fastapi.testclient import TestClient

    from app.handoff_store import HandoffStore
    from app.main import app
    from tests.test_retention import lifespan_env

    db = tmp_path / "checkpoints.sqlite"
    seed = HandoffStore(tmp_path / "handoffs.sqlite")  # where lifespan_env points the queue
    seeded_old(seed, "HND-OLD", 40, base=time.time())
    seeded_old(seed, "HND-NEW", 2, base=time.time())
    seed.close()

    lifespan_env(monkeypatch, db, THREAD_TTL_DAYS=30)
    try:
        with TestClient(app) as client:
            assert [h["id"] for h in client.get("/operator/handoffs", headers=op()).json()["handoffs"]] == ["HND-NEW"]
    finally:
        app.state.service = None


# ---------------- HANDOFF_IMPORT_JSONL ----------------

def _start_api(monkeypatch, tmp_path, **env):
    """Start the real lifespan on temp files, with a JSONL of three old cases."""
    import json as _json

    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.graph import build_graph
    from app.main import app

    old = tmp_path / "handoffs.jsonl"
    old.write_text("\n".join(_json.dumps({"handoff_id": f"HND-OLD00{i}", "created_at": "2026-01-01T10:00:00+00:00",
                                          "user_id": A, "reason": "user_requested", "user_message": f"old {i}"})
                             for i in (1, 2, 3)) + "\n", encoding="utf-8")
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(tmp_path / "checkpoints.sqlite"))
    monkeypatch.setenv("HANDOFFS_DB_PATH", str(tmp_path / "handoffs.sqlite"))
    monkeypatch.setenv("HANDOFFS_FILE", str(old))
    monkeypatch.setenv("THREAD_TTL_DAYS", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("app.graph.build_default_graph", lambda checkpointer=None, **_: build_graph(
        lambda s: {}, lambda s: {}, lambda s: {}, lambda s: {}, lambda s: {}, checkpointer=checkpointer))
    monkeypatch.setattr("app.rag.store.collection_size", lambda: 1)
    get_settings.cache_clear()
    app.state.service = None
    return TestClient(app), old


def test_the_old_file_is_not_imported_by_default_and_the_log_says_so(auth_env, monkeypatch, tmp_path, caplog):
    client, old = _start_api(monkeypatch, tmp_path)
    try:
        with client as c:
            assert c.get("/operator/handoffs", headers=op()).json()["count"] == 0
    finally:
        from app.main import app

        app.state.service = None
    assert "3 handoff records that are NOT in the operator queue" in caplog.text
    assert "HANDOFF_IMPORT_JSONL" in caplog.text
    assert old.read_text(encoding="utf-8").count("\n") == 3  # and the file itself is untouched


@pytest.mark.parametrize("value", ["true", "1", "True"])
def test_with_the_flag_on_it_imports_once_and_restarts_do_not_duplicate(auth_env, monkeypatch, tmp_path, value):
    from app.main import app

    client, _ = _start_api(monkeypatch, tmp_path, HANDOFF_IMPORT_JSONL=value)
    try:
        for _ in range(2):
            app.state.service = None
            with client as c:
                assert c.get("/operator/handoffs", headers=op()).json()["count"] == 3
    finally:
        app.state.service = None


def test_the_flag_defaults_to_off():
    from app.config import Settings

    assert Settings(_env_file=None).handoff_import_jsonl is False


def test_no_warning_when_the_file_holds_only_audit_lines_or_does_not_exist(auth_env, monkeypatch, tmp_path, caplog):
    from app.handoff import AuditNotifier
    from app.main import _warn_if_unimported

    _warn_if_unimported(str(tmp_path / "missing.jsonl"))
    AuditNotifier(tmp_path / "audit.jsonl").notify({"handoff_id": "HND-1", "user_id": A, "reason": "user_requested"})
    _warn_if_unimported(str(tmp_path / "audit.jsonl"))
    assert "NOT in the operator queue" not in caplog.text
