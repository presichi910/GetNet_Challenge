"""Observability: tokens and cost per node, the trace store (no text, deleted with the conversation and by
TTL), /operator/metrics, alerts, and a broken store never failing a turn. No network."""
import hashlib
import logging
import time
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from app.agents.knowledge import make_knowledge_node
from app.agents.router import make_router_node
from app.agents.support import make_support_node
from app.agents.synthesizer import make_synthesizer_node
from app.data.repository import CustomerRepository
from app.graph import build_graph
from app.observability import (DEFAULT_PRICES, TraceStore, TurnCollector, cost_usd, price_table, sum_costs,
                               turn_alerts)
from app.service import ChatService
from tests.conftest import ToolFakeModel, bearer, make_jwt, operator_jwt

SECRET_QUESTION = "zebra-sapphire-question-7781 when is my payout"
SECRET_ANSWER = "violet-harbor-answer-4410 tomorrow"
BIG, SMALL = "m-big", "m-small"
PRICES = {BIG: (1.0, 10.0), SMALL: (0.5, 2.0)}


class NamedFake(ToolFakeModel):
    """A fake chat model that reports a model name to callbacks, like ChatGroq / ChatOpenAI do."""

    model_name: str = "m-fake"

    def _get_ls_params(self, stop=None, **kwargs):
        params = super()._get_ls_params(stop=stop, **kwargs)
        params["ls_model_name"] = self.model_name
        return params


def named(model: str, *messages: AIMessage) -> NamedFake:
    return NamedFake(model_name=model, messages=iter(messages))


def usage(i: int, o: int) -> dict:
    return {"input_tokens": i, "output_tokens": o, "total_tokens": i + o}


def routing(agents: list[str]) -> AIMessage:
    return AIMessage(content="", usage_metadata=usage(100, 10), tool_calls=[{
        "name": "RoutingDecision", "id": "r1",
        "args": {"reasoning": "r", "agents": agents, "language": "English", "wants_human": False}}])


def build(tmp_path, *, support_model=BIG, router_model=SMALL, synth_answer=SECRET_ANSWER):
    """Real router, support, knowledge and synthesizer nodes with fake models; the plan is support + knowledge,
    so the synthesizer merges (one LLM call there too)."""
    repo = CustomerRepository(tickets_file=tmp_path / "tickets.jsonl", today=date(2026, 1, 15))
    router = named(router_model, routing(["support", "knowledge"]))
    support = named(support_model,
                    AIMessage(content="", usage_metadata=usage(200, 20),
                              tool_calls=[{"name": "get_payout_schedule", "args": {}, "id": "t1"}]),
                    AIMessage(content="support says tomorrow", usage_metadata=usage(300, 30)))
    # Knowledge must use a tool: its first answer gets a nudge and the second one is taken (2 calls, 400/40 tokens)
    knowledge = named(BIG, AIMessage(content="knowledge says D+1", usage_metadata=usage(200, 20)),
                      AIMessage(content="knowledge says D+1", usage_metadata=usage(200, 20)))
    synth = named(router_model, AIMessage(content=synth_answer, usage_metadata=usage(50, 5)))
    return build_graph(make_router_node(router), make_knowledge_node(knowledge), make_support_node(llm=support, repo=repo),
                       make_synthesizer_node(synth), lambda s: {}, checkpointer=MemorySaver())


@pytest.fixture
def priced(monkeypatch):
    monkeypatch.setattr("app.service.price_table", lambda override="": dict(PRICES))


@pytest.fixture
def store(tmp_path):
    s = TraceStore(tmp_path / "traces.sqlite")
    yield s
    s.close()


# ---- cost ----

def test_cost_uses_the_input_price_for_input_tokens_and_the_output_price_for_output_tokens():
    # 1M input at $1 + 1M output at $10. Swapping the prices would give 10 + 1 for (1M, 0) and (0, 1M)
    assert cost_usd(BIG, 1_000_000, 0, PRICES) == pytest.approx(1.0)
    assert cost_usd(BIG, 0, 1_000_000, PRICES) == pytest.approx(10.0)
    assert cost_usd(SMALL, 2000, 500, PRICES) == pytest.approx((2000 * 0.5 + 500 * 2.0) / 1e6)


def test_a_model_without_a_price_costs_null_not_zero(caplog):
    with caplog.at_level(logging.WARNING, logger="observability"):
        assert cost_usd("m-nobody-priced-this", 1000, 1000, PRICES) is None
    assert "no price for model" in caplog.text
    assert sum_costs([0.1, None]) is None and sum_costs([0.1, 0.2]) == pytest.approx(0.3)


def test_the_price_table_has_the_fireworks_and_groq_models_and_accepts_an_override():
    assert {"accounts/fireworks/models/gpt-oss-120b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"} <= set(DEFAULT_PRICES)
    table = price_table('{"accounts/fireworks/models/gpt-oss-20b": [0.07, 0.3], "openai/gpt-oss-120b": [1, 2]}')
    assert table["accounts/fireworks/models/gpt-oss-20b"] == (0.07, 0.3)
    assert table["openai/gpt-oss-120b"] == (1.0, 2.0)
    assert price_table("not json") == DEFAULT_PRICES  # a bad override is ignored, the defaults stay


# ---- the collector inside the real graph ----

def test_tokens_and_cost_are_summed_per_node_and_model_through_the_real_graph(tmp_path):
    graph = build(tmp_path)
    collector = TurnCollector(dict(PRICES))
    ChatService(graph).run_raw("when is my payout", "cliente1988", thread_id="cliente1988:c1", callbacks=[collector])
    u = collector.usage()
    nodes = {(g["node"], g["model"]): g for g in u["by_node"]}
    assert set(nodes) == {("router", SMALL), ("support", BIG), ("knowledge", BIG), ("synthesizer", SMALL)}
    assert (nodes[("support", BIG)]["calls"], nodes[("support", BIG)]["input_tokens"],
            nodes[("support", BIG)]["output_tokens"]) == (2, 500, 50)  # the nested agent's two calls, both under support
    assert (nodes[("router", SMALL)]["input_tokens"], nodes[("router", SMALL)]["output_tokens"]) == (100, 10)
    assert (u["input_tokens"], u["output_tokens"], u["total_tokens"]) == (1050, 105, 1155)
    expected = (500 * 1 + 50 * 10 + 400 * 1 + 40 * 10) / 1e6 + (150 * 0.5 + 15 * 2) / 1e6
    assert u["cost_usd"] == pytest.approx(expected)
    assert [g["node"] for g in u["by_node"]] == ["router", "knowledge", "support", "synthesizer"]
    tools = [s for s in collector.spans if s["kind"] == "tool"]
    assert [(t["node"], t["name"], t["ok"]) for t in tools] == [("support", "get_payout_schedule", True)]


def test_one_unpriced_model_makes_the_turn_cost_null_but_keeps_the_priced_nodes(tmp_path):
    graph = build(tmp_path, router_model="m-unpriced")
    collector = TurnCollector(dict(PRICES))
    ChatService(graph).run_raw("hi", "cliente1988", thread_id="cliente1988:c1", callbacks=[collector])
    u = collector.usage()
    assert u["cost_usd"] is None
    by = {g["node"]: g["cost_usd"] for g in u["by_node"]}
    assert by["router"] is None and by["synthesizer"] is None and by["support"] > 0


def test_a_failed_llm_call_is_a_span_with_its_error_type_and_no_text():
    from uuid import uuid4

    c = TurnCollector(dict(PRICES))
    rid = uuid4()
    c.on_chat_model_start({}, [[]], run_id=rid, metadata={"langgraph_checkpoint_ns": "router:abc", "ls_model_name": BIG})

    class RateLimitError(Exception):
        status_code = 429

    c.on_llm_error(RateLimitError("secret prompt text in the error"), run_id=rid)
    (span,) = c.spans
    assert (span["node"], span["ok"], span["error_type"]) == ("router", False, "RateLimitError (429)")
    assert "secret" not in str(span)
    assert c.rate_limited()


# ---- the service: usage in the response, the stored trace, alerts, failures ----

def test_handle_returns_usage_and_stores_a_trace_without_any_text(tmp_path, store, priced):
    svc = ChatService(build(tmp_path), traces=store)
    resp = svc.handle(SECRET_QUESTION, "cliente1988", conversation_id="c1")
    assert resp.usage.total_tokens == 1155 and resp.usage.cost_usd > 0
    assert resp.response == SECRET_ANSWER
    assert store.count() == 1
    store.close()  # flush everything to disk, then read the raw bytes of the file and its WAL
    for path in tmp_path.glob("traces.sqlite*"):
        raw = path.read_bytes()
        for text in ("zebra-sapphire", "violet-harbor", "support says", "knowledge says", "payout_date", "2026-01"):
            assert text.encode() not in raw, f"{text!r} found in {path.name}"


def test_the_existing_log_line_keeps_its_fields_and_gets_tokens_and_cost(tmp_path, priced, caplog):
    svc = ChatService(build(tmp_path))
    with caplog.at_level(logging.INFO, logger="chat"):
        resp = svc.handle("hello", "cliente1988", conversation_id="c1")
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("trace="))
    assert line.startswith(f"trace={resp.trace_id} user=cliente1988 conv=c1 plan=['support', 'knowledge'] used=")
    assert " handoff=None latency_ms=" in line
    assert line.endswith(f"tokens=1155 cost_usd={resp.usage.cost_usd:.6f}")


def test_a_broken_trace_store_never_fails_the_turn(tmp_path, priced, caplog):
    class Broken:
        def record(self, *a, **k):
            raise RuntimeError("disk full")

    svc = ChatService(build(tmp_path), traces=Broken())
    with caplog.at_level(logging.WARNING, logger="chat"):
        resp = svc.handle("hello", "cliente1988", conversation_id="c1")
    assert resp.response == SECRET_ANSWER and resp.usage.total_tokens == 1155
    assert "could not store trace" in caplog.text


def test_a_broken_collector_never_fails_the_turn(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad price table")

    monkeypatch.setattr("app.service.price_table", boom)
    resp = ChatService(build(tmp_path)).handle("hello", "cliente1988", conversation_id="c1")
    assert resp.response == SECRET_ANSWER and resp.usage is None


def test_a_turn_above_the_token_or_cost_limit_logs_an_alert(tmp_path, priced, monkeypatch, caplog):
    from app.config import get_settings

    monkeypatch.setenv("MAX_TURN_TOKENS", "1000")
    monkeypatch.setenv("MAX_TURN_COST_USD", "0.000001")
    get_settings.cache_clear()
    with caplog.at_level(logging.WARNING, logger="chat"):
        ChatService(build(tmp_path)).handle("hello", "cliente1988", conversation_id="c1")
    alerts = [r for r in caplog.records if r.levelno == logging.WARNING and r.getMessage().startswith("ALERT")]
    assert any("turn_tokens 1155 > 1000" in r.getMessage() for r in alerts)
    assert any("turn_cost_usd" in r.getMessage() for r in alerts)


def test_no_alert_under_the_limits_and_a_429_is_an_alert():
    c = TurnCollector(dict(PRICES))
    assert turn_alerts(c.usage(), c, 1000, 1.0) == []
    c.spans.append({"kind": "llm", "node": "router", "name": BIG, "input_tokens": 0, "output_tokens": 0,
                    "cost_usd": 0.0, "ok": False, "error_type": "RateLimitError (429)", "latency_ms": 1})
    assert turn_alerts(c.usage(), c, 1000, 1.0) == ["provider_rate_limit RateLimitError (429)"]


# ---- deletion and retention ----

def test_delete_conversation_erases_its_traces_and_only_its_own(tmp_path, store, priced):
    from app.memory import open_memory

    mem = open_memory(tmp_path / "cp.sqlite")
    svc = ChatService(build(tmp_path), memory=mem, traces=store)
    svc.handle("hello", "cliente1988", conversation_id="c1")
    svc.graph = build(tmp_path)  # fresh fakes for the next turns
    svc.handle("hello", "cliente1988", conversation_id="c2")
    svc.graph = build(tmp_path)
    svc.handle("hello", "cliente2024", conversation_id="c1")
    assert store.count() == 3
    assert svc.delete_conversation("cliente2024", "c2") is False  # not theirs: nothing goes
    assert svc.delete_conversation("cliente1988", "c1") is True
    assert store.count() == 2
    rows = store.conn.execute("SELECT user_id, conversation_id FROM traces ORDER BY user_id").fetchall()
    assert rows == [("cliente1988", "c2"), ("cliente2024", "c1")]
    assert store.conn.execute("SELECT COUNT(*) FROM spans s LEFT JOIN traces t USING (trace_id) "
                              "WHERE t.trace_id IS NULL").fetchone()[0] == 0  # no orphan spans
    mem.close()


def record(store, trace_id, *, created_at, latency=100, degraded=False, handoff=None, spans=(), errors=0, user="u1",
           conv="c1"):
    spans = list(spans)
    store.record({"trace_id": trace_id, "user_id": user, "conversation_id": conv, "created_at": created_at,
                  "latency_ms": latency, "plan": ["support"], "degraded": degraded, "handoff_reason": handoff,
                  "input_tokens": sum(s["input_tokens"] for s in spans),
                  "output_tokens": sum(s["output_tokens"] for s in spans),
                  "cost_usd": sum_costs(s["cost_usd"] for s in spans), "provider_errors": errors}, spans)


def span(node, model, i, o, cost, kind="llm", ok=True):
    return {"kind": kind, "node": node, "name": model, "input_tokens": i, "output_tokens": o, "cost_usd": cost,
            "latency_ms": 10, "ok": ok, "error_type": None if ok else "X"}


def test_the_ttl_purge_erases_old_traces_through_the_retention_pass(tmp_path, store):
    from app.main import _purge_once
    from app.memory import open_memory

    now = time.time()
    record(store, "old", created_at=now - 40 * 86400, spans=[span("router", BIG, 1, 1, 0.1)])
    record(store, "new", created_at=now - 1 * 86400)
    mem = open_memory(tmp_path / "cp.sqlite", ttl_days=30)
    _purge_once(mem, None, store)
    assert [r[0] for r in store.conn.execute("SELECT trace_id FROM traces")] == ["new"]
    assert store.conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0] == 0
    assert store.purge_expired(0) == 0  # 0 = keep forever
    mem.close()


# ---- /operator/metrics ----

@pytest.fixture
def api(auth_env, store):
    from app.main import app

    app.state.service = ChatService(graph=None, traces=store)
    with TestClient(app) as c:
        yield c
    app.state.service = None


def seed_metrics(store):
    t0 = datetime(2026, 11, 10, tzinfo=timezone.utc).timestamp()
    record(store, "a", created_at=t0 + 10, latency=100, spans=[span("router", SMALL, 100, 10, 0.001),
                                                                 span("support", BIG, 1000, 100, 0.01),
                                                                 span("support", "get_payout_schedule", 0, 0, None, "tool")])
    record(store, "b", created_at=t0 + 20, latency=300, degraded=True, errors=1,
           spans=[span("router", SMALL, 100, 10, 0.001), span("knowledge", BIG, 0, 0, 0.0, ok=False)])
    record(store, "c", created_at=t0 + 30, latency=200, handoff="user_requested",
           spans=[span("router", "m-unpriced", 50, 5, None)])
    record(store, "late", created_at=t0 + 86400 * 5, latency=999, spans=[span("router", SMALL, 7, 7, 0.5)])
    return t0


def test_metrics_is_403_for_a_merchant_and_401_without_a_token(api):
    assert api.get("/operator/metrics", headers=bearer(make_jwt("cliente1988"))).status_code == 403
    assert api.get("/operator/metrics", headers=bearer(make_jwt("cliente1988", "chat operator"))).status_code == 403
    assert api.get("/operator/metrics").status_code == 401


def test_metrics_aggregates_for_an_operator(api, store):
    t0 = seed_metrics(store)
    since = datetime.fromtimestamp(t0, timezone.utc).isoformat()
    until = datetime.fromtimestamp(t0 + 86400, timezone.utc).isoformat()
    r = api.get("/operator/metrics", params={"since": since, "until": until}, headers=bearer(operator_jwt()))
    assert r.status_code == 200
    m = r.json()
    assert m["requests"] == 3  # "late" is outside the window
    assert (m["latency_ms_p50"], m["latency_ms_p95"]) == (200, 300)
    assert m["degraded_rate"] == pytest.approx(1 / 3) and m["handoff_rate"] == pytest.approx(1 / 3)
    assert m["provider_errors"] == 1
    assert (m["input_tokens"], m["output_tokens"]) == (1250, 125)
    assert m["cost_usd"] == pytest.approx(0.012) and m["unpriced_llm_calls"] == 1
    by_node = {g["node"]: g for g in m["by_node"]}
    assert (by_node["router"]["calls"], by_node["router"]["input_tokens"], by_node["router"]["unpriced_calls"]) == (3, 250, 1)
    assert by_node["knowledge"]["errors"] == 1
    by_model = {g["model"]: g for g in m["by_model"]}
    assert by_model["m-unpriced"]["cost_usd"] is None  # unknown, not 0
    assert by_model[BIG]["cost_usd"] == pytest.approx(0.01) and by_model[SMALL]["calls"] == 2
    assert m["tools"] == [{"name": "get_payout_schedule", "calls": 1, "errors": 0}]
    whole = api.get("/operator/metrics", headers=bearer(operator_jwt())).json()
    assert whole["requests"] == 4


def test_chat_through_the_api_carries_usage(auth_env, tmp_path, priced):
    from app.main import app

    app.state.service = ChatService(build(tmp_path))
    try:
        with TestClient(app) as c:
            r = c.post("/chat", json={"message": "hello"}, headers=bearer(make_jwt("cliente1988")))
    finally:
        app.state.service = None
    assert r.status_code == 200
    body = r.json()
    assert body["usage"]["total_tokens"] == 1155
    assert {g["node"] for g in body["usage"]["by_node"]} == {"router", "support", "knowledge", "synthesizer"}


# ---- nothing in the agents changed ----

PROMPT_SHA256 = {
    "JUDGE_PROMPT": "80f52019fb280750536f951ed21bd4607772c1a00af62e787f4cc2163cca03dd",
    "KNOWLEDGE_PROMPT": "de14433fd4badce8622520cf95ff59cb7e23268af150f99c0e2961960a1aec1e",
    "ROUTER_PROMPT": "991ab27c7e18644499336197a5912c7657923aa453929510d178d3a32ec12e64",
    "SUMMARIZER_PROMPT": "b7bc7fccb60b8954a85589562e3ecff2a66a1f83a5549fd61608524c791168f9",
    "SUPPORT_PROMPT": "6bec20eaa52185bbd3cd0ec3265556127008e98be5cd737eaf8808495e9dd93b",
    "SYNTHESIZER_DIRECT_PROMPT": "2a746dbcd9856012eb22205bcb2599f3fb228be9cdfc52076bb7b725a8129279",
    "SYNTHESIZER_MERGE_PROMPT": "465ba7903d2fefa8a58a79fd404ac76d14a4cb91f17e93843663dbdc7df88ecc",
}


def test_every_prompt_is_byte_for_byte_what_it_was_before_observability():
    import app.prompts as prompts

    found = {n: hashlib.sha256(getattr(prompts, n).encode("utf-8")).hexdigest()
             for n in dir(prompts) if n.endswith("_PROMPT")}
    assert found == PROMPT_SHA256
