"""Observability: tokens, cost and per-node spans of every turn, kept in SQLite.

- `TurnCollector` is a LangChain callback handler, one per request, attached through the config of
  `graph.invoke` (no prompt or node changes). For every LLM call it records the LangGraph node, the model,
  input/output tokens (`usage_metadata`), latency and the error type; for every tool call its name,
  latency and ok/error. It never keeps message text, answers or tool results.
- `cost_usd` uses the price table below (USD per million tokens). A model without a price has
  `cost_usd = None`, never 0, and a warning is logged once per model.
- `TraceStore` keeps one row per turn (`traces`) and one per LLM/tool call (`spans`) in its own SQLite
  file (`TRACES_DB_PATH`). It holds ids, counts and timings only. A conversation's traces are erased with
  `DELETE /conversations/{id}`, and traces older than `THREAD_TTL_DAYS` by the retention purge.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

log = logging.getLogger("observability")

# USD per 1M tokens (input, output). Serverless list prices as of PRICES_AS_OF, from
# https://docs.fireworks.ai/serverless/pricing and https://console.groq.com/docs/models.
# Fireworks does not list gpt-oss-20b (it is not served serverless on the account used), so it has no
# price on purpose: its cost is reported as null. Override or extend with MODEL_PRICES_JSON, e.g.
# MODEL_PRICES_JSON='{"accounts/fireworks/models/gpt-oss-20b": [0.07, 0.30]}'.
PRICES_AS_OF = "2026-11-12"
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "accounts/fireworks/models/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-120b": (0.15, 0.60),   # Groq
    "openai/gpt-oss-20b": (0.075, 0.30),   # Groq
    "qwen/qwen3.8-27b": (0.80, 4.00),      # Groq, the eval judge
}
NODES = ("summarize", "router", "knowledge", "support", "synthesizer", "handoff")
RATE_LIMIT_RE = re.compile(r"RateLimit|\b429\b|rate.?limit|too many requests", re.I)


def price_table(override_json: str = "") -> dict[str, tuple[float, float]]:
    """The default prices plus MODEL_PRICES_JSON ({model: [input, output]}). A bad override is logged and ignored."""
    table = dict(DEFAULT_PRICES)
    if override_json:
        try:
            for model, (inp, out) in json.loads(override_json).items():
                table[model] = (float(inp), float(out))
        except (ValueError, TypeError) as e:
            log.warning("MODEL_PRICES_JSON ignored, it is not {model: [input, output]}: %s", e)
    return table


_unpriced_warned: set[str] = set()


def cost_usd(model: str, input_tokens: int, output_tokens: int, prices: dict[str, tuple[float, float]]) -> float | None:
    """USD for one call, or None if the model has no price (never 0: an unknown cost is not free)."""
    price = prices.get(model)
    if price is None:
        if model not in _unpriced_warned:
            _unpriced_warned.add(model)
            log.warning("no price for model %r: its cost is reported as null (set MODEL_PRICES_JSON)", model)
        return None
    inp, out = price
    return (input_tokens * inp + output_tokens * out) / 1_000_000


def sum_costs(costs) -> float | None:
    """Total of several costs; None if any of them is unknown."""
    total = 0.0
    for c in costs:
        if c is None:
            return None
        total += c
    return total


def _node_of(metadata: dict | None) -> str:
    """The top-level LangGraph node of a run. Inside a node, a nested agent (create_agent) reports its own
    node names ("model", "tools"), but the checkpoint namespace starts with the outer one ("support:<id>|...")."""
    md = metadata or {}
    ns = md.get("langgraph_checkpoint_ns") or ""
    first = ns.split("|", 1)[0].split(":", 1)[0]
    return first or md.get("langgraph_node") or "unknown"


def _model_of(metadata: dict | None, kwargs: dict) -> str:
    params = kwargs.get("invocation_params") or {}
    return str((metadata or {}).get("ls_model_name") or params.get("model") or params.get("model_name") or "unknown")


def _usage_of(response) -> tuple[int, int]:
    try:
        msg = response.generations[0][0].message
        usage = getattr(msg, "usage_metadata", None) or {}
        if usage:
            return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
    except (AttributeError, IndexError):
        pass
    token_usage = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
    return int(token_usage.get("prompt_tokens", 0)), int(token_usage.get("completion_tokens", 0))


def _error_type(error: BaseException) -> str:
    """The error class and, if there is one, the HTTP status. Never the message (it can echo the input)."""
    status = getattr(error, "status_code", None)
    name = type(error).__name__
    if status is None and RATE_LIMIT_RE.search(name + " " + str(error)[:200]):
        status = 429
    return f"{name} ({status})" if status else name


class TurnCollector(BaseCallbackHandler):
    """Spans of one turn. Thread-safe: LangGraph may call back from worker threads."""

    raise_error = False  # an observability bug must never break a turn

    def __init__(self, prices: dict[str, tuple[float, float]] | None = None, clock=time.perf_counter):
        self.prices = prices if prices is not None else price_table()
        self.clock = clock
        self.spans: list[dict] = []
        self._open: dict[UUID, dict] = {}
        self._lock = threading.Lock()

    # ---- LLM ----
    def on_chat_model_start(self, serialized, messages, *, run_id: UUID, metadata=None, **kwargs: Any) -> None:
        self._start(run_id, {"kind": "llm", "node": _node_of(metadata), "name": _model_of(metadata, kwargs)})

    def on_llm_start(self, serialized, prompts, *, run_id: UUID, metadata=None, **kwargs: Any) -> None:
        self._start(run_id, {"kind": "llm", "node": _node_of(metadata), "name": _model_of(metadata, kwargs)})

    def on_llm_end(self, response, *, run_id: UUID, **kwargs: Any) -> None:
        span = self._finish(run_id)
        if span is None:
            return
        span["input_tokens"], span["output_tokens"] = _usage_of(response)
        span["cost_usd"] = cost_usd(span["name"], span["input_tokens"], span["output_tokens"], self.prices)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        span = self._finish(run_id)
        if span is not None:
            span.update(ok=False, error_type=_error_type(error), cost_usd=0.0 if span["name"] in self.prices else None)

    # ---- tools ----
    def on_tool_start(self, serialized, input_str, *, run_id: UUID, metadata=None, **kwargs: Any) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name") or "unknown"
        self._start(run_id, {"kind": "tool", "node": _node_of(metadata), "name": str(name)})

    def on_tool_end(self, output, *, run_id: UUID, **kwargs: Any) -> None:
        self._finish(run_id)  # the result is never kept

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        span = self._finish(run_id)
        if span is not None:
            span.update(ok=False, error_type=_error_type(error))

    # ---- internals ----
    def _start(self, run_id: UUID, span: dict) -> None:
        span.update(t0=self.clock(), input_tokens=0, output_tokens=0, cost_usd=None, ok=True, error_type=None,
                    latency_ms=0)
        with self._lock:
            self._open[run_id] = span

    def _finish(self, run_id: UUID) -> dict | None:
        with self._lock:
            span = self._open.pop(run_id, None)
            if span is None:
                return None
            span["latency_ms"] = int((self.clock() - span.pop("t0")) * 1000)
            self.spans.append(span)
            return span

    # ---- results ----
    def llm_spans(self) -> list[dict]:
        return [s for s in self.spans if s["kind"] == "llm"]

    def usage(self) -> dict:
        """{input_tokens, output_tokens, total_tokens, cost_usd, by_node: [{node, model, calls, ...}]}."""
        groups: dict[tuple[str, str], dict] = {}
        for s in self.llm_spans():
            g = groups.setdefault((s["node"], s["name"]), {"node": s["node"], "model": s["name"], "calls": 0,
                                                             "input_tokens": 0, "output_tokens": 0, "costs": []})
            g["calls"] += 1
            g["input_tokens"] += s["input_tokens"]
            g["output_tokens"] += s["output_tokens"]
            g["costs"].append(s["cost_usd"])
        by_node = []
        for g in groups.values():
            costs = g.pop("costs")
            by_node.append({**g, "cost_usd": sum_costs(costs)})
        order = {n: i for i, n in enumerate(NODES)}
        by_node.sort(key=lambda g: (order.get(g["node"], len(order)), g["model"]))
        inp = sum(g["input_tokens"] for g in by_node)
        out = sum(g["output_tokens"] for g in by_node)
        return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
                "cost_usd": sum_costs(s["cost_usd"] for s in self.llm_spans()), "by_node": by_node}

    def provider_errors(self) -> list[str]:
        return [s["error_type"] for s in self.llm_spans() if not s["ok"]]

    def rate_limited(self) -> bool:
        return any(RATE_LIMIT_RE.search(e or "") for e in self.provider_errors())


def turn_alerts(usage: dict, collector: TurnCollector, max_tokens: int, max_cost: float) -> list[str]:
    """What is wrong with this turn, for a WARNING log line (empty: nothing)."""
    alerts = []
    if max_tokens and usage["total_tokens"] > max_tokens:
        alerts.append(f"turn_tokens {usage['total_tokens']} > {max_tokens}")
    if max_cost and usage["cost_usd"] is not None and usage["cost_usd"] > max_cost:
        alerts.append(f"turn_cost_usd {usage['cost_usd']:.6f} > {max_cost}")
    if collector.rate_limited():
        alerts.append("provider_rate_limit " + ",".join(e for e in collector.provider_errors() if RATE_LIMIT_RE.search(e)))
    return alerts


# ---------------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conversation_id TEXT NOT NULL, created_at REAL NOT NULL,
    latency_ms INTEGER NOT NULL, plan TEXT NOT NULL, degraded INTEGER NOT NULL, handoff_reason TEXT,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cost_usd REAL, provider_errors INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS traces_conv ON traces (user_id, conversation_id);
CREATE INDEX IF NOT EXISTS traces_time ON traces (created_at);
CREATE TABLE IF NOT EXISTS spans (
    trace_id TEXT NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE, seq INTEGER NOT NULL,
    kind TEXT NOT NULL, node TEXT NOT NULL, name TEXT NOT NULL, input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL, cost_usd REAL, latency_ms INTEGER NOT NULL, ok INTEGER NOT NULL, error_type TEXT,
    PRIMARY KEY (trace_id, seq));
"""


def _percentile(values: list[int], p: float) -> int | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


class TraceStore:
    """traces + spans in one SQLite file. No message text, answers or tool results ever reach it."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA secure_delete=ON")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def record(self, trace: dict, spans: list[dict]) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO traces VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (trace["trace_id"], trace["user_id"], trace["conversation_id"], trace.get("created_at", time.time()),
                 trace["latency_ms"], json.dumps(trace.get("plan", [])), int(bool(trace.get("degraded"))),
                 trace.get("handoff_reason"), trace["input_tokens"], trace["output_tokens"], trace.get("cost_usd"),
                 trace.get("provider_errors", 0)))
            self.conn.executemany(
                "INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(trace["trace_id"], i, s["kind"], s["node"], s["name"], s["input_tokens"], s["output_tokens"],
                  s["cost_usd"], s["latency_ms"], int(bool(s["ok"])), s["error_type"]) for i, s in enumerate(spans)])
            self.conn.commit()

    def _delete(self, where: str, args: tuple) -> int:
        with self.lock:
            ids = [r[0] for r in self.conn.execute(f"SELECT trace_id FROM traces WHERE {where}", args)]
            if ids:
                marks = ",".join("?" * len(ids))
                self.conn.execute(f"DELETE FROM spans WHERE trace_id IN ({marks})", ids)
                self.conn.execute(f"DELETE FROM traces WHERE trace_id IN ({marks})", ids)
                self.conn.commit()
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return len(ids)

    def delete_conversation(self, user_id: str, conversation_id: str) -> int:
        return self._delete("user_id = ? AND conversation_id = ?", (user_id, conversation_id))

    def purge_expired(self, ttl_days: float, now: float | None = None) -> int:
        """Traces older than `ttl_days` (0 = keep). Same window as the conversations they belong to."""
        if not ttl_days:
            return 0
        cutoff = (time.time() if now is None else now) - ttl_days * 86400
        n = self._delete("created_at < ?", (cutoff,))
        if n:
            log.info("retention: purged %d traces older than %s days", n, ttl_days)
        return n

    def count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]

    def metrics(self, since: float | None = None, until: float | None = None) -> dict:
        """Aggregates over the traces with since <= created_at < until."""
        where, args = "1=1", []
        if since is not None:
            where += " AND t.created_at >= ?"
            args.append(since)
        if until is not None:
            where += " AND t.created_at < ?"
            args.append(until)
        with self.lock:
            rows = self.conn.execute(
                f"SELECT latency_ms, degraded, handoff_reason, provider_errors FROM traces t WHERE {where}", args).fetchall()
            spans = self.conn.execute(
                f"SELECT s.node, s.name, s.input_tokens, s.output_tokens, s.cost_usd, s.ok FROM spans s "
                f"JOIN traces t ON t.trace_id = s.trace_id WHERE s.kind = 'llm' AND {where}", args).fetchall()
            tools = self.conn.execute(
                f"SELECT s.name, COUNT(*), SUM(1 - s.ok) FROM spans s JOIN traces t ON t.trace_id = s.trace_id "
                f"WHERE s.kind = 'tool' AND {where} GROUP BY s.name ORDER BY s.name", args).fetchall()
        n = len(rows)

        def group(key_index: int, key_name: str) -> list[dict]:
            out: dict[str, dict] = {}
            for sp in spans:
                g = out.setdefault(sp[key_index], {key_name: sp[key_index], "calls": 0, "errors": 0, "input_tokens": 0,
                                                    "output_tokens": 0, "cost_usd": 0.0, "unpriced_calls": 0})
                g["calls"] += 1
                g["errors"] += 0 if sp[5] else 1
                g["input_tokens"] += sp[2]
                g["output_tokens"] += sp[3]
                if sp[4] is None:
                    g["unpriced_calls"] += 1
                else:
                    g["cost_usd"] += sp[4]
            for g in out.values():
                if g["unpriced_calls"] == g["calls"]:
                    g["cost_usd"] = None  # nothing in this group has a price
            return sorted(out.values(), key=lambda g: g[key_name])

        priced = [sp[4] for sp in spans if sp[4] is not None]
        return {
            "requests": n,
            "latency_ms_p50": _percentile([r[0] for r in rows], 0.5),
            "latency_ms_p95": _percentile([r[0] for r in rows], 0.95),
            "degraded_rate": (sum(r[1] for r in rows) / n) if n else None,
            "handoff_rate": (sum(1 for r in rows if r[2]) / n) if n else None,
            "provider_errors": sum(r[3] for r in rows),
            "input_tokens": sum(sp[2] for sp in spans),
            "output_tokens": sum(sp[3] for sp in spans),
            "cost_usd": sum(priced) if priced or not spans else None,
            "unpriced_llm_calls": sum(1 for sp in spans if sp[4] is None),
            "by_node": group(0, "node"),
            "by_model": group(1, "model"),
            "tools": [{"name": t[0], "calls": t[1], "errors": t[2]} for t in tools],
            "prices_as_of": PRICES_AS_OF,
        }

    def close(self) -> None:
        with self.lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self.conn.close()
