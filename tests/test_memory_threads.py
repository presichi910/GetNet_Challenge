"""Thread registry: turns, listing, ownership-checked lookup and delete, and really erasing data.
Real SQLite, stub agents: no network, no keys."""
import sqlite3
from pathlib import Path

import pytest

from app.memory import DAY, Memory, ThreadLimitExceeded
from app.service import ChatService
from tests.test_memory import cfg, human_messages, make_graph

A, B = "cliente1988", "cliente2024"
T0 = 1_700_000_000.0


@pytest.fixture
def mem(tmp_path):
    m = Memory(tmp_path / "checkpoints.sqlite", ttl_days=30, max_threads_per_user=3)
    yield m
    m.close()


def raw_bytes(mem):
    """Everything on disk: the database file and its write-ahead log."""
    wal = Path(str(mem.path) + "-wal")
    return mem.path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")


def service(mem):
    return ChatService(make_graph(mem.saver), memory=mem)


def test_turns_are_counted_when_a_turn_is_answered(mem):
    svc = service(mem)
    for i in range(3):
        svc.handle(f"m{i}", A, conversation_id="c1")
    svc.handle("x", A, conversation_id="c2")
    by_id = {t["conversation_id"]: t for t in mem.list_threads(A)}
    assert by_id["c1"]["turns"] == 3 and by_id["c2"]["turns"] == 1


def test_a_failed_turn_is_not_counted(mem):
    svc = service(mem)
    svc.handle("ok", A, conversation_id="c1")
    mem.begin_turn(A, f"{A}:c1")  # accepted, but never answered
    assert mem.get_thread(A, f"{A}:c1")["turns"] == 1


def test_listing_is_only_the_users_own_most_recent_first_and_has_no_content(mem):
    mem.begin_turn(A, f"{A}:old", now=T0)
    mem.begin_turn(A, f"{A}:new", now=T0 + DAY)
    mem.begin_turn(B, f"{B}:other", now=T0 + 2 * DAY)
    listed = mem.list_threads(A)
    assert [t["conversation_id"] for t in listed] == ["new", "old"]
    assert all(set(t) == {"conversation_id", "created_at", "last_active", "turns"} for t in listed)
    assert [t["conversation_id"] for t in mem.list_threads(B)] == ["other"]
    assert mem.list_threads("nobody") == []


def test_get_thread_is_none_for_somebody_elses_or_a_missing_thread(mem):
    mem.begin_turn(A, f"{A}:c1")
    assert mem.get_thread(A, f"{A}:c1")["conversation_id"] == "c1"
    assert mem.get_thread(B, f"{A}:c1") is None  # A's key asked by B
    assert mem.get_thread(A, f"{A}:missing") is None


def test_delete_of_somebody_elses_or_a_missing_thread_changes_nothing(mem):
    svc = service(mem)
    svc.handle("A private", A, conversation_id="c1")
    assert mem.delete_thread_for(B, f"{A}:c1") is False
    assert mem.delete_thread_for(A, f"{A}:missing") is False
    assert mem.owner_of(f"{A}:c1") == A
    assert human_messages(svc.graph, f"{A}:c1") == ["A private"]


def test_delete_removes_the_registry_row_the_checkpoints_and_frees_the_slot(mem):
    svc = service(mem)
    for conv in ("c1", "c2", "c3"):
        svc.handle(f"hello {conv}", A, conversation_id=conv)
    with pytest.raises(ThreadLimitExceeded):
        mem.begin_turn(A, f"{A}:c4")

    assert mem.delete_thread_for(A, f"{A}:c2") is True
    assert mem.owner_of(f"{A}:c2") is None and mem.thread_count(A) == 2
    for table in ("checkpoints", "writes"):
        assert mem.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE thread_id = ?", (f"{A}:c2",)).fetchone()[0] == 0
    assert human_messages(svc.graph, f"{A}:c2") == []
    assert human_messages(svc.graph, f"{A}:c1") == ["hello c1"]  # the others are untouched
    assert svc.handle("a new one", A, conversation_id="c4").response == "seen=a new one"
    assert mem.delete_thread_for(A, f"{A}:c2") is False  # already gone


def test_secure_delete_is_on(mem):
    assert mem.conn.execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_a_deleted_conversation_leaves_no_trace_in_the_file_or_the_wal(mem):
    svc = service(mem)
    svc.handle("remember ZEBRA-TOKEN-4711 please", A, conversation_id="secret")
    svc.handle("something else", A, conversation_id="keep")
    assert b"ZEBRA-TOKEN-4711" in raw_bytes(mem)  # control: it really is on disk

    assert mem.delete_thread_for(A, f"{A}:secret") is True

    assert b"ZEBRA-TOKEN-4711" not in raw_bytes(mem)
    # and in every table, as text
    for (table,) in mem.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        for row in mem.conn.execute(f'SELECT * FROM "{table}"'):
            assert b"ZEBRA-TOKEN-4711" not in repr(row).encode()
    assert human_messages(svc.graph, f"{A}:keep") == ["something else"]  # the other one is intact


def test_a_purged_conversation_leaves_no_trace_either(mem):
    mem.begin_turn(A, f"{A}:old", now=T0)
    svc = service(mem)
    svc.run_raw("expiring YAK-TOKEN-0815", A, f"{A}:old")
    assert b"YAK-TOKEN-0815" in raw_bytes(mem)
    assert mem.purge_expired(now=T0 + 31 * DAY) == 1
    assert b"YAK-TOKEN-0815" not in raw_bytes(mem)


def test_a_file_from_before_the_turns_column_is_upgraded(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE threads (thread_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
                 "created_at REAL NOT NULL, last_active REAL NOT NULL)")
    conn.execute("INSERT INTO threads VALUES ('cliente1988:legacy', 'cliente1988', 1.0, 2.0)")
    conn.commit()
    conn.close()
    m = Memory(path)
    try:
        assert m.list_threads(A) == [{"conversation_id": "legacy", "created_at": 1.0, "last_active": 2.0, "turns": 0}]
        m.begin_turn(A, f"{A}:legacy")
        m.complete_turn(f"{A}:legacy")
        assert m.get_thread(A, f"{A}:legacy")["turns"] == 1
    finally:
        m.close()
