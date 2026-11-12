"""The handoff queue in SQLite: state machine, exactly-once, idempotency, migration. No network."""
import json
import sqlite3
import threading

import pytest

from app.handoff_store import HandoffConflict, HandoffNotFound, HandoffStore, case_summary


def record(hid="HND-AAAAAA", user="cliente1988", conv="c1", **kw):
    return {"handoff_id": hid, "created_at": "2026-01-01T10:00:00+00:00", "user_id": user, "conversation_id": conv,
            "reason": "user_requested", "priority": "normal", "ticket_id": None, "language": "English",
            "user_message": "I want a person", "conversation": [], "agent_findings": {}, "tool_calls": [], **kw}


@pytest.fixture
def store(tmp_path):
    s = HandoffStore(tmp_path / "handoffs.sqlite")
    yield s
    s.close()


def test_a_new_handoff_is_pending_and_keeps_the_whole_record(store):
    store.notify(record(agent_findings={"support": "balance is 10"}))
    h = store.get("HND-AAAAAA")
    assert (h["status"], h["merchant_id"], h["conversation_id"], h["claimed_by"]) == ("pending", "cliente1988", "c1", None)
    assert h["record"]["agent_findings"] == {"support": "balance is 10"}
    assert "Customer: I want a person" in h["summary"] and "support: balance is 10" in h["summary"]


def test_queue_is_oldest_first_and_filters_by_status(store):
    # created_at of a live handoff is "now"; the age order is the order of insertion
    for hid in ("HND-000001", "HND-000002", "HND-000003"):
        store.notify(record(hid))
    store.claim("HND-000001", "ops1")
    assert [h["id"] for h in store.list_queue()] == ["HND-000001", "HND-000002", "HND-000003"]
    assert [h["id"] for h in store.list_queue("pending")] == ["HND-000002", "HND-000003"]
    assert [h["id"] for h in store.list_queue("claimed")] == ["HND-000001"]
    assert store.list_queue("resolved") == []
    assert len(store.list_queue(limit=2)) == 2
    with pytest.raises(ValueError):
        store.list_queue("done")


def test_claim_then_reply_then_the_customer_gets_the_message_once(store):
    store.notify(record())
    assert store.claim("HND-AAAAAA", "ops1")["status"] == "claimed"
    h, replayed = store.reply("HND-AAAAAA", "ops1", "approve", "Done, refunded.", "key-0001")
    assert (h["status"], h["resolution"], h["operator_reply"], replayed) == ("resolved", "approve", "Done, refunded.", False)
    first = store.pop_undelivered("cliente1988", "c1")
    assert [(m["handoff_id"], m["decision"], m["message"]) for m in first] == [("HND-AAAAAA", "approve", "Done, refunded.")]
    assert store.pop_undelivered("cliente1988", "c1") == []  # delivered, not again
    assert [m["delivered"] for m in store.messages_for_conversation("cliente1988", "c1")] == [True]  # still readable


def test_claiming_again_as_the_same_operator_is_idempotent(store):
    store.notify(record())
    a = store.claim("HND-AAAAAA", "ops1", now=100.0)
    b = store.claim("HND-AAAAAA", "ops1", now=200.0)
    assert a["claimed_at"] == b["claimed_at"] == 100.0  # the first claim stands
    assert [e["action"] for e in store.events("HND-AAAAAA")] == ["created", "claim"]  # and is recorded once


def test_invalid_transitions_are_conflicts_and_unknown_ids_are_not_found(store):
    store.notify(record())
    with pytest.raises(HandoffConflict):  # not claimed yet
        store.reply("HND-AAAAAA", "ops1", "approve", "hi", "key-0001")
    store.claim("HND-AAAAAA", "ops1")
    with pytest.raises(HandoffConflict):  # claimed by someone else
        store.claim("HND-AAAAAA", "ops2")
    with pytest.raises(HandoffConflict):
        store.reply("HND-AAAAAA", "ops2", "approve", "hi", "key-0001")
    store.reply("HND-AAAAAA", "ops1", "reject", "No.", "key-0001")
    with pytest.raises(HandoffConflict):  # resolved: nobody claims it again, not even the same operator
        store.claim("HND-AAAAAA", "ops1")
    with pytest.raises(HandoffConflict):  # a different key on a resolved handoff
        store.reply("HND-AAAAAA", "ops1", "reject", "No.", "key-0002")
    for call in (lambda: store.claim("HND-NOPE", "ops1"), lambda: store.reply("HND-NOPE", "ops1", "approve", "x", "k")):
        with pytest.raises(HandoffNotFound):
            call()
    assert len(store.messages_for_conversation("cliente1988", "c1")) == 1  # none of the above stored anything


def test_the_same_idempotency_key_replays_the_result_without_a_second_message(store):
    store.notify(record())
    store.claim("HND-AAAAAA", "ops1")
    first, r1 = store.reply("HND-AAAAAA", "ops1", "approve", "Done.", "key-0001", now=100.0)
    again, r2 = store.reply("HND-AAAAAA", "ops1", "approve", "Done.", "key-0001", now=999.0)
    assert (r1, r2) == (False, True)
    assert again["resolved_at"] == first["resolved_at"] == 100.0
    assert len(store.messages_for_conversation("cliente1988", "c1")) == 1
    assert [e["action"] for e in store.events("HND-AAAAAA")].count("reply") == 1


def test_the_same_key_with_other_content_or_from_another_operator_is_a_conflict(store):
    store.notify(record())
    store.claim("HND-AAAAAA", "ops1")
    store.reply("HND-AAAAAA", "ops1", "approve", "Done.", "key-0001")
    with pytest.raises(HandoffConflict, match="different"):
        store.reply("HND-AAAAAA", "ops1", "reject", "Done.", "key-0001")
    with pytest.raises(HandoffConflict, match="different"):
        store.reply("HND-AAAAAA", "ops1", "approve", "Changed.", "key-0001")
    with pytest.raises(HandoffConflict):
        store.reply("HND-AAAAAA", "ops2", "approve", "Done.", "key-0001")  # the key does not open the door for B


def test_decision_is_validated_in_the_store_too(store):
    store.notify(record())
    store.claim("HND-AAAAAA", "ops1")
    with pytest.raises(ValueError):
        store.reply("HND-AAAAAA", "ops1", "maybe", "x", "key-0001")
    assert store.get("HND-AAAAAA")["status"] == "claimed"


def test_every_operator_action_is_in_the_audit_trail_with_operator_and_time(store):
    store.notify(record())
    store.claim("HND-AAAAAA", "ops1", now=111.0)
    store.reply("HND-AAAAAA", "ops1", "approve", "ok", "key-0001", now=222.0)
    ev = store.events("HND-AAAAAA")
    assert [(e["actor"], e["action"]) for e in ev] == [("system", "created"), ("ops1", "claim"), ("ops1", "reply")]
    assert [e["at"] for e in ev[1:]] == [111.0, 222.0] and ev[2]["detail"] == "approve"


# ---- concurrency: one UPDATE ... WHERE status decides, whoever is faster ----

def _race(callers):
    """Run the callables at the same moment. Returns (results, errors)."""
    barrier, out, errs = threading.Barrier(len(callers)), [], []

    def run(fn):
        barrier.wait()
        try:
            out.append(fn())
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    threads = [threading.Thread(target=run, args=(fn,)) for fn in callers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    return out, errs


def test_two_simultaneous_claims_through_separate_connections_one_wins(tmp_path):
    """Two stores on the same file = two connections with two different locks, like two processes.
    The only thing between them is SQLite and the UPDATE ... WHERE status = 'pending'."""
    path = tmp_path / "h.sqlite"
    a, b = HandoffStore(path), HandoffStore(path)
    a.notify(record())
    try:
        out, errs = _race([lambda: a.claim("HND-AAAAAA", "ops1"), lambda: b.claim("HND-AAAAAA", "ops2")])
        assert len(out) == 1 and len(errs) == 1 and isinstance(errs[0], HandoffConflict)
        assert a.get("HND-AAAAAA")["claimed_by"] == out[0]["claimed_by"]
        assert [e["action"] for e in a.events("HND-AAAAAA")].count("claim") == 1
    finally:
        a.close()
        b.close()


def test_many_operators_claiming_at_once_exactly_one_wins(tmp_path):
    path = tmp_path / "h.sqlite"
    stores = [HandoffStore(path) for _ in range(8)]
    stores[0].notify(record())
    try:
        out, errs = _race([(lambda s=s, i=i: s.claim("HND-AAAAAA", f"ops{i}")) for i, s in enumerate(stores)])
        assert len(out) == 1 and len(errs) == 7 and all(isinstance(e, HandoffConflict) for e in errs)
    finally:
        for s in stores:
            s.close()


def test_simultaneous_replies_with_the_same_key_store_one_message(tmp_path):
    path = tmp_path / "h.sqlite"
    a, b = HandoffStore(path), HandoffStore(path)
    a.notify(record())
    a.claim("HND-AAAAAA", "ops1")
    try:
        out, errs = _race([lambda: a.reply("HND-AAAAAA", "ops1", "approve", "Done.", "key-0001"),
                           lambda: b.reply("HND-AAAAAA", "ops1", "approve", "Done.", "key-0001")])
        assert errs == [] and sorted(replayed for _, replayed in out) == [False, True]
        assert len(a.messages_for_conversation("cliente1988", "c1")) == 1
    finally:
        a.close()
        b.close()


def test_simultaneous_turns_do_not_both_receive_the_same_message(tmp_path):
    path = tmp_path / "h.sqlite"
    a, b = HandoffStore(path), HandoffStore(path)
    a.notify(record())
    a.claim("HND-AAAAAA", "ops1")
    a.reply("HND-AAAAAA", "ops1", "approve", "Done.", "key-0001")
    try:
        out, errs = _race([lambda: a.pop_undelivered("cliente1988", "c1"),
                           lambda: b.pop_undelivered("cliente1988", "c1")])
        assert errs == [] and sorted(len(m) for m in out) == [0, 1]
    finally:
        a.close()
        b.close()


def test_the_database_itself_refuses_a_second_message_for_a_handoff(store):
    store.notify(record())
    store.claim("HND-AAAAAA", "ops1")
    store.reply("HND-AAAAAA", "ops1", "approve", "Done.", "key-0001")
    with pytest.raises(sqlite3.IntegrityError):  # even code that skipped the state machine could not duplicate it
        with store._tx() as c:
            c.execute("INSERT INTO operator_messages (handoff_id, merchant_id, conversation_id, decision, body, "
                      "created_at) VALUES ('HND-AAAAAA', 'cliente1988', 'c1', 'approve', 'dup', 1)")


# ---- the merchant side ----

def test_the_merchant_side_only_sees_its_own_conversations(store):
    store.notify(record("HND-AAAAAA", user="cliente1988", conv="c1"))
    store.notify(record("HND-BBBBBB", user="cliente2024", conv="c1"))  # same conversation_id, other merchant
    store.claim("HND-AAAAAA", "ops1")
    store.reply("HND-AAAAAA", "ops1", "approve", "For A only.", "key-0001")
    assert store.status_for("cliente1988", "HND-AAAAAA") == "resolved"
    assert store.status_for("cliente2024", "HND-AAAAAA") is None  # somebody else's id looks like a missing one
    assert store.messages_for_conversation("cliente2024", "c1") == []
    assert store.pop_undelivered("cliente2024", "c1") == []
    assert [h["id"] for h in store.open_for_conversation("cliente2024", "c1")] == ["HND-BBBBBB"]
    assert store.open_for_conversation("cliente1988", "c1") == []  # resolved is no longer open
    assert [m["message"] for m in store.pop_undelivered("cliente1988", "c1")] == ["For A only."]


# ---- migration from the JSONL ----

def write_jsonl(path, *records, extra=()):
    path.write_text("\n".join([*(json.dumps(r) for r in records), *extra]) + "\n", encoding="utf-8")


def test_import_loads_every_record_once_and_a_second_run_adds_nothing(store, tmp_path):
    src = tmp_path / "handoffs.jsonl"
    old = record("HND-OLD001", created_at="2025-12-01T08:00:00+00:00", agent_findings={"knowledge": "some finding"})
    del old["conversation_id"]  # the old format had no conversation_id
    write_jsonl(src, record("HND-OLD002", user="cliente2024"), old)  # file order is not age order
    assert store.import_jsonl(src) == {"imported": 2, "already_there": 0, "invalid": 0, "redacted": 0}
    assert store.import_jsonl(src) == {"imported": 0, "already_there": 2, "invalid": 0, "redacted": 0}
    assert [h["id"] for h in store.list_queue()] == ["HND-OLD001", "HND-OLD002"]  # by the age in the record
    h = store.get("HND-OLD001")
    assert h["status"] == "pending" and h["conversation_id"] == "default"  # the default conversation
    assert h["record"] == old  # nothing lost
    assert [e["action"] for e in store.events("HND-OLD001")] == ["imported"]


def test_import_never_resets_a_handoff_somebody_already_worked_on(store, tmp_path):
    src = tmp_path / "handoffs.jsonl"
    write_jsonl(src, record("HND-OLD001"))
    store.import_jsonl(src)
    store.claim("HND-OLD001", "ops1")
    store.reply("HND-OLD001", "ops1", "approve", "ok", "key-0001")
    assert store.import_jsonl(src)["imported"] == 0
    assert store.get("HND-OLD001")["status"] == "resolved"


def test_import_skips_bad_lines_and_a_missing_file_is_not_an_error(store, tmp_path):
    src = tmp_path / "handoffs.jsonl"
    write_jsonl(src, record("HND-OLD001"), extra=["{not json", '{"handoff_id": "HND-X"}', "[1, 2]", "", '"text"'])
    assert store.import_jsonl(src) == {"imported": 1, "already_there": 0, "invalid": 4, "redacted": 0}
    assert store.import_jsonl(tmp_path / "nope.jsonl") == {"imported": 0, "already_there": 0, "invalid": 0, "redacted": 0}


def test_import_keeps_a_record_whose_created_at_is_not_a_date(store, tmp_path):
    src = tmp_path / "handoffs.jsonl"
    write_jsonl(src, record("HND-OLD001", created_at="yesterday"))
    assert store.import_jsonl(src)["imported"] == 1  # kept, dated now: better than losing it


def test_case_summary_is_short_and_deterministic():
    r = record(user_message="x" * 1000, agent_findings={"support": "y" * 1000}, earlier_summary="z" * 1000)
    s = case_summary(r)
    assert len(s) < 900 and s == case_summary(r)


def test_data_survives_reopening_the_file(tmp_path):
    a = HandoffStore(tmp_path / "h.sqlite")
    a.notify(record())
    a.claim("HND-AAAAAA", "ops1")
    a.close()
    b = HandoffStore(tmp_path / "h.sqlite")
    try:
        assert b.get("HND-AAAAAA")["claimed_by"] == "ops1"
    finally:
        b.close()
