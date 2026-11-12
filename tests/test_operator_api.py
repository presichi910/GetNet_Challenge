"""Human oversight through the API: roles, queue, claim, reply, delivery to the customer.

Real SQLite (queue and conversations), real handoff node, stub router/agents, fake LLM: no network, no keys."""
import json
import threading

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from app.config import get_settings
from app.graph import build_graph
from app.handoff import FanOutNotifier, FileQueueNotifier, make_handoff_node
from app.handoff_store import HandoffStore
from app.main import app
from app.memory import Memory
from app.service import ChatService
from tests.conftest import bearer, make_jwt, operator_jwt

A, B = "cliente1988", "cliente2024"
OP1, OP2 = "ops1", "ops2"


def build_service(repo, mem, store, audit):
    """Router: a message that says 'human' asks for a person. The synthesizer answers by listing every user
    message the conversation holds, so the reply shows exactly what the graph (and any LLM) would see."""

    def router(state):
        wants = "human" in str(state["messages"][-1].content).lower()
        return {"plan": [], "step": 0, "language": "English", "routing_reasoning": "stub", "agent_outputs": {},
                "final_answer": "", "wants_human": wants, "handoff": None}

    def synthesizer(state):
        answer = "seen=" + "|".join(m.content for m in state["messages"] if isinstance(m, HumanMessage))
        return {"final_answer": answer, "messages": [AIMessage(content=answer)]}

    node = make_handoff_node(repo=repo, notifier=FanOutNotifier(store, FileQueueNotifier(audit)))
    graph = build_graph(router, lambda s: {}, lambda s: {}, synthesizer, node, checkpointer=mem.saver)
    return ChatService(graph, memory=mem, handoffs=store)


@pytest.fixture
def parts(tmp_path, repo):
    mem = Memory(tmp_path / "checkpoints.sqlite", ttl_days=30, max_threads_per_user=5)
    store = HandoffStore(tmp_path / "handoffs.sqlite")
    audit = tmp_path / "handoffs.jsonl"
    yield mem, store, audit, repo
    store.close()
    mem.close()


@pytest.fixture
def store(parts):
    return parts[1]


@pytest.fixture
def audit(parts):
    return parts[2]


@pytest.fixture
def client(auth_env, parts):
    mem, store, audit, repo = parts
    app.state.service = build_service(repo, mem, store, audit)
    with TestClient(app) as c:
        yield c
    app.state.service = None


def m(user, **kw):
    return bearer(make_jwt(user, **kw))


def op(name=OP1, **kw):
    return bearer(operator_jwt(name, **kw))


def chat(client, user, message="hello", conv="c1"):
    r = client.post("/chat", json={"message": message, "conversation_id": conv}, headers=m(user))
    assert r.status_code == 200, r.text
    return r.json()


def open_case(client, user=A, conv="c1", message="I need a human please"):
    body = chat(client, user, message, conv)
    assert body["handoff"] is not None
    return body["handoff"]["id"]


def claim(client, hid, who=OP1):
    return client.post(f"/operator/handoffs/{hid}/claim", headers=op(who))


def reply(client, hid, who=OP1, decision="approve", message="We refunded you.", key="key-00000001"):
    return client.post(f"/operator/handoffs/{hid}/reply", headers=op(who),
                       json={"decision": decision, "message": message, "idempotency_key": key})


# ---------------- roles ----------------

OPERATOR_CALLS = [("GET", "/operator/handoffs"), ("GET", "/operator/handoffs/HND-AAAAAA"),
                  ("POST", "/operator/handoffs/HND-AAAAAA/claim"), ("POST", "/operator/handoffs/HND-AAAAAA/reply")]


@pytest.mark.parametrize("scope", ["chat", "operator", "chat operator", None])
@pytest.mark.parametrize("method, path", OPERATOR_CALLS)
def test_a_merchant_is_403_on_every_operator_endpoint_whatever_its_scopes(client, method, path, scope):
    body = {"decision": "approve", "message": "x", "idempotency_key": "key-00000001"}
    r = client.request(method, path, headers=m(A, scope=scope), json=body if method == "POST" else None)
    assert r.status_code == 403


@pytest.mark.parametrize("method, path", OPERATOR_CALLS)
def test_operator_endpoints_need_a_token(client, method, path):
    assert client.request(method, path).status_code == 401


def test_an_operator_token_without_the_operator_scope_is_403(client):
    assert client.get("/operator/handoffs", headers=op(scope="chat")).status_code == 403
    assert client.get("/operator/handoffs", headers=op(scope=None)).status_code == 403


def test_an_operator_is_403_on_chat_and_nothing_runs(client, store):
    r = client.post("/chat", json={"message": "I need a human"}, headers=op(scope="chat operator"))
    assert r.status_code == 403
    assert store.list_queue() == []
    assert client.get("/conversations/c1/operator-messages", headers=op()).status_code == 403


def test_a_merchant_token_is_not_made_an_operator_by_the_sub(client):
    r = client.get("/operator/handoffs", headers=m("ops1", scope="operator"))  # a merchant that is called ops1
    assert r.status_code == 403


# ---------------- the full flow ----------------

def test_a_handoff_from_the_agents_lands_in_the_queue_with_its_conversation(client, store, audit):
    body = chat(client, A, "I need a human please", "c1")
    h = body["handoff"]
    assert h["status"] == "pending" and h["id"].startswith("HND-")
    row = store.get(h["id"])
    assert (row["merchant_id"], row["conversation_id"], row["status"], row["reason"]) == (A, "c1", "pending", "user_requested")
    assert row["record"]["user_message"] == "I need a human please" and "Customer: I need a human" in row["summary"]
    audit_record = json.loads(audit.read_text(encoding="utf-8").strip())  # the JSONL audit copy is still written
    assert audit_record["handoff_id"] == h["id"] and audit_record["conversation_id"] == "c1"


def test_full_flow_create_list_claim_reply_and_the_customer_receives_it(client):
    hid = open_case(client)

    listed = client.get("/operator/handoffs?status=pending", headers=op()).json()
    assert listed["count"] == 1 and listed["handoffs"][0]["id"] == hid
    assert (listed["handoffs"][0]["merchant_id"], listed["handoffs"][0]["conversation_id"]) == (A, "c1")

    r = claim(client, hid)
    assert r.status_code == 200 and r.json()["status"] == "claimed" and r.json()["claimed_by"] == OP1
    assert r.json()["record"]["user_message"] == "I need a human please"  # the operator gets the whole case

    # while it is claimed the customer sees the state, and nothing about who has it
    mid = chat(client, A, "any news?", "c1")
    assert mid["open_handoffs"] == [{"id": hid, "status": "claimed"}] and mid["operator_messages"] == []
    assert OP1 not in json.dumps(mid)

    r = reply(client, hid, message="Approved: we refunded the fee.")
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["resolution"], r.json()["replayed"]) == ("resolved", "approve", False)

    nxt = chat(client, A, "thanks", "c1")  # the next turn of that conversation
    assert [(x["handoff_id"], x["decision"], x["message"], x["sender"]) for x in nxt["operator_messages"]] == [
        (hid, "approve", "Approved: we refunded the fee.", "human_operator")]
    assert nxt["open_handoffs"] == [] and OP1 not in json.dumps(nxt)
    assert chat(client, A, "and again", "c1")["operator_messages"] == []  # delivered once

    got = client.get("/conversations/c1/operator-messages", headers=m(A)).json()  # still readable afterwards
    assert got["conversation_id"] == "c1"
    assert [(x["message"], x["delivered"]) for x in got["messages"]] == [("Approved: we refunded the fee.", True)]


def test_the_operator_message_never_enters_what_the_graph_sees(client):
    """Not injected into the conversation history: the LLM sees exactly what it saw before this step."""
    hid = open_case(client)
    claim(client, hid)
    reply(client, hid, message="SECRET-OPERATOR-TEXT")
    chat(client, A, "thanks", "c1")
    turn = chat(client, A, "one more", "c1")
    assert "SECRET-OPERATOR-TEXT" not in turn["response"]  # the stub echoes every user message in the history
    detail = client.get("/conversations/c1", headers=m(A)).json()
    assert "SECRET-OPERATOR-TEXT" not in json.dumps(detail)


def test_the_queue_is_oldest_first_and_the_status_filter_works(client):
    ids = [open_case(client, A, f"c{i}", "human please") for i in range(3)]
    claim(client, ids[1])
    allrows = client.get("/operator/handoffs", headers=op()).json()["handoffs"]
    assert [h["id"] for h in allrows] == ids
    assert [h["id"] for h in client.get("/operator/handoffs?status=pending", headers=op()).json()["handoffs"]] == [ids[0], ids[2]]
    assert [h["id"] for h in client.get("/operator/handoffs?status=claimed", headers=op()).json()["handoffs"]] == [ids[1]]
    assert client.get("/operator/handoffs?status=bogus", headers=op()).status_code == 422
    assert client.get("/operator/handoffs?limit=0", headers=op()).status_code == 422
    assert len(client.get("/operator/handoffs?limit=2", headers=op()).json()["handoffs"]) == 2


def test_an_operator_sees_every_merchants_handoffs(client):
    a, b = open_case(client, A), open_case(client, B)
    seen = {h["id"]: h["merchant_id"] for h in client.get("/operator/handoffs", headers=op()).json()["handoffs"]}
    assert seen == {a: A, b: B}


# ---------------- transitions ----------------

def test_claim_by_the_same_operator_again_is_idempotent(client):
    hid = open_case(client)
    first, second = claim(client, hid), claim(client, hid)
    assert first.status_code == second.status_code == 200
    assert first.json()["claimed_at"] == second.json()["claimed_at"]


def test_claim_of_a_handoff_held_by_another_operator_is_409(client):
    hid = open_case(client)
    assert claim(client, hid, OP1).status_code == 200
    r = claim(client, hid, OP2)
    assert r.status_code == 409
    assert client.get(f"/operator/handoffs/{hid}", headers=op(OP1)).json()["claimed_by"] == OP1


def test_two_simultaneous_claims_through_the_api_one_wins_one_gets_409(client):
    hid = open_case(client)
    others = [TestClient(app), TestClient(app)]  # no lifespan: they share the service of `client`
    barrier, codes = threading.Barrier(2), []

    def go(c, who):
        barrier.wait()
        codes.append(c.post(f"/operator/handoffs/{hid}/claim", headers=op(who)).status_code)

    threads = [threading.Thread(target=go, args=(others[0], OP1)), threading.Thread(target=go, args=(others[1], OP2))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert sorted(codes) == [200, 409]
    winner = client.get(f"/operator/handoffs/{hid}", headers=op()).json()["claimed_by"]
    assert winner in (OP1, OP2)
    assert [e["actor"] for e in app.state.service.handoffs.events(hid) if e["action"] == "claim"] == [winner]


def test_reply_with_the_same_idempotency_key_delivers_one_message(client):
    hid = open_case(client)
    claim(client, hid)
    first, again = reply(client, hid), reply(client, hid)
    assert first.status_code == again.status_code == 200
    assert (first.json()["replayed"], again.json()["replayed"]) == (False, True)
    assert first.json()["resolved_at"] == again.json()["resolved_at"]
    got = client.get("/conversations/c1/operator-messages", headers=m(A)).json()["messages"]
    assert len(got) == 1
    assert len(chat(client, A, "next", "c1")["operator_messages"]) == 1


def test_reply_on_a_handoff_nobody_claimed_is_409(client):
    hid = open_case(client)
    assert reply(client, hid).status_code == 409
    assert client.get(f"/operator/handoffs/{hid}", headers=op()).json()["status"] == "pending"
    assert client.get("/conversations/c1/operator-messages", headers=m(A)).json()["messages"] == []


def test_reply_on_a_handoff_claimed_by_another_operator_is_409(client):
    hid = open_case(client)
    claim(client, hid, OP1)
    assert reply(client, hid, OP2).status_code == 409
    assert client.get("/conversations/c1/operator-messages", headers=m(A)).json()["messages"] == []
    assert reply(client, hid, OP1).status_code == 200  # the owner still can


def test_a_different_key_on_a_resolved_handoff_is_409_and_sends_nothing(client):
    hid = open_case(client)
    claim(client, hid)
    assert reply(client, hid, key="key-00000001").status_code == 200
    assert reply(client, hid, key="key-00000002", message="Changed my mind").status_code == 409
    assert reply(client, hid, key="key-00000001", message="Different text").status_code == 409  # same key, other content
    assert [x["message"] for x in client.get("/conversations/c1/operator-messages", headers=m(A)).json()["messages"]] == [
        "We refunded you."]


def test_claiming_a_resolved_handoff_is_409(client):
    hid = open_case(client)
    claim(client, hid)
    reply(client, hid)
    assert claim(client, hid).status_code == 409


def test_unknown_and_malformed_ids_are_404(client):
    for hid in ("HND-ZZZZZZ", "nope", "HND-", "HND-%20x", "x" * 80):
        assert client.get(f"/operator/handoffs/{hid}", headers=op()).status_code == 404
        assert claim(client, hid).status_code == 404
        assert reply(client, hid).status_code == 404


# ---------------- input validation ----------------

@pytest.mark.parametrize("payload", [
    {"decision": "maybe", "message": "x", "idempotency_key": "key-00000001"},
    {"decision": "approve", "message": "", "idempotency_key": "key-00000001"},
    {"decision": "approve", "message": "   \n ", "idempotency_key": "key-00000001"},
    {"decision": "approve", "message": "x" * 1001, "idempotency_key": "key-00000001"},
    {"decision": "approve", "message": "bad \x00 byte", "idempotency_key": "key-00000001"},
    {"decision": "approve", "message": "x"},
    {"decision": "approve", "message": "x", "idempotency_key": "short"},
    {"decision": "approve", "message": "x", "idempotency_key": "has spaces in it"},
    {"decision": "approve", "message": "x", "idempotency_key": "k" * 65},
    {"message": "x", "idempotency_key": "key-00000001"},
    {"decision": "approve", "message": ["x"], "idempotency_key": "key-00000001"},
])
def test_invalid_reply_input_is_422_and_changes_nothing(client, payload):
    hid = open_case(client)
    claim(client, hid)
    r = client.post(f"/operator/handoffs/{hid}/reply", headers=op(), json=payload)
    assert r.status_code == 422
    assert client.get(f"/operator/handoffs/{hid}", headers=op()).json()["status"] == "claimed"


def test_the_longest_allowed_message_and_a_multiline_one_work_and_are_trimmed(client):
    hid = open_case(client)
    claim(client, hid)
    assert reply(client, hid, message="  " + "x" * 996 + "  ").json()["operator_reply"] == "x" * 996  # 1000 raw, trimmed
    hid2 = open_case(client, A, "c2")
    claim(client, hid2)
    assert reply(client, hid2, message="line one\nline two", key="key-00000002").json()["operator_reply"] == "line one\nline two"


# ---------------- who sees what ----------------

def test_merchant_b_does_not_see_or_receive_the_message_for_merchant_a(client):
    hid = open_case(client, A, "c1")
    chat(client, B, "hello from B", "c1")  # B has a conversation with the same id
    claim(client, hid)
    reply(client, hid, message="Only for A")

    b_turn = chat(client, B, "anything for me?", "c1")
    assert b_turn["operator_messages"] == [] and b_turn["open_handoffs"] == [] and b_turn["handoff"] is None
    assert "Only for A" not in json.dumps(b_turn)
    assert client.get("/conversations/c1/operator-messages", headers=m(B)).json()["messages"] == []

    a_turn = chat(client, A, "and for me?", "c1")  # B's turns did not consume A's message
    assert [x["message"] for x in a_turn["operator_messages"]] == ["Only for A"]


def test_the_messages_endpoint_is_404_for_a_conversation_that_is_not_yours_or_does_not_exist(client):
    open_case(client, A, "only-a")
    claim(client, client.get("/operator/handoffs", headers=op()).json()["handoffs"][0]["id"])
    for conv in ("only-a", "ghost", "bad conv!", "x" * 80):
        r = client.get(f"/conversations/{conv}/operator-messages", headers=m(B))
        assert (r.status_code, r.json()) == (404, {"detail": "Conversation not found."})
    assert client.get("/conversations/only-a/operator-messages", headers=m(A)).status_code == 200
    assert client.get("/conversations/only-a/operator-messages").status_code == 401


def test_a_merchant_response_never_carries_operator_identity_or_other_merchants_data(client):
    a = open_case(client, A, "c1")
    b = open_case(client, B, "c1")
    claim(client, a, OP1)
    claim(client, b, OP2)
    reply(client, a, OP1, message="reply A", key="key-0000000a")
    turn_a = chat(client, A, "status?", "c1")
    text = json.dumps(turn_a)
    assert b not in text and B not in text and "reply A" in text
    assert OP1 not in text and OP2 not in text and "claimed_by" not in text
    turn_b = chat(client, B, "status?", "c1")
    assert turn_b["open_handoffs"] == [{"id": b, "status": "claimed"}] and a not in json.dumps(turn_b)


def test_this_turns_handoff_reports_its_current_status(client):
    hid = open_case(client, A, "c1")
    claim(client, hid)
    second = chat(client, A, "human please, again", "c1")  # a second handoff in the same conversation
    assert second["handoff"]["status"] == "pending" and second["handoff"]["id"] != hid
    assert second["open_handoffs"] == [{"id": hid, "status": "claimed"}]  # the earlier one, not repeated as "this turn's"


# ---------------- audit ----------------

def test_every_operator_action_is_recorded_with_operator_id_and_time(client, store):
    hid = open_case(client)
    claim(client, hid, OP1)
    claim(client, hid, OP2)  # refused: must not be recorded as an action
    reply(client, hid, OP1)
    reply(client, hid, OP1)  # a replay: not a second action
    ev = store.events(hid)
    assert [(e["actor"], e["action"]) for e in ev] == [("system", "created"), (OP1, "claim"), (OP1, "reply")]
    assert all(isinstance(e["at"], float) and e["at"] > 0 for e in ev)


# ---------------- resilience ----------------

def test_a_broken_queue_never_fails_the_customers_turn(client):
    class Broken:
        def status_for(self, *a):
            raise RuntimeError("db gone")

        open_for_conversation = pop_undelivered = status_for

    app.state.service.handoffs = Broken()
    r = client.post("/chat", json={"message": "hello", "conversation_id": "c1"}, headers=m(A))
    assert r.status_code == 200 and r.json()["operator_messages"] == [] and r.json()["open_handoffs"] == []


def test_without_a_queue_the_operator_endpoints_are_503_and_chat_still_works(auth_env, parts):
    mem, _, audit, repo = parts
    svc = build_service(repo, mem, HandoffStore(audit.parent / "x.sqlite"), audit)
    svc.handoffs = None
    app.state.service = svc
    try:
        with TestClient(app) as c:
            assert c.get("/operator/handoffs", headers=op()).status_code == 503
            assert c.post("/chat", json={"message": "hello"}, headers=m(A)).status_code == 200
    finally:
        app.state.service = None


# ---------------- the one-time migration, at startup ----------------

def test_startup_imports_the_old_queue_once_and_a_restart_does_not_duplicate(auth_env, monkeypatch, tmp_path):
    old = tmp_path / "handoffs.jsonl"
    rows = [{"handoff_id": f"HND-OLD00{i}", "created_at": f"2026-01-0{i}T10:00:00+00:00", "user_id": A, "reason": "user_requested",
             "priority": "normal", "user_message": f"old case {i}", "agent_findings": {}} for i in (1, 2, 3)]
    old.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(tmp_path / "checkpoints.sqlite"))
    monkeypatch.setenv("HANDOFFS_DB_PATH", str(tmp_path / "handoffs.sqlite"))
    monkeypatch.setenv("HANDOFFS_FILE", str(old))
    monkeypatch.setattr("app.graph.build_default_graph", lambda checkpointer=None, **_: build_graph(
        lambda s: {}, lambda s: {}, lambda s: {}, lambda s: {}, lambda s: {}, checkpointer=checkpointer))
    monkeypatch.setattr("app.rag.store.collection_size", lambda: 1)
    get_settings.cache_clear()

    for _ in range(2):  # first start, then a restart
        app.state.service = None
        with TestClient(app) as c:
            r = c.get("/operator/handoffs?status=pending", headers=op()).json()
            assert [h["id"] for h in r["handoffs"]] == ["HND-OLD001", "HND-OLD002", "HND-OLD003"]
            assert r["handoffs"][0]["summary"].startswith("Customer: old case 1")
    app.state.service = None


def test_a_handoff_worked_on_survives_a_restart_and_is_not_reimported_as_pending(auth_env, monkeypatch, tmp_path):
    old = tmp_path / "handoffs.jsonl"
    old.write_text(json.dumps({"handoff_id": "HND-OLD001", "created_at": "2026-01-01T10:00:00+00:00", "user_id": A,
                               "reason": "user_requested", "user_message": "old"}) + "\n", encoding="utf-8")
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(tmp_path / "checkpoints.sqlite"))
    monkeypatch.setenv("HANDOFFS_DB_PATH", str(tmp_path / "handoffs.sqlite"))
    monkeypatch.setenv("HANDOFFS_FILE", str(old))
    monkeypatch.setattr("app.graph.build_default_graph", lambda checkpointer=None, **_: build_graph(
        lambda s: {}, lambda s: {}, lambda s: {}, lambda s: {}, lambda s: {}, checkpointer=checkpointer))
    monkeypatch.setattr("app.rag.store.collection_size", lambda: 1)
    get_settings.cache_clear()
    app.state.service = None
    with TestClient(app) as c:
        assert claim(c, "HND-OLD001").status_code == 200
        assert reply(c, "HND-OLD001").status_code == 200
    app.state.service = None
    with TestClient(app) as c:
        assert c.get("/operator/handoffs?status=pending", headers=op()).json()["count"] == 0
        assert c.get("/operator/handoffs/HND-OLD001", headers=op()).json()["status"] == "resolved"
    app.state.service = None
