"""Retention: TTL purge, per-user thread limit (429), ownership. Real SQLite, stub agents, no network."""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.config import get_settings
from app.graph import build_graph
from app.main import app
from app.memory import DAY, Memory, ThreadLimitExceeded, ThreadOwnershipError
from app.service import ChatService
from tests.conftest import bearer, make_jwt
from tests.test_memory import cfg, human_messages, make_graph

A, B = "cliente1988", "cliente2024"
T0 = 1_700_000_000.0  # a fixed "now" for the TTL tests


@pytest.fixture
def db(tmp_path):
    return tmp_path / "checkpoints.sqlite"


def rows(mem, table, thread_id):
    return mem.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE thread_id = ?", (thread_id,)).fetchone()[0]


def talk(mem, user, conv, text, now=None):
    """One turn on a thread registered at `now`, like ChatService.handle does."""
    thread = f"{user}:{conv}"
    mem.begin_turn(user, thread, now=now)
    return ChatService(make_graph(mem.saver)).run_raw(text, user, thread)


# ---------------- TTL ----------------

def test_an_idle_thread_is_purged_with_its_checkpoints_and_the_rest_is_untouched(db):
    mem = Memory(db, ttl_days=30)
    try:
        talk(mem, A, "old", "very old", now=T0)
        talk(mem, A, "fresh", "fresh", now=T0 + 29 * DAY)
        talk(mem, B, "old", "B old", now=T0 + 10 * DAY)
        assert rows(mem, "checkpoints", f"{A}:old") > 0

        purged = mem.purge_expired(now=T0 + 31 * DAY)  # A:old is 31 days idle, the others are not

        assert purged == 1
        assert rows(mem, "checkpoints", f"{A}:old") == 0 and rows(mem, "writes", f"{A}:old") == 0
        assert mem.owner_of(f"{A}:old") is None
        g = make_graph(mem.saver)
        assert human_messages(g, f"{A}:old") == []
        assert human_messages(g, f"{A}:fresh") == ["fresh"] and human_messages(g, f"{B}:old") == ["B old"]
    finally:
        mem.close()


def test_activity_resets_the_clock(db):
    mem = Memory(db, ttl_days=30)
    try:
        talk(mem, A, "c1", "first", now=T0)
        talk(mem, A, "c1", "again", now=T0 + 20 * DAY)  # used again on day 20
        assert mem.purge_expired(now=T0 + 31 * DAY) == 0  # 11 days idle, kept
        assert human_messages(make_graph(mem.saver), f"{A}:c1") == ["first", "again"]
        assert mem.purge_expired(now=T0 + 51 * DAY) == 1  # 31 days idle
    finally:
        mem.close()


def test_ttl_zero_never_purges(db):
    mem = Memory(db, ttl_days=0)
    try:
        talk(mem, A, "c1", "x", now=T0)
        assert mem.purge_expired(now=T0 + 3650 * DAY) == 0
        assert rows(mem, "checkpoints", f"{A}:c1") > 0
    finally:
        mem.close()


def test_checkpoints_without_a_registry_row_are_purged_too(db):
    mem = Memory(db, ttl_days=30)
    try:
        ChatService(make_graph(mem.saver)).run_raw("leftover", A, f"{A}:ghost")  # never registered
        talk(mem, A, "kept", "kept", now=T0)
        assert mem.purge_expired(now=T0 + DAY) == 1
        assert rows(mem, "checkpoints", f"{A}:ghost") == 0 and rows(mem, "checkpoints", f"{A}:kept") > 0
    finally:
        mem.close()


def test_purge_after_close_is_a_quiet_noop(db):
    mem = Memory(db, ttl_days=30)
    mem.close()
    assert mem.purge_expired() == 0


# ---------------- limit per user ----------------

def test_the_limit_blocks_a_new_thread_but_never_deletes_or_blocks_existing_ones(db):
    mem = Memory(db, ttl_days=30, max_threads_per_user=3)
    try:
        for conv in ("c1", "c2", "c3"):
            talk(mem, A, conv, f"hello {conv}")
        with pytest.raises(ThreadLimitExceeded) as exc:
            mem.begin_turn(A, f"{A}:c4")
        assert "3 conversations" in str(exc.value) and "No conversation was deleted" in str(exc.value)
        assert "30 days" in str(exc.value)

        assert mem.thread_count(A) == 3  # nothing was deleted to make room
        for conv in ("c1", "c2", "c3"):
            assert human_messages(make_graph(mem.saver), f"{A}:{conv}") == [f"hello {conv}"]
        talk(mem, A, "c2", "still works")  # an existing thread keeps working at the limit
        assert human_messages(make_graph(mem.saver), f"{A}:c2") == ["hello c2", "still works"]
        talk(mem, B, "c1", "B is not affected")  # the limit is per user
        assert mem.thread_count(B) == 1
    finally:
        mem.close()


def test_the_default_conversation_counts_as_one_thread(db):
    mem = Memory(db, max_threads_per_user=2)
    try:
        talk(mem, A, "default", "x")
        talk(mem, A, "other", "y")
        with pytest.raises(ThreadLimitExceeded):
            mem.begin_turn(A, f"{A}:third")
    finally:
        mem.close()


def test_limit_zero_means_unlimited(db):
    mem = Memory(db, max_threads_per_user=0)
    try:
        for i in range(30):
            mem.begin_turn(A, f"{A}:c{i}")
        assert mem.thread_count(A) == 30
    finally:
        mem.close()


def test_a_slot_frees_when_a_thread_expires(db):
    mem = Memory(db, ttl_days=30, max_threads_per_user=1)
    try:
        talk(mem, A, "c1", "x", now=T0)
        with pytest.raises(ThreadLimitExceeded):
            mem.begin_turn(A, f"{A}:c2", now=T0 + DAY)
        mem.purge_expired(now=T0 + 31 * DAY)
        assert mem.begin_turn(A, f"{A}:c2", now=T0 + 31 * DAY) is True
    finally:
        mem.close()


def test_the_limit_holds_under_concurrent_requests(db):
    mem = Memory(db, max_threads_per_user=5)
    try:
        def try_new(i):
            try:
                return mem.begin_turn(A, f"{A}:c{i}")
            except ThreadLimitExceeded:
                return None

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(try_new, range(40)))
        assert results.count(True) == 5 and results.count(None) == 35
        assert mem.thread_count(A) == 5
    finally:
        mem.close()


def test_a_failed_first_turn_gives_the_slot_back_and_leaves_nothing(db):
    def router(state):
        return {"plan": [], "step": 0, "language": "English", "routing_reasoning": "s", "agent_outputs": {}, "final_answer": ""}

    def synthesizer(state):
        if state["messages"][-1].content == "boom":
            raise RuntimeError("boom")
        return {"final_answer": "ok", "messages": [AIMessage(content="ok")]}

    mem = Memory(db, max_threads_per_user=1)
    try:
        svc = ChatService(build_graph(router, lambda s: {}, lambda s: {}, synthesizer, lambda s: {},
                                      checkpointer=mem.saver), memory=mem)
        with pytest.raises(RuntimeError):
            svc.handle("boom", A, conversation_id="c1")
        assert mem.thread_count(A) == 0 and rows(mem, "checkpoints", f"{A}:c1") == 0
        svc.handle("fine", A, conversation_id="c2")  # the only slot was free
        with pytest.raises(RuntimeError):
            svc.handle("boom", A, conversation_id="c2")  # an existing thread keeps its slot when a later turn fails
        assert mem.thread_count(A) == 1
    finally:
        mem.close()


# ---------------- ownership ----------------

def test_a_thread_is_owned_by_its_user_and_nobody_else_can_use_it(db):
    mem = Memory(db)
    try:
        mem.begin_turn(A, f"{A}:c1")
        assert mem.owner_of(f"{A}:c1") == A
        with pytest.raises(ThreadOwnershipError):
            mem.begin_turn(B, f"{A}:c1")  # B asks for A's key
        mem.conn.execute("UPDATE threads SET user_id = ? WHERE thread_id = ?", (B, f"{A}:c1"))  # corrupted registry
        with pytest.raises(ThreadOwnershipError):
            mem.begin_turn(A, f"{A}:c1")
    finally:
        mem.close()


# ---------------- through the API and the lifespan ----------------

def post(client, user, **body):
    body.setdefault("message", "hi")
    return client.post("/chat", json=body, headers=bearer(make_jwt(user)))


def test_the_api_answers_429_with_a_clear_message_and_runs_nothing(auth_env, db):
    mem = Memory(db, ttl_days=30, max_threads_per_user=2)
    svc = ChatService(make_graph(mem.saver), memory=mem)
    app.state.service = svc
    try:
        with TestClient(app) as c:
            assert post(c, A, conversation_id="c1").status_code == 200
            assert post(c, A, conversation_id="c2").status_code == 200
            r = post(c, A, message="a third one", conversation_id="c3")
            assert r.status_code == 429
            assert "2 conversations" in r.json()["detail"] and "No conversation was deleted" in r.json()["detail"]
            assert post(c, A, conversation_id="c1").status_code == 200  # existing threads still work
            assert post(c, B, conversation_id="c3").status_code == 200  # another customer is not affected
    finally:
        app.state.service = None
        mem.close()
    mem2 = Memory(db)
    try:
        assert human_messages(make_graph(mem2.saver), f"{A}:c3") == []  # the refused message left no trace
        assert mem2.thread_count(A) == 2
    finally:
        mem2.close()


def lifespan_env(monkeypatch, db, **env):
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(db))
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    get_settings.cache_clear()
    opened = []

    def spy_open(path, **limits):
        opened.append(Memory(path, **limits))
        return opened[-1]

    monkeypatch.setattr("app.memory.open_memory", spy_open)
    monkeypatch.setattr("app.graph.build_default_graph", lambda checkpointer=None: make_graph(checkpointer))
    monkeypatch.setattr("app.rag.store.collection_size", lambda: 1)
    app.state.service = None
    return opened


def test_the_lifespan_purges_expired_threads_at_startup_and_passes_the_limits(auth_env, monkeypatch, db):
    seed = Memory(db, ttl_days=30)
    talk(seed, A, "ancient", "old data", now=time.time() - 40 * DAY)
    talk(seed, A, "recent", "recent data", now=time.time() - 1 * DAY)
    seed.close()

    opened = lifespan_env(monkeypatch, db, THREAD_TTL_DAYS=30, MAX_THREADS_PER_USER=7)
    try:
        with TestClient(app):
            mem = opened[0]
            assert (mem.ttl_days, mem.max_threads) == (30, 7)
            assert mem.owner_of(f"{A}:ancient") is None and mem.owner_of(f"{A}:recent") == A
            assert human_messages(make_graph(mem.saver), f"{A}:ancient") == []
    finally:
        app.state.service = None


def test_the_periodic_task_purges_while_the_api_runs(auth_env, monkeypatch, db):
    opened = lifespan_env(monkeypatch, db, THREAD_TTL_DAYS=30, PURGE_INTERVAL_MINUTES=0.0005)  # ~30 ms
    try:
        with TestClient(app):
            mem = opened[0]
            talk(mem, A, "late", "expires while running", now=time.time() - 40 * DAY)
            deadline = time.time() + 5
            while time.time() < deadline and mem.owner_of(f"{A}:late") is not None:
                time.sleep(0.05)
            assert mem.owner_of(f"{A}:late") is None
            assert rows(mem, "checkpoints", f"{A}:late") == 0
    finally:
        app.state.service = None


def test_ttl_zero_in_the_environment_starts_no_purge_and_limit_zero_is_unlimited(auth_env, monkeypatch, db):
    opened = lifespan_env(monkeypatch, db, THREAD_TTL_DAYS=0, MAX_THREADS_PER_USER=0)
    try:
        with TestClient(app) as c:
            for i in range(25):
                assert post(c, A, conversation_id=f"c{i}").status_code == 200
            assert opened[0].thread_count(A) == 25
    finally:
        app.state.service = None
