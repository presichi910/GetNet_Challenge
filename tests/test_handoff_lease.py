"""Claim leases: a claim holds for a while, then the handoff is open again. Injected clock, no sleeping."""
import sqlite3
import threading

import pytest

from app.handoff_store import HandoffConflict, HandoffStore
from tests.test_handoff_store import record

T0 = 1_800_000_000.0
LEASE = 900.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = HandoffStore(tmp_path / "h.sqlite", lease_seconds=LEASE, clock=clock)
    s.notify(record())
    yield s
    s.close()


def test_a_claim_gets_a_lease_of_the_configured_length(store):
    h = store.claim("HND-AAAAAA", "ops1")
    assert h["claimed_at"] == T0 and h["lease_expires_at"] == T0 + LEASE


def test_the_default_lease_is_fifteen_minutes(tmp_path):
    s = HandoffStore(tmp_path / "x.sqlite")
    try:
        assert s.lease_seconds == 900
    finally:
        s.close()


@pytest.mark.parametrize("bad", [0, -5])
def test_a_lease_must_be_positive(tmp_path, bad):
    with pytest.raises(ValueError):
        HandoffStore(tmp_path / "x.sqlite", lease_seconds=bad)


def test_while_the_lease_holds_nobody_else_can_claim_or_reply(store, clock):
    store.claim("HND-AAAAAA", "ops1")
    clock.advance(LEASE - 1)
    with pytest.raises(HandoffConflict):
        store.claim("HND-AAAAAA", "ops2")
    with pytest.raises(HandoffConflict):
        store.reply("HND-AAAAAA", "ops2", "approve", "x", "key-00000001")
    assert store.get("HND-AAAAAA")["claimed_by"] == "ops1"


def test_an_expired_claim_is_open_again_when_listing(store, clock):
    store.claim("HND-AAAAAA", "ops1")
    clock.advance(LEASE)  # exactly at the end: expired
    queue = store.list_queue("pending")
    assert [h["id"] for h in queue] == ["HND-AAAAAA"]
    assert queue[0]["claimed_by"] is None and queue[0]["claimed_at"] is None and queue[0]["lease_expires_at"] is None
    assert store.list_queue("claimed") == []


def test_an_expired_claim_can_be_claimed_by_another_operator(store, clock):
    store.claim("HND-AAAAAA", "ops1")
    clock.advance(LEASE + 1)
    h = store.claim("HND-AAAAAA", "ops2")
    assert (h["claimed_by"], h["claimed_at"], h["lease_expires_at"]) == ("ops2", T0 + LEASE + 1, T0 + 2 * LEASE + 1)


def test_the_operator_whose_lease_expired_can_no_longer_reply_but_can_claim_again(store, clock):
    store.claim("HND-AAAAAA", "ops1")
    clock.advance(LEASE + 1)
    with pytest.raises(HandoffConflict, match="Claim the handoff"):
        store.reply("HND-AAAAAA", "ops1", "approve", "late", "key-00000001")
    assert store.messages_for_conversation("cliente1988", "c1") == []
    store.claim("HND-AAAAAA", "ops1")
    h, _ = store.reply("HND-AAAAAA", "ops1", "approve", "on time", "key-00000001")
    assert h["status"] == "resolved" and h["lease_expires_at"] is None


def test_the_first_operator_can_not_reply_over_the_one_who_took_over(store, clock):
    store.claim("HND-AAAAAA", "ops1")
    clock.advance(LEASE + 1)
    store.claim("HND-AAAAAA", "ops2")
    with pytest.raises(HandoffConflict):
        store.reply("HND-AAAAAA", "ops1", "approve", "x", "key-00000001")
    store.reply("HND-AAAAAA", "ops2", "approve", "y", "key-00000002")


def test_claiming_again_before_the_end_renews_the_lease_and_keeps_claimed_at(store, clock):
    store.claim("HND-AAAAAA", "ops1")
    clock.advance(LEASE - 10)
    again = store.claim("HND-AAAAAA", "ops1")
    assert again["claimed_at"] == T0 and again["lease_expires_at"] == clock.t + LEASE
    clock.advance(20)  # past the first lease, inside the renewed one
    assert store.get("HND-AAAAAA")["status"] == "claimed"
    assert [e["action"] for e in store.events("HND-AAAAAA")] == ["created", "claim"]  # a renewal is not a new claim


def test_expiry_is_recorded_with_the_operator_who_lost_it(store, clock):
    store.claim("HND-AAAAAA", "ops1")
    clock.advance(LEASE + 5)
    store.list_queue()
    store.list_queue()  # a second sweep does not record it twice
    ev = store.events("HND-AAAAAA")
    assert [(e["actor"], e["action"], e["detail"]) for e in ev] == [
        ("system", "created", "user_requested"), ("ops1", "claim", None), ("system", "lease_expired", "ops1")]
    assert ev[-1]["at"] == T0 + LEASE + 5


def test_only_the_expired_claims_go_back(store, clock):
    store.notify(record("HND-BBBBBB"))
    store.claim("HND-AAAAAA", "ops1")
    clock.advance(600)
    store.claim("HND-BBBBBB", "ops2")
    clock.advance(400)  # A: 1000 s old (expired), B: 400 s old (valid)
    assert {h["id"]: h["status"] for h in store.list_queue()} == {"HND-AAAAAA": "pending", "HND-BBBBBB": "claimed"}


def test_the_customer_side_sees_pending_again_after_expiry(store, clock):
    store.claim("HND-AAAAAA", "ops1")
    assert store.status_for("cliente1988", "HND-AAAAAA") == "claimed"
    assert store.open_for_conversation("cliente1988", "c1") == [{"id": "HND-AAAAAA", "status": "claimed"}]
    clock.advance(LEASE)
    assert store.status_for("cliente1988", "HND-AAAAAA") == "pending"
    assert store.open_for_conversation("cliente1988", "c1") == [{"id": "HND-AAAAAA", "status": "pending"}]


def test_a_resolved_handoff_never_expires(store, clock):
    store.claim("HND-AAAAAA", "ops1")
    store.reply("HND-AAAAAA", "ops1", "approve", "done", "key-00000001")
    clock.advance(100 * LEASE)
    assert store.get("HND-AAAAAA")["status"] == "resolved"


def test_two_operators_taking_an_expired_claim_at_once_one_wins(tmp_path, clock):
    path = tmp_path / "h.sqlite"
    stores = [HandoffStore(path, lease_seconds=LEASE, clock=clock) for _ in range(3)]
    stores[0].notify(record())
    stores[0].claim("HND-AAAAAA", "ops0")
    clock.advance(LEASE + 1)
    barrier, out, errs = threading.Barrier(3), [], []

    def go(s, who):
        barrier.wait()
        try:
            out.append(s.claim("HND-AAAAAA", who))
        except HandoffConflict as e:
            errs.append(e)

    threads = [threading.Thread(target=go, args=(s, f"ops{i + 1}")) for i, s in enumerate(stores)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    try:
        assert len(out) == 1 and len(errs) == 2
        assert [e["actor"] for e in stores[0].events("HND-AAAAAA") if e["action"] == "claim"] == ["ops0", out[0]["claimed_by"]]
    finally:
        for s in stores:
            s.close()


def test_a_file_from_before_leases_gets_the_column_and_its_old_claims_open_again(tmp_path, clock):
    path = tmp_path / "old.sqlite"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE handoffs (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, merchant_id TEXT NOT NULL,
            reason TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'normal', summary TEXT NOT NULL, record TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', claimed_by TEXT, claimed_at REAL, resolution TEXT,
            operator_reply TEXT, resolved_at REAL, created_at REAL NOT NULL, idempotency_key TEXT);
        INSERT INTO handoffs (id, conversation_id, merchant_id, reason, summary, record, status, claimed_by, claimed_at, created_at)
            VALUES ('HND-OLD', 'c1', 'u', 'user_requested', 's', '{}', 'claimed', 'ops1', 1.0, 1.0);
    """)
    c.commit()
    c.close()
    s = HandoffStore(path, clock=clock)
    try:
        assert s.get("HND-OLD")["status"] == "pending"  # a claim with no lease is expired, never stuck forever
    finally:
        s.close()
