"""POST /operator/handoffs/{id}/reject: an operator gives a case up (back to the queue, or closed)."""
import json
import threading

import pytest

from app.handoff_store import HandoffConflict, HandoffNotFound, HandoffStore
from tests.test_handoff_lease import LEASE, Clock
from tests.test_handoff_store import record
from tests.test_operator_api import (A, OP1, OP2, audit, chat, claim, client, m, op, open_case, parts,  # noqa: F401
                                     reply, store as api_store)

# ---------------- the store ----------------


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = HandoffStore(tmp_path / "h.sqlite", lease_seconds=LEASE, clock=clock)
    s.notify(record())
    s.claim("HND-AAAAAA", "ops1")
    yield s
    s.close()


def test_releasing_puts_the_case_back_in_the_queue_for_anyone(store):
    h = store.release("HND-AAAAAA", "ops1", "this is a billing case, not mine")
    assert (h["status"], h["claimed_by"], h["claimed_at"], h["lease_expires_at"]) == ("pending", None, None, None)
    assert [x["id"] for x in store.list_queue("pending")] == ["HND-AAAAAA"]
    assert store.claim("HND-AAAAAA", "ops2")["claimed_by"] == "ops2"


def test_release_and_close_are_audited_with_operator_reason_and_time(store, clock):
    clock.advance(30)
    store.release("HND-AAAAAA", "ops1", "not mine")
    store.claim("HND-AAAAAA", "ops2")
    store.release("HND-AAAAAA", "ops2", "duplicate of HND-OTHER", close=True)
    ev = store.events("HND-AAAAAA")
    assert [(e["actor"], e["action"], e["detail"]) for e in ev] == [
        ("system", "created", "user_requested"), ("ops1", "claim", None), ("ops1", "release", "not mine"),
        ("ops2", "claim", None), ("ops2", "close", "duplicate of HND-OTHER")]
    assert ev[2]["at"] == clock.t


def test_closing_resolves_it_as_reject_without_a_message_for_the_customer(store):
    h = store.release("HND-AAAAAA", "ops1", "spam", close=True)
    assert (h["status"], h["resolution"], h["operator_reply"], h["lease_expires_at"]) == ("resolved", "reject", None, None)
    assert store.messages_for_conversation("cliente1988", "c1") == []
    assert store.pop_undelivered("cliente1988", "c1") == []
    assert store.open_for_conversation("cliente1988", "c1") == []


def test_invalid_transitions_are_conflicts_and_unknown_ids_are_not_found(store, clock):
    store.notify(record("HND-BBBBBB"))
    with pytest.raises(HandoffConflict, match="do not hold"):  # never claimed
        store.release("HND-BBBBBB", "ops1", "x")
    with pytest.raises(HandoffConflict, match="another"):  # held by someone else
        store.release("HND-AAAAAA", "ops2", "x")
    with pytest.raises(HandoffConflict, match="another"):
        store.release("HND-AAAAAA", "ops2", "x", close=True)
    assert store.get("HND-AAAAAA")["claimed_by"] == "ops1"  # none of that changed anything
    store.release("HND-AAAAAA", "ops1", "x")  # now pending
    with pytest.raises(HandoffConflict, match="do not hold"):  # twice in a row
        store.release("HND-AAAAAA", "ops1", "x")
    store.claim("HND-AAAAAA", "ops1")
    clock.advance(LEASE + 1)  # the lease ran out: no valid claim
    with pytest.raises(HandoffConflict):
        store.release("HND-AAAAAA", "ops1", "late")
    store.claim("HND-AAAAAA", "ops1")
    store.reply("HND-AAAAAA", "ops1", "approve", "done", "key-00000001")
    for close in (False, True):
        with pytest.raises(HandoffConflict, match="Already resolved"):
            store.release("HND-AAAAAA", "ops1", "x", close=close)
    with pytest.raises(HandoffNotFound):
        store.release("HND-NOPE", "ops1", "x")


def test_a_closed_handoff_can_not_be_claimed_replied_or_released_again(store):
    store.release("HND-AAAAAA", "ops1", "spam", close=True)
    with pytest.raises(HandoffConflict):
        store.claim("HND-AAAAAA", "ops1")
    with pytest.raises(HandoffConflict):
        store.reply("HND-AAAAAA", "ops1", "approve", "x", "key-00000001")
    with pytest.raises(HandoffConflict):
        store.release("HND-AAAAAA", "ops1", "again")


def test_a_reply_and_a_release_at_the_same_moment_exactly_one_wins(tmp_path):
    path = tmp_path / "h.sqlite"
    a, b = HandoffStore(path), HandoffStore(path)
    a.notify(record())
    a.claim("HND-AAAAAA", "ops1")
    barrier, out, errs = threading.Barrier(2), [], []

    def go(fn):
        barrier.wait()
        try:
            out.append(fn())
        except HandoffConflict as e:
            errs.append(e)

    threads = [threading.Thread(target=go, args=(lambda: a.reply("HND-AAAAAA", "ops1", "approve", "x", "key-00000001"),)),
               threading.Thread(target=go, args=(lambda: b.release("HND-AAAAAA", "ops1", "give up"),))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    try:
        assert len(out) == 1 and len(errs) == 1
        h = a.get("HND-AAAAAA")
        # whichever won, the state is consistent: a message exists only if it ended resolved
        assert (h["status"] == "resolved") == bool(a.messages_for_conversation("cliente1988", "c1"))
    finally:
        a.close()
        b.close()


# ---------------- the API ----------------

def reject(client, hid, who=OP1, **body):
    return client.post(f"/operator/handoffs/{hid}/reject", headers=op(who), json={"reason": "not my area", **body})


def test_release_through_the_api_back_to_pending_and_another_operator_takes_it(client):
    hid = open_case(client)
    claim(client, hid, OP1)
    r = reject(client, hid)
    assert r.status_code == 200 and r.json()["status"] == "pending" and r.json()["claimed_by"] is None
    assert chat(client, A, "status?", "c1")["open_handoffs"] == [{"id": hid, "status": "pending"}]
    assert reply(client, hid, OP1).status_code == 409  # OP1 gave it up
    assert claim(client, hid, OP2).status_code == 200
    assert reply(client, hid, OP2).status_code == 200


def test_close_through_the_api_sends_nothing_to_the_customer(client):
    hid = open_case(client)
    claim(client, hid)
    r = reject(client, hid, close=True, reason="duplicate")
    assert (r.status_code, r.json()["status"], r.json()["resolution"], r.json()["operator_reply"]) == (200, "resolved", "reject", None)
    turn = chat(client, A, "status?", "c1")
    assert turn["operator_messages"] == [] and turn["open_handoffs"] == []
    assert "duplicate" not in json.dumps(turn)  # the reason is for operators
    assert client.get("/conversations/c1/operator-messages", headers=m(A)).json()["messages"] == []


def test_reject_conflicts_and_404s_through_the_api(client):
    hid = open_case(client)
    assert reject(client, hid).status_code == 409  # not claimed
    claim(client, hid, OP1)
    assert reject(client, hid, OP2).status_code == 409  # somebody else's
    assert reject(client, hid, OP1).status_code == 200
    assert reject(client, hid, OP1).status_code == 409  # already released
    for bad in ("HND-ZZZZZZ", "nope", "HND-"):
        assert reject(client, bad).status_code == 404


def test_only_operators_can_reject(client):
    hid = open_case(client)
    claim(client, hid)
    body = {"reason": "x"}
    assert client.post(f"/operator/handoffs/{hid}/reject", json=body).status_code == 401
    for scope in ("chat", "operator", "chat operator"):
        assert client.post(f"/operator/handoffs/{hid}/reject", json=body, headers=m(A, scope=scope)).status_code == 403
    assert client.get(f"/operator/handoffs/{hid}", headers=op()).json()["status"] == "claimed"


@pytest.mark.parametrize("payload", [
    {}, {"reason": ""}, {"reason": "   "}, {"reason": "x" * 501}, {"reason": "bad \x00 byte"},
    {"reason": "ok", "close": "yes"}, {"reason": "ok", "close": 1}, {"reason": ["x"]},
])
def test_invalid_reject_input_is_422_and_changes_nothing(client, payload):
    hid = open_case(client)
    claim(client, hid)
    assert client.post(f"/operator/handoffs/{hid}/reject", headers=op(), json=payload).status_code == 422
    assert client.get(f"/operator/handoffs/{hid}", headers=op()).json()["status"] == "claimed"


def test_the_reason_is_in_the_audit_trail_through_the_api(client, api_store):
    hid = open_case(client)
    claim(client, hid, OP1)
    reject(client, hid, OP1, reason="  wrong queue  ")
    assert [(e["actor"], e["action"], e["detail"]) for e in api_store.events(hid)][-1] == (OP1, "release", "wrong queue")
