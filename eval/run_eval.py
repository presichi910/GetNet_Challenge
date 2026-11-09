"""Offline evaluation harness.

    python -m eval.run_eval                 # all cases, in-process graph
    python -m eval.run_eval --only q05 x01  # subset (id prefixes)
    python -m eval.run_eval --no-judge      # deterministic checks only (routing + tool use)

Three layers of signal per case:
  1. Routing accuracy   (deterministic) - did the router pick the right specialists?
  2. Tool-use accuracy  (deterministic) - did the agents call the tools that should ground the answer?
  3. Answer quality     (LLM judge)     - relevance / groundedness / completeness / tone, 1-5.
Also reports latency (p50/p95) and the share of degraded responses.
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import time
import uuid
from datetime import datetime
from pathlib import Path

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
    """True/False when the case states an expectation (`expect_handoff`), None when it does not care."""
    expected = case.get("expect_handoff")
    return None if expected is None else (reason is not None) == expected


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


def evaluate(cases: list[dict], service, judge: Judge | None, pause: float) -> list[dict]:
    run_id = uuid.uuid4().hex[:6]
    rows = []
    for c in cases:
        print(f"-> {c['id']}: {c['message']}")
        try:
            # unique thread per case: no cross-case conversation memory
            state, latency = service.run_raw(c["message"], c["user_id"], thread_id=f"eval-{run_id}-{c['id']}")
        except Exception as e:  # noqa: BLE001
            rows.append({"id": c["id"], "error": str(e), "routing_ok": False, "tools_ok": False, "latency_ms": 0})
            continue
        outs = state.get("agent_outputs", {})
        called = [tc["name"] for o in outs.values() for tc in o.get("tool_calls", [])]
        evidence = [ctx for o in outs.values() for ctx in o.get("contexts", [])]
        if state.get("handoff"):  # the judge must see that the handoff really happened (ids are not invented)
            evidence.append(f"[handoff system action] a human handoff record and ticket were created: {state['handoff']}")
        row = {
            "id": c["id"], "message": c["message"], "plan": state.get("plan", []),
            "answer": state["final_answer"], "tools_called": called,
            "routing_ok": routing_ok(c, state.get("plan", [])), "tools_ok": tools_ok(c, called),
            "degraded": any(o.get("error") for o in outs.values()), "latency_ms": latency,
        }
        reason = (state.get("handoff") or {}).get("reason")
        row["handoff"] = reason
        row["handoff_ok"] = handoff_ok(c, reason)
        if judge:
            row["judge"] = judge.score(c["message"], row["answer"], evidence, c.get("criteria", ""))
        rows.append(row)
        if pause:
            time.sleep(pause)  # be gentle with provider rate limits
    return rows


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    judged = [r["judge"] for r in rows if r.get("judge") and r["judge"]["verdict"] != "error"]
    lat = [r["latency_ms"] for r in rows if r["latency_ms"]]
    s = {
        "cases": n,
        "routing_accuracy": sum(r["routing_ok"] for r in rows) / n if n else 0,
        "tool_use_accuracy": sum(r["tools_ok"] for r in rows) / n if n else 0,
        "degraded_rate": sum(bool(r.get("degraded")) for r in rows) / n if n else 0,
        "latency_ms_p50": pct(lat, 0.5), "latency_ms_p95": pct(lat, 0.95),
    }
    checked = [r["handoff_ok"] for r in rows if r.get("handoff_ok") is not None]
    if checked:
        s["handoff_accuracy"] = sum(checked) / len(checked)  # right call on every case that states an expectation
    s["handoff_rate"] = sum(bool(r.get("handoff")) for r in rows) / n if n else 0
    if judged:
        for k in SCORE_KEYS:
            s[f"avg_{k}"] = round(statistics.mean(j[k] for j in judged), 2)
        s["judge_pass_rate"] = sum(j["verdict"] == "pass" for j in judged) / len(judged)
    return s


def write_report(rows: list[dict], summary: dict) -> Path:
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out / f"{stamp}.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = ["# Evaluation report", "", f"_Generated {stamp}_", "", "## Summary", ""]
    lines += [f"- **{k}**: {v:.2f}" if isinstance(v, float) else f"- **{k}**: {v}" for k, v in summary.items()]
    lines += ["", "## Cases", "", "| id | route | tools | handoff | rel | grounded | compl | tone | verdict | ms |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        j = r.get("judge", {})
        h_ok = {True: "OK", False: "FAIL", None: "-"}[r.get("handoff_ok")]
        lines.append(f"| {r['id']} | {'OK' if r['routing_ok'] else 'FAIL'} {r.get('plan', '')} | {'OK' if r['tools_ok'] else 'FAIL'} "
                     f"| {h_ok} {r.get('handoff') or ''} | {j.get('relevance', '-')} | {j.get('groundedness', '-')} | {j.get('completeness', '-')} | {j.get('tone', '-')} "
                     f"| {j.get('verdict', '-')} | {r['latency_ms']} |")
    failures = [r for r in rows if r.get("judge", {}).get("verdict") == "fail"]
    if failures:
        lines += ["", "## Judge failures", ""]
        lines += [f"- **{r['id']}**: {r['judge']['reason']}" for r in failures]
    path = out / f"{stamp}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    (out / "latest.md").write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="case id prefixes to run")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between cases")
    args = ap.parse_args()

    from app.graph import build_default_graph
    from app.service import ChatService

    cases = load_cases(args.only)
    rows = evaluate(cases, ChatService(build_default_graph()), None if args.no_judge else Judge(), args.pause)
    summary = summarize(rows)
    path = write_report(rows, summary)
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"\nReport: {path}")


if __name__ == "__main__":
    main()
