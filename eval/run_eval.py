"""Offline evaluation.

    python -m eval.run_eval                 # all cases
    python -m eval.run_eval --only q05 x01  # only these ids (prefix)
    python -m eval.run_eval --no-judge      # skip the LLM judge
    python -m eval.run_eval --retries 3     # wait out per-minute rate limits (default 2)
    python -m eval.run_eval --repeats 3     # 3 runs + eval/reports/REPEATS-<stamp>.md (spread, unstable cases)
    python -m eval.run_eval --keep-latest   # never overwrite latest.md

Models per role come from LLM_MODEL (knowledge), LIGHT_MODEL (router, synthesizer), SUPPORT_MODEL (support,
default LIGHT_MODEL) and JUDGE_MODEL, and the report says which ones ran. A partial run (--only) never overwrites latest.md.

Per case: routing check, tool check, handoff check, and the LLM judge (relevance,
groundedness, completeness, tone, 1-5). Also latency p50/p95, degraded rate, and the tokens and
estimated cost of every case (agents and judge, per model; app/observability.py prices).

Holdout cases ("holdout": true in the dataset) are never used to tune a prompt (ho06, ho08 and ho12 are "baseline seen": see docs/FINAL_REPORT.md, section 7).
They run with the rest but are summarized apart ("Holdout" section), so the main metrics stay comparable
with earlier reports and the holdout shows how the system does on cases nobody tuned it for.

A case that hit a PROVIDER error (rate limit, exhausted quota, connection/5xx) says nothing
about the system's quality, so it is marked `error` and left out of every metric. A run with
provider errors does not overwrite eval/reports/latest.md.
"""
from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import re
import statistics
import time
import uuid
from datetime import datetime
from pathlib import Path

from app.observability import PRICES_AS_OF, TurnCollector, price_table, sum_costs
from eval.judge import SCORE_KEYS, Judge

ROOT = Path(__file__).parent


def custom_dataset() -> Path | None:
    """The cases file named by EVAL_DATASET, or None (the default: eval/dataset.jsonl). It lets a run and the judge's batch tool (eval/rejudge.py, which reads the
    cases through `load_cases`) work on another file with the same schema, e.g. the replan probe, without touching the dataset. Unset or blank = today's behavior."""
    value = os.environ.get("EVAL_DATASET", "").strip()
    return Path(value) if value else None


def dataset_path() -> Path:
    return custom_dataset() or ROOT / "dataset.jsonl"


def should_update_latest(only: list[str] | None, keep_latest: bool, label: str | None) -> bool:
    """latest.md is the report of the main run: a partial run, a labeled branch or a run on another dataset never replaces it."""
    return not only and not keep_latest and not label and custom_dataset() is None


HOLDOUT_V3 = ROOT / "holdout_v3.jsonl"


def with_holdout_v3() -> bool:
    """True when EVAL_WITH_HOLDOUT_V3 is set (--with-holdout-v3 sets it; eval/rejudge.py reads the cases through `load_cases`, so it sees the same set).
    The v3 cases are appended after the dataset's; unset (the default) nothing changes."""
    return os.environ.get("EVAL_WITH_HOLDOUT_V3", "").strip().lower() in ("1", "true", "yes")


def load_cases(only: list[str] | None, regular_only: bool = False) -> list[dict]:
    path = dataset_path()
    if not path.exists():
        raise SystemExit(f"run_eval: the cases file {path} does not exist (EVAL_DATASET={os.environ.get('EVAL_DATASET', '')!r})")
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if with_holdout_v3():
        cases += [json.loads(line) for line in HOLDOUT_V3.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [c for c in cases if (not only or any(c["id"].startswith(p) for p in only)) and not (regular_only and c.get("holdout"))]


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
    row = {"id": case["id"], "message": case["message"], "provider_error": reason, "latency_ms": latency_ms}
    if case.get("holdout"):
        row["holdout"] = True
    return row


def split_holdout(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(main rows, holdout rows)."""
    return [r for r in rows if not r.get("holdout")], [r for r in rows if r.get("holdout")]


HOLDOUT_ORIGINAL_LAST = 5  # ho01-ho05 were seen during development; ho06 and later are the new holdout (ho06, ho08 and ho12 are baseline seen)
HOLDOUT_GROUPS = {"original": "original (ho01-ho05), seen during development",
                  "blind": "new (ho06 and later; ho06, ho08 and ho12 are baseline seen)",
                  "v3": "v3 (hv3_01-hv3_20; written and committed before any plan 9.6 behavior change)"}
# What the per-case marks say. The key "blind" is the historical name of the new holdout group: saved summaries and the final report read it, so only the text changed.
HOLDOUT_MARK = {"original": "original", "blind": "new", "v3": "v3"}


def holdout_group(case_id: str) -> str:
    """The group "blind" (the new holdout, historical key) only for ids ho06 and later; every other holdout id counts as "original" (the new
    group is never assumed). Derived from the id, so no case in the dataset has to change."""
    if case_id.startswith("hv3_"):
        return "v3"
    m = re.match(r"ho(\d+)", case_id)
    return "blind" if m and int(m.group(1)) > HOLDOUT_ORIGINAL_LAST else "original"


def summarize_all(rows: list[dict]) -> dict:
    """The main summary (holdout cases left out) plus, if there are holdout cases, their own summary
    under "holdout". `provider_errors` counts both, so a run with an error anywhere never becomes latest.md."""
    main, held = split_holdout(rows)
    s = summarize(main)
    if held:
        s["holdout"] = summarize(held)
        # the two holdouts apart: the original one was seen while developing, the new one was not
        s["holdout_groups"] = {g: summarize([r for r in held if holdout_group(r["id"]) == g])
                               for g in HOLDOUT_GROUPS if any(holdout_group(r["id"]) == g for r in held)}
        s["provider_errors"] = s.get("provider_errors", 0) + s["holdout"].get("provider_errors", 0)
        s.update(usage_summary(rows))  # tokens and cost: the whole run, holdout included
    return s


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
            thread_id = f"{thread_base}-{attempt}"
            history = history_messages(case)
            persistent = bool(history) and hasattr(service, "seed")
            if persistent:
                # the earlier turns go INTO the thread, the checkpointer and the graph are reopened (a restart), and only then is
                # the last message answered: the memory the API really uses is what the case runs on (eval/persistent.py)
                service.seed(thread_id, case["user_id"], history)
                service.restart()
                restored = service.thread_messages(thread_id)
            extra = {"context": history} if history and not persistent else {}  # single-turn cases call it as before
            if _accepts(service.run_raw, "callbacks"):
                extra["callbacks"] = [collector]
            state, latency = service.run_raw(case["message"], case["user_id"], thread_id=thread_id, **extra)
            if persistent:
                after = service.thread_messages(thread_id)
                state["eval_memory"] = {"restart": True, "seeded": len(history), "restored": len(restored),
                                        "restored_intact": [str(m.content) for m in restored] == [str(m.content) for m in history],
                                        "after": len(after),
                                        "answered_in_thread": bool(after) and str(after[-1].content) == str(state.get("final_answer"))}
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
        obs = state.get("observe") or {}  # the observe step's retry: its passages are evidence the judge must see
        called = [tc["name"] for o in outs.values() for tc in o.get("tool_calls", [])] + [tc["name"] for tc in obs.get("tool_calls", [])]
        evidence = [ctx for o in outs.values() for ctx in o.get("contexts", [])] + list(obs.get("evidence", []))
        if state.get("handoff"):  # so the judge knows the handoff really happened
            evidence.append(f"[handoff system action] a human handoff record and ticket were created: {state['handoff']}")
        row = {
            "id": c["id"], "message": c["message"], "plan": state.get("plan", []),
            "answer": state["final_answer"], "tools_called": called,
            # what the judge saw (tool outputs). Stored so a human can check groundedness against it
            "evidence": evidence,
            "verify_note": state.get("verify_note", ""),
            "verify_raw": state.get("verify_raw", ""),  # the verifier's statements and quotes, eval only
            # the exact calls (agent, tool, arguments), to see what the model really asked for
            "tool_calls": [{"agent": a, "name": tc["name"], "args": tc.get("args", {})}
                           for a, o in outs.items() for tc in o.get("tool_calls", [])]
                          + [{"agent": "observe", "name": tc["name"], "args": tc.get("args", {})} for tc in obs.get("tool_calls", [])],
            "replans": list(obs.get("replans", [])),
            "routing_ok": routing_ok(c, state.get("plan", [])), "tools_ok": tools_ok(c, called),
            "degraded": any(o.get("error") for o in outs.values()), "latency_ms": latency,
        }
        reason = (state.get("handoff") or {}).get("reason")
        row["handoff"] = reason
        row["handoff_ok"] = handoff_ok(c, reason)
        if c.get("history"):
            row["history"] = c["history"]  # which conversation the message was answered in
        mem = state.get("eval_memory")
        if mem:  # deterministic: after the restart the thread holds exactly the earlier turns, and the answer was written to it
            row["memory"] = mem
            row["memory_ok"] = bool(mem["restored_intact"] and mem["restored"] == mem["seeded"] and mem["after"] >= mem["seeded"] + 2
                                    and mem["answered_in_thread"])
        if c.get("holdout"):
            row["holdout"] = True
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
    remembered = [r["memory_ok"] for r in rows if r.get("memory_ok") is not None]
    if remembered:  # multi-turn cases run through the real checkpointer with a restart (eval/persistent.py)
        s["memory_accuracy"] = sum(remembered) / len(remembered)
    judge_errors = sum(1 for r in rows if r.get("judge", {}).get("verdict") == "error")
    if judge_errors:
        s["judge_errors"] = judge_errors  # judge failed (not scored), the case still counts for routing/tools
    if judged:
        for k in SCORE_KEYS:
            s[f"avg_{k}"] = round(statistics.mean(j[k] for j in judged), 2)
        s["judge_pass_rate"] = sum(j["verdict"] == "pass" for j in judged) / len(judged)
        # how many rows the judge scored (every judge-based rate above is over THIS many, not over `cases`): with a partial scoring
        # (eval/rejudge.py stopped by the daily limit) the two differ, and a rate without its n would pass for a full one
        s["judged_cases"] = len(judged)
        s["judge_passes"] = sum(j["verdict"] == "pass" for j in judged)
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
    synth = s.effective_synth_model
    judge_host = ", ".join(s.openrouter_provider_order.get(s.judge_model, [])) if s.judge_provider.lower() == "openrouter" else ""
    return {"agent provider": s.agent_provider, "knowledge": s.effective_knowledge_model,
            "router / synthesizer": s.light_model if synth == s.light_model else f"{s.light_model} / {synth}",
            "support": s.effective_support_model,
            "judge": f"{s.judge_model} ({s.judge_provider}{', ' + judge_host if judge_host else ''})"}


def render_report(rows: list[dict], summary: dict, stamp: str, models: dict | None = None) -> str:
    """Markdown report. Rates and judge scores are shown as percentages."""
    lines = ["# Evaluation report", "", f"_Generated {stamp}_", ""]
    if models:
        lines += ["_Models: " + ", ".join(f"{role} = `{name}`" for role, name in models.items()) + "_", ""]
    lines += ["## Summary", "", f"_Judge scores are shown as a percentage of the maximum ({MAX_SCORE})._", ""]
    if summary.get("provider_errors"):
        lines += [f"**{summary['provider_errors']} case(s) hit a provider error (rate limit / quota / outage) "
                  "and are excluded from every metric below.**", ""]
    if summary.get("holdout"):
        lines += [f"_The summary covers the {summary.get('cases', 0)} regular cases; the "
                  f"{summary['holdout'].get('cases', 0)} holdout cases are summarized apart, below. "
                  "`provider_errors` counts both._", ""]
    for k, v in summary.items():
        if k in ("cases", "tokens_by_model", "holdout", "holdout_groups"):
            continue
        if k.endswith("_cost_usd"):
            lines.append(f"- **{k}**: {_usd(v)}")
        elif k.startswith("avg_"):
            lines.append(f"- **{k}**: {_score_pct(v)}")
        elif k.endswith(("_accuracy", "_rate")):
            lines.append(f"- **{k}**: {v * 100:.0f}%")
        else:
            lines.append(f"- **{k}**: {v}")
    if summary.get("holdout"):
        groups = summary.get("holdout_groups") or {"original": summary["holdout"]}
        for g, part in groups.items():
            lines += ["", f"## Holdout - {HOLDOUT_GROUPS[g]}", "",
                      "_Never used to tune a prompt or a threshold; it only measures._", ""]
            for k, v in part.items():
                if k == "tokens_by_model":
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
        lines.append(f"| {r['id']}{' (holdout, ' + HOLDOUT_MARK[holdout_group(r['id'])] + ')' if r.get('holdout') else ''} | {'OK' if r['routing_ok'] else 'FAIL'} {r.get('plan', '')} | {'OK' if r['tools_ok'] else 'FAIL'} "
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


def run_config(label: str | None = None) -> dict:
    """What the system was configured to do in this run, written into every report and JSON, so a run can never be mistaken for another:
    a run with the observe step on is marked, and the main run says REPLAN_MAX 0 out loud."""
    from app.config import get_settings

    s = get_settings()
    from eval.provenance import repo_state

    git = repo_state()
    cfg = {"commit": git["commit"], "tree_clean": git["tree_clean"],
           "replan_max": s.replan_max, "rag_mode": s.rag_mode, "verifier_mode": s.verifier_mode, "approvals": "off (eval)", "fallback_provider": "off (eval)"}
    if label:
        cfg["label"] = label
    if with_holdout_v3():
        cfg["with_holdout_v3"] = True  # only when set, like `dataset`
    if custom_dataset() is not None:  # only when it is not the default, so the report of a normal run is what it always was
        cfg["dataset"] = custom_dataset().name  # type: ignore[union-attr]
    return cfg


def run_repeats(cases: list[dict], service, judge, repeats: int, pause: float, retries: int = 0,
                models: dict | None = None, update_latest: bool = True, sleep=time.sleep,
                config: dict | None = None) -> tuple[list, Path | None]:
    """Run the cases `repeats` times. Each run gets its own report; only the FIRST run may become latest.md
    (and only if it is full and clean, as always). With more than one run, the spread goes to REPEATS-<stamp>.md.
    -> ([(rows, summary, report path)], repeats report path or None)."""
    from eval import repeats as rep_mod

    done = []
    for i in range(repeats):
        if repeats > 1:
            print(f"\n=== run {i + 1} of {repeats} ===")
        rows = evaluate(cases, service, judge, pause, retries=retries, sleep=sleep)
        summary = {**summarize_all(rows), **(config or {})}
        path = write_report(rows, summary, models, update_latest=update_latest and i == 0)
        done.append((rows, summary, path))
    if repeats < 2:
        return done, None
    agg = rep_mod.aggregate([d[0] for d in done], [d[1] for d in done])
    return done, rep_mod.write(agg, models, sources=[d[2].stem for d in done], config=config)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="case id prefixes to run")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between cases")
    ap.add_argument("--retries", type=int, default=2, help="times to wait out a per-minute rate limit per case")
    ap.add_argument("--repeats", type=int, default=1, help="run the cases N times and write REPEATS-<stamp>.md")
    ap.add_argument("--keep-latest", action="store_true", help="never overwrite eval/reports/latest.md")
    ap.add_argument("--regular-only", action="store_true", help="leave the holdout cases out (they only measure, never tune)")
    ap.add_argument("--replan-max", type=int, default=None,
                    help="set REPLAN_MAX for this run (0 = the observe step off, the default; 2 = the 'replan on' branch)")
    ap.add_argument("--label", default=None, help="mark the run in its reports and JSON, e.g. --label 'replan on'")
    ap.add_argument("--require-clean", action="store_true",
                    help="refuse to run unless the code is committed and the tree is clean: the reports cite the commit they came from")
    ap.add_argument("--dataset", default=None, metavar="PATH",
                    help="run another cases file with the same schema (sets EVAL_DATASET, which eval/rejudge.py also reads); latest.md is never replaced by such a run")
    ap.add_argument("--with-holdout-v3", action="store_true",
                    help="run eval/holdout_v3.jsonl (hv3_*) together with the dataset; its metrics stay in their own holdout group (sets EVAL_WITH_HOLDOUT_V3, which eval/rejudge.py also reads)")
    ap.add_argument("--inject-history", action="store_true",
                    help="the old way: earlier turns of a multi-turn case go in as context (no checkpointer, no restart)")
    args = ap.parse_args()

    import os

    from app.config import get_settings
    from app.graph import build_default_graph
    from app.service import ChatService

    if get_settings().fallback_provider:  # a measurement runs on ONE provider: no silent switch to another model mid-report
        print(f"NOTE: FALLBACK_PROVIDER={get_settings().fallback_provider} is set; the eval turns provider failover off for this run.")
        os.environ["FALLBACK_PROVIDER"] = ""
        get_settings.cache_clear()
    if args.with_holdout_v3:
        os.environ["EVAL_WITH_HOLDOUT_V3"] = "1"
    if args.dataset:
        os.environ["EVAL_DATASET"] = str(Path(args.dataset).resolve())
    if args.require_clean:
        from eval.provenance import require_clean

        ok, message = require_clean()
        print(("Provenance: " if ok else "REFUSING TO RUN: ") + message)
        if not ok:
            raise SystemExit(2)
    if args.replan_max is not None:  # the setting is read when the graph is built, so it is set before that
        os.environ["REPLAN_MAX"] = str(args.replan_max)
        get_settings.cache_clear()
    config = run_config(args.label)
    print("Config:", ", ".join(f"{k}={v}" for k, v in config.items()))
    cases = load_cases(args.only, regular_only=args.regular_only)
    models = current_models()
    print("Models:", ", ".join(f"{role}={name}" for role, name in models.items()))
    if args.inject_history:
        service = ChatService(build_default_graph(keep_verify_raw=True))
    else:
        from eval.persistent import PersistentEvalService

        service = PersistentEvalService(keep_verify_raw=True)  # multi-turn cases: real SQLite checkpointer + a restart
    try:
        done, repeats_path = run_repeats(cases, service, None if args.no_judge else Judge(),
                                         max(1, args.repeats), args.pause, retries=args.retries, models=models,
                                         update_latest=should_update_latest(args.only, args.keep_latest, args.label),
                                         config=config)
    finally:
        if hasattr(service, "close"):
            service.close()
    rows, summary, path = done[-1]
    print("\n=== SUMMARY (last run) ===")
    print(json.dumps(summary, indent=2))
    print("\nReports: " + ", ".join(str(d[2]) for d in done))
    if repeats_path:
        print(f"Repeats: {repeats_path}")
    errored = sorted({r["id"] for d in done for r in d[0] if r.get("provider_error")})
    if errored:
        print(f"\nWARNING: {len(errored)} case(s) hit a provider error (quota / rate limit / outage) and were "
              "left out of the metrics.\n"
              f"Re-run them when the provider recovers: python -m eval.run_eval --only {' '.join(errored)}")


if __name__ == "__main__":
    main()
