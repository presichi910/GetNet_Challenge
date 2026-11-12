"""Offline evaluation.

    python -m eval.run_eval                 # all cases
    python -m eval.run_eval --only q05 x01  # only these ids (prefix)
    python -m eval.run_eval --no-judge      # skip the LLM judge
    python -m eval.run_eval --retries 3     # wait out per-minute rate limits (default 2)

Models per role come from LLM_MODEL (knowledge), LIGHT_MODEL (router, synthesizer), SUPPORT_MODEL (support,
default LIGHT_MODEL) and JUDGE_MODEL, and the report says which ones ran. A partial run (--only) never overwrites latest.md.

Per case: routing check, tool check, handoff check, and the LLM judge (relevance,
groundedness, completeness, tone, 1-5). Also latency p50/p95, degraded rate, and the tokens and
estimated cost of every case (agents and judge, per model; app/observability.py prices).

A case that hit a PROVIDER error (rate limit, exhausted quota, connection/5xx) says nothing
about the system's quality, so it is marked `error` and left out of every metric. A run with
provider errors does not overwrite eval/reports/latest.md.
"""
from __future__ import annotations

import argparse
import inspect
import json
import logging
import re
import statistics
import time
import uuid
from datetime import datetime
from pathlib import Path

from app.observability import PRICES_AS_OF, TurnCollector, price_table, sum_costs
from eval.judge import SCORE_KEYS, Judge

ROOT = Path(__file__).parent


def load_cases(only: list[str] | None) -> list[dict]:
    cases = [json.loads(l) for l in (ROOT / "dataset.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    return [c for c in cases if not only or any(c["id"].startswith(p) for p in only)]


def routing_ok(case: dict, plan: list[str]) -> bool:
    exp, opt, got = set(case["expected_agents"]), set(case.get("optional_agents", [])), set(plan)
    return exp <= got <= exp | opt


def tools_ok(case: dict, called: list[str]) -> bool:
    return set(case.get("expected_tools", [])) <= set(called)


def handoff_ok(case: dict, reason: str | None) -> bool | None:
    """True/False if the case has `expect_handoff`, None if it doesn't care."""
    expected = case.get("expect_handoff")
    return None if expected is None else (reason is not None) == expected


# Errors that come from the provider (quota, rate limit, outage), not from our agents.
# `BadRequestError ... tool_use_failed` (the model sent a broken tool call) is NOT here: that is
# a model failure and it counts against the system.
PROVIDER_ERROR_RE = re.compile(
    r"RateLimitError|APIConnectionError|APITimeoutError|InternalServerError|ServiceUnavailable|"
    r"ConnectError|ReadTimeout|Error code: (?:429|5\d\d)\b|rate limit|tokens per (?:day|minute)|"
    r"quota|overloaded|connection (?:error|reset|refused)|ModelNotFound|Model not found", re.I)


def is_provider_error(text: str | None) -> bool:
    return bool(text) and bool(PROVIDER_ERROR_RE.search(text))


def provider_errors_in(state: dict) -> list[tuple[str, str]]:
    """[(where, full error text)] for every provider failure in this run.

    The router and the synthesizer swallow their exceptions and use a fallback, so they leave
    the cause in `router_error` / `synth_error`. Agents leave it in their `error`."""
    found = []
    if is_provider_error(state.get("router_error")):
        found.append(("router", state["router_error"]))
    for agent, out in state.get("agent_outputs", {}).items():
        if is_provider_error(out.get("error")):
            found.append((agent, out["error"]))
    if is_provider_error(state.get("synth_error")):
        found.append(("synthesizer", state["synth_error"]))
    return found


def provider_error_in(state: dict) -> str | None:
    """Short text for the report ("router: RateLimitError: ..."), or None. Display only: it is
    cut, so never classify with it (the 'tokens per day' part may be cut off)."""
    found = provider_errors_in(state)
    if not found:
        return None
    return "; ".join(f"{where}: {' '.join(str(msg).split())[:160]}" for where, msg in found)


def is_daily_limit(text: str | None) -> bool:
    """A daily quota does not come back by waiting a few seconds, so it is never retried."""
    return bool(text) and bool(re.search(r"per day|\(TPD\)|\(RPD\)", text, re.I))


def retry_wait(text: str, floor: float = 5.0, cap: float = 70.0) -> float:
    """Seconds to wait after a per-minute limit: the 'try again in 7.2s' hint plus a margin."""
    ms = re.search(r"try again in ([\d.]+)ms", text or "")
    m = re.search(r"try again in (?:(\d+)m(?!s))?(?:([\d.]+)s)?", text or "")
    hinted = float(ms.group(1)) / 1000 if ms else (int(m.group(1) or 0) * 60 + float(m.group(2) or 0)) if m else 0.0
    return min(cap, max(floor, hinted + 2))


def history_messages(case: dict) -> list:
    """Earlier turns of a multi-turn case, as the messages the graph would hold after them. They are
    given to the graph as context, so setting the case up costs no model calls and every run of the
    case starts from exactly the same conversation. Only the LAST message of the case is evaluated."""
    from langchain_core.messages import AIMessage, HumanMessage

    return [(HumanMessage if t["role"] == "user" else AIMessage)(content=t["content"]) for t in case.get("history", [])]


def judge_question(case: dict) -> str:
    """What the judge reads as the user's question: the earlier turns (it cannot see the graph's memory) and the message."""
    if not case.get("history"):
        return case["message"]
    past = "\n".join(f"{'Customer' if t['role'] == 'user' else 'Assistant'}: {t['content']}" for t in case["history"])
    return f"(Earlier in the same conversation)\n{past}\n\n(Latest customer message)\n{case['message']}"


def error_row(case: dict, reason: str, latency_ms: int = 0) -> dict:
    return {"id": case["id"], "message": case["message"], "provider_error": reason, "latency_ms": latency_ms}


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


def _accepts(fn, name: str) -> bool:
    """True if `fn` takes the keyword `name` (or **kwargs): an older service without it still works."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def _prices() -> dict:
    from app.config import get_settings

    return price_table(get_settings().model_prices_json)


def run_case(service, case: dict, thread_base: str, retries: int, sleep,
             collector_out: list | None = None) -> tuple[dict | None, int, str | None]:
    """Run one case. A per-minute rate limit is waited out and retried (new thread each time);
    anything else, including a daily quota, is returned as it is. -> (state, latency_ms, exception).
    `collector_out` receives the TurnCollector of the attempt that is returned (tokens and cost)."""
    for attempt in range(retries + 1):
        state, latency, exc = None, 0, None
        collector = TurnCollector(_prices())
        try:
            extra = {"context": history_messages(case)} if case.get("history") else {}  # single-turn cases call it as before
            if _accepts(service.run_raw, "callbacks"):
                extra["callbacks"] = [collector]
            state, latency = service.run_raw(case["message"], case["user_id"], thread_id=f"{thread_base}-{attempt}", **extra)
        except Exception as e:  # noqa: BLE001
            exc = f"{type(e).__name__}: {e}"
        # classify with the FULL text: a daily quota must not be mistaken for a per-minute limit
        problem = exc or " ".join(msg for _, msg in provider_errors_in(state or {}))
        if is_provider_error(problem) and not is_daily_limit(problem) and attempt < retries:
            wait = retry_wait(problem)
            print(f"   rate limited, waiting {wait:.0f}s and retrying ({attempt + 1}/{retries})")
            sleep(wait)
            continue
        if collector_out is not None:
            collector_out.append(collector)
        return state, latency, exc
    raise AssertionError("unreachable")


def evaluate(cases: list[dict], service, judge: Judge | None, pause: float,
             retries: int = 0, sleep=time.sleep) -> list[dict]:
    run_id = uuid.uuid4().hex[:6]
    rows = []
    for c in cases:
        print(f"-> {c['id']}: {c['message']}")
        # new thread per case, no shared memory
        collectors: list[TurnCollector] = []
        state, latency, exc = run_case(service, c, f"eval-{run_id}-{c['id']}", retries, sleep, collectors)
        if exc is not None:
            if is_provider_error(exc):
                rows.append(error_row(c, f"run: {' '.join(exc.split())[:200]}"))
            else:
                rows.append({"id": c["id"], "error": exc, "routing_ok": False, "tools_ok": False, "latency_ms": 0})
            continue
        perr = provider_error_in(state)
        if perr:  # not a verdict on the system: mark it, skip the judge (saves quota) and move on
            print(f"   provider error, case excluded: {perr}")
            rows.append(error_row(c, perr, latency))
            continue
        outs = state.get("agent_outputs", {})
        called = [tc["name"] for o in outs.values() for tc in o.get("tool_calls", [])]
        evidence = [ctx for o in outs.values() for ctx in o.get("contexts", [])]
        if state.get("handoff"):  # so the judge knows the handoff really happened
            evidence.append(f"[handoff system action] a human handoff record and ticket were created: {state['handoff']}")
        row = {
            "id": c["id"], "message": c["message"], "plan": state.get("plan", []),
            "answer": state["final_answer"], "tools_called": called,
            # the exact calls (agent, tool, arguments), to see what the model really asked for
            "tool_calls": [{"agent": a, "name": tc["name"], "args": tc.get("args", {})}
                           for a, o in outs.items() for tc in o.get("tool_calls", [])],
            "routing_ok": routing_ok(c, state.get("plan", [])), "tools_ok": tools_ok(c, called),
            "degraded": any(o.get("error") for o in outs.values()), "latency_ms": latency,
        }
        reason = (state.get("handoff") or {}).get("reason")
        row["handoff"] = reason
        row["handoff_ok"] = handoff_ok(c, reason)
        if c.get("history"):
            row["history"] = c["history"]  # which conversation the message was answered in
        if collectors and collectors[0].spans:
            row["usage"] = collectors[0].usage()
        if judge:
            jc = TurnCollector(_prices())
            if _accepts(judge.score, "callbacks"):
                row["judge"] = judge.score(judge_question(c), row["answer"], evidence, c.get("criteria", ""),
                                           callbacks=[jc])
                if jc.spans:
                    row["judge_usage"] = jc.usage()
            else:
                row["judge"] = judge.score(judge_question(c), row["answer"], evidence, c.get("criteria", ""))
        rows.append(row)
        if pause:
            time.sleep(pause)  # avoid rate limits
    return rows


def summarize(rows: list[dict]) -> dict:
    """Metrics over the cases that really ran. Provider errors are counted apart, never averaged in."""
    provider_errors = sum(1 for r in rows if r.get("provider_error"))
    rows = [r for r in rows if not r.get("provider_error")]
    n = len(rows)
    if not n:
        return {"cases": 0, "provider_errors": provider_errors}
    judged = [r["judge"] for r in rows if r.get("judge") and r["judge"]["verdict"] != "error"]
    lat = [r["latency_ms"] for r in rows if r["latency_ms"]]
    s = {
        "cases": n,
        "provider_errors": provider_errors,
        "routing_accuracy": sum(r["routing_ok"] for r in rows) / n if n else 0,
        "tool_use_accuracy": sum(r["tools_ok"] for r in rows) / n if n else 0,
        "degraded_rate": sum(bool(r.get("degraded")) for r in rows) / n if n else 0,
        "latency_ms_p50": pct(lat, 0.5), "latency_ms_p95": pct(lat, 0.95),
    }
    checked = [r["handoff_ok"] for r in rows if r.get("handoff_ok") is not None]
    if checked:
        s["handoff_accuracy"] = sum(checked) / len(checked)  # only cases with an expectation
    s["handoff_rate"] = sum(bool(r.get("handoff")) for r in rows) / n if n else 0
    judge_errors = sum(1 for r in rows if r.get("judge", {}).get("verdict") == "error")
    if judge_errors:
        s["judge_errors"] = judge_errors  # judge failed (not scored), the case still counts for routing/tools
    if judged:
        for k in SCORE_KEYS:
            s[f"avg_{k}"] = round(statistics.mean(j[k] for j in judged), 2)
        s["judge_pass_rate"] = sum(j["verdict"] == "pass" for j in judged) / len(judged)
    s.update(usage_summary(rows))
    return s


def usage_summary(rows: list[dict]) -> dict:
    """Tokens per model (agents and judge apart) and the estimated cost. A cost is None when a model of
    that group has no price. Empty when no row recorded usage (e.g. an old report)."""
    agents = [g for r in rows for g in (r.get("usage") or {}).get("by_node", [])]
    judges = [g for r in rows for g in (r.get("judge_usage") or {}).get("by_node", [])]
    if not agents and not judges:
        return {}
    by_model: dict[tuple[str, str], dict] = {}
    for role, groups in (("agents", agents), ("judge", judges)):
        for g in groups:
            m = by_model.setdefault((role, g["model"]), {"role": role, "model": g["model"], "calls": 0,
                                                         "input_tokens": 0, "output_tokens": 0, "costs": []})
            m["calls"] += g["calls"]
            m["input_tokens"] += g["input_tokens"]
            m["output_tokens"] += g["output_tokens"]
            m["costs"].append(g["cost_usd"])
    models = []
    for m in by_model.values():
        costs = m.pop("costs")
        models.append({**m, "cost_usd": sum_costs(costs)})
    agent_cost = sum_costs(m["cost_usd"] for m in models if m["role"] == "agents")
    judge_cost = sum_costs(m["cost_usd"] for m in models if m["role"] == "judge")
    case_tokens = [r["usage"]["total_tokens"] for r in rows if r.get("usage")]
    return {
        "agent_tokens": sum(m["input_tokens"] + m["output_tokens"] for m in models if m["role"] == "agents"),
        "judge_tokens": sum(m["input_tokens"] + m["output_tokens"] for m in models if m["role"] == "judge"),
        "agent_tokens_per_case_mean": round(statistics.mean(case_tokens)) if case_tokens else 0,
        "agent_cost_usd": agent_cost, "judge_cost_usd": judge_cost,
        "total_cost_usd": sum_costs([agent_cost, judge_cost]),
        "tokens_by_model": models,
    }


MAX_SCORE = 5  # judge scale is 1-5; reports show it as a percentage of the maximum


def _score_pct(v) -> str:
    return "-" if v is None else f"{v / MAX_SCORE * 100:.0f}%"


def _usd(v) -> str:
    return "n/a" if v is None else f"${v:.4f}"


def current_models() -> dict:
    from app.config import get_settings

    s = get_settings()
    return {"agent provider": s.agent_provider, "knowledge": s.llm_model, "router / synthesizer": s.light_model,
            "support": s.effective_support_model, "judge": f"{s.judge_model} (groq)"}


def render_report(rows: list[dict], summary: dict, stamp: str, models: dict | None = None) -> str:
    """Markdown report. Rates and judge scores are shown as percentages."""
    lines = ["# Evaluation report", "", f"_Generated {stamp}_", ""]
    if models:
        lines += ["_Models: " + ", ".join(f"{role} = `{name}`" for role, name in models.items()) + "_", ""]
    lines += ["## Summary", "", f"_Judge scores are shown as a percentage of the maximum ({MAX_SCORE})._", ""]
    if summary.get("provider_errors"):
        lines += [f"**{summary['provider_errors']} case(s) hit a provider error (rate limit / quota / outage) "
                  "and are excluded from every metric below.**", ""]
    for k, v in summary.items():
        if k in ("cases", "tokens_by_model"):
            continue
        if k.endswith("_cost_usd"):
            lines.append(f"- **{k}**: {_usd(v)}")
        elif k.startswith("avg_"):
            lines.append(f"- **{k}**: {_score_pct(v)}")
        elif k.endswith(("_accuracy", "_rate")):
            lines.append(f"- **{k}**: {v * 100:.0f}%")
        else:
            lines.append(f"- **{k}**: {v}")
    if summary.get("tokens_by_model"):
        lines += ["", "## Tokens and estimated cost per model", "",
                  f"_Prices: app/observability.py (USD per 1M tokens, list prices of {PRICES_AS_OF}); "
                  "a model without a price shows `n/a`, not 0._", "",
                  "| role | model | calls | input | output | cost USD |", "|---|---|---|---|---|---|"]
        for m in summary["tokens_by_model"]:
            lines.append(f"| {m['role']} | `{m['model']}` | {m['calls']} | {m['input_tokens']} | {m['output_tokens']} "
                         f"| {_usd(m['cost_usd'])} |")
    lines += ["", "## Cases", "", "_tokens / cost: the agents of the case (the judge is apart, above)._", "",
              "| id | route | tools | handoff | rel | grounded | compl | tone | verdict | ms | tokens | cost USD |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r.get("provider_error"):
            lines.append(f"| {r['id']} | - | - | - | - | - | - | - | error | {r['latency_ms']} | - | - |")
            continue
        j = r.get("judge", {})
        u = r.get("usage") or {}
        h_ok = {True: "OK", False: "FAIL", None: "-"}[r.get("handoff_ok")]
        lines.append(f"| {r['id']} | {'OK' if r['routing_ok'] else 'FAIL'} {r.get('plan', '')} | {'OK' if r['tools_ok'] else 'FAIL'} "
                     f"| {h_ok} {r.get('handoff') or ''} | {_score_pct(j.get('relevance'))} | {_score_pct(j.get('groundedness'))} "
                     f"| {_score_pct(j.get('completeness'))} | {_score_pct(j.get('tone'))} "
                     f"| {j.get('verdict', '-')} | {r['latency_ms']} | {u.get('total_tokens', '-')} "
                     f"| {_usd(u['cost_usd']) if u else '-'} |")
    errored = [r for r in rows if r.get("provider_error")]
    if errored:
        lines += ["", "## Provider errors (excluded from the metrics, re-run these cases)", ""]
        lines += [f"- **{r['id']}**: {r['provider_error']}" for r in errored]
    failures = [r for r in rows if r.get("judge", {}).get("verdict") == "fail"]
    if failures:
        lines += ["", "## Judge failures", ""]
        lines += [f"- **{r['id']}**: {r['judge']['reason']}" for r in failures]
    return "\n".join(lines)


def write_report(rows: list[dict], summary: dict, models: dict | None = None, update_latest: bool = True) -> Path:
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out / f"{stamp}.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    text = render_report(rows, summary, stamp, models)
    path = out / f"{stamp}.md"
    path.write_text(text, encoding="utf-8")
    # a partial run or one with provider errors must not replace the versioned full report
    if update_latest and not summary.get("provider_errors"):
        (out / "latest.md").write_text(text, encoding="utf-8")
    return path


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="case id prefixes to run")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between cases")
    ap.add_argument("--retries", type=int, default=2, help="times to wait out a per-minute rate limit per case")
    args = ap.parse_args()

    from app.graph import build_default_graph
    from app.service import ChatService

    cases = load_cases(args.only)
    models = current_models()
    print("Models:", ", ".join(f"{role}={name}" for role, name in models.items()))
    rows = evaluate(cases, ChatService(build_default_graph()), None if args.no_judge else Judge(), args.pause,
                    retries=args.retries)
    summary = summarize(rows)
    path = write_report(rows, summary, models, update_latest=not args.only)
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"\nReport: {path}")
    errored = [r["id"] for r in rows if r.get("provider_error")]
    if errored:
        print(f"\nWARNING: {len(errored)} case(s) hit a provider error (quota / rate limit / outage) and were "
              "left out of the metrics.\n"
              f"Re-run them when the provider recovers: python -m eval.run_eval --only {' '.join(errored)}")


if __name__ == "__main__":
    main()
