"""Choose the OpenRouter host of openai/gpt-oss-120b by the rule of `docs/PREREG_PLAN96.md`, amendment 1 (written and committed before this ran).

    python -m eval.host_latency --list                 # the candidates, with why each is in or out (public endpoint, free)
    python -m eval.host_latency                        # measure every candidate (cap US$0.10) and write eval/reports/HOST-LATENCY-<stamp>.{json,md}
    python -m eval.host_latency --decide FILE.json     # apply the rule again to a saved measurement (no calls)
    python -m eval.host_latency --incumbent none       # the unconditional winner (the default is the host pinned in the settings: amendment 1b)

Per candidate host: 10 calls (5 of the router and 5 of the knowledge agent, with the real prompts and tool schemas, on the messages of the regular cases
q01 q03 q06 q09 q10), ONE attempt each (no client retries: a stall is data), then `q01` complete twice through `eval.run_eval`. The rule picks the cheapest host
whose p50 is within 1.5 times the fastest and whose tools work in every call; a tie (costs within 5 %) goes to the faster. With an INCUMBENT (amendment 1b, clarification of 2026-11-19: C1 and C3 must run on the same host) the host
pinned stays if it passes that rule, and another replaces it only if it fails. The key is used only in the
Authorization header: it is never stored, and neither is a request header or a message text.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess  # nosec B404
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "eval" / "reports"
BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "openai/gpt-oss-120b"
DEFAULT_HOST = "deepinfra/bf16"  # what stays when no host meets the rule
CASES = ("q01", "q03", "q06", "q09", "q10")  # regular cases only: the holdout never picks a host
ACCEPTED_QUANTIZATION = {"bf16", "fp16", "unknown"}  # "unknown" = not declared
MIN_UPTIME = 99.0
CALL_TIMEOUT_S = 120.0
TOLERANCE = 1.5  # p50 within this many times the fastest
TIE = 0.05  # costs within 5 % of the cheapest are a tie: the faster wins
CAP_USD = 0.10
FIRST_HOST_ESTIMATE_USD = 0.01
Q01_PLAN, Q01_TOOL = ["knowledge"], "search_getnet_kb"  # what q01 did in every one of the 5 base repetitions
KNOWLEDGE_TOOLS = ("search_getnet_kb", "web_search", "escalate_to_human")


# ---------------------------------------------------------------------------------------------- candidates

def quantization_of(endpoint: dict) -> str:
    return (endpoint.get("quantization") or "unknown").lower()


def eligibility(endpoint: dict) -> tuple[bool, str]:
    """(in, reason): the candidate filter of the rule."""
    quant = quantization_of(endpoint)
    if quant not in ACCEPTED_QUANTIZATION:
        return False, f"quantization {quant}"
    if "tools" not in (endpoint.get("supported_parameters") or []):
        return False, "no tools"
    uptime = endpoint.get("uptime_last_30m")
    if uptime is None:
        return False, "uptime not reported"
    if uptime < MIN_UPTIME:
        return False, f"uptime {uptime:.1f} %"
    return True, "ok"


def listed_price(endpoint: dict) -> float:
    pricing = endpoint.get("pricing") or {}
    return float(pricing.get("prompt") or 0) + float(pricing.get("completion") or 0)


def candidates(endpoints: list[dict]) -> list[dict]:
    """The eligible endpoints, one per tag, cheapest listed price first (so the spending cap cuts the expensive ones)."""
    seen, out = set(), []
    for e in sorted(endpoints, key=listed_price):
        if e.get("tag") in seen or not eligibility(e)[0]:
            continue
        seen.add(e["tag"])
        out.append(e)
    return out


def plan_hosts(endpoints: list[dict], incumbent: str | None = None) -> list[tuple[dict, bool]]:
    """(endpoint, is_reference) in measuring order: the candidates, cheapest first, then (amendment 1a) the previously pinned host, and the incumbent, as REFERENCES when they are not candidates.
    A reference is measured the same way, listed apart, and never enters the decision (an incumbent that is not a candidate that day fails the rule)."""
    cands = candidates(endpoints)
    plan = [(e, False) for e in cands]
    tags = {e["tag"] for e in cands}
    for tag in dict.fromkeys([DEFAULT_HOST, incumbent]):
        if tag and tag not in tags:
            reference = next((e for e in endpoints if e.get("tag") == tag), None)
            if reference is not None:
                plan.append((reference, True))
    return plan


def default_incumbent(model: str = MODEL) -> str | None:
    """The host pinned in the settings for the model (OPENROUTER_PROVIDER_ORDER): the incumbent of amendment 1b."""
    from app.config import get_settings

    order = get_settings().openrouter_provider_order.get(model) or []
    return order[0] if order else None


# ---------------------------------------------------------------------------------------------- requests

def build_bodies(model: str = MODEL) -> list[dict]:
    """The 10 request bodies, without a host: 5 router calls and 5 knowledge calls, from the real prompts and tool schemas and the code's own extra_body."""
    os.environ.update(AGENT_PROVIDER="openrouter", FALLBACK_PROVIDER="")
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from app.agents.common import format_prior_context
    from app.agents.router import RoutingDecision
    from app.config import get_settings
    from app.llm import get_llm
    from app.prompts import KNOWLEDGE_PROMPT, ROUTER_PROMPT
    from app.tools.handoff_tools import escalate_to_human
    from app.tools.knowledge_tools import search_getnet_kb, web_search

    get_settings.cache_clear()
    llm = get_llm(model=model)
    router_tool = convert_to_openai_tool(RoutingDecision)
    knowledge_tools = [convert_to_openai_tool(t) for t in (search_getnet_kb, web_search, escalate_to_human)]
    messages = {}
    for line in (ROOT / "eval" / "dataset.jsonl").read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        for prefix in CASES:
            if case["id"].startswith(prefix + "_"):
                messages[prefix] = case["message"]
    assert set(messages) == set(CASES), f"missing cases: {set(CASES) - set(messages)}"

    def body_of(msgs, **kw) -> dict:
        payload = llm._get_request_payload(msgs, **kw)
        payload.update(payload.pop("extra_body", None) or {})  # the OpenAI client merges extra_body into the body; do the same
        return payload

    bodies = []
    for prefix in CASES:
        bodies.append({"kind": "router", "case": prefix, "body": body_of(
            [SystemMessage(content=ROUTER_PROMPT), HumanMessage(content=messages[prefix])], tools=[router_tool],
            tool_choice={"type": "function", "function": {"name": router_tool["function"]["name"]}})})
    system = KNOWLEDGE_PROMPT.format(language="English", prior_context=format_prior_context({}))
    for prefix in CASES:
        bodies.append({"kind": "knowledge", "case": prefix, "body": body_of(
            [SystemMessage(content=system), HumanMessage(content=messages[prefix])], tools=knowledge_tools, tool_choice="auto")})
    return bodies


def pin(body: dict, tag: str) -> dict:
    """The body with the host pinned and no fallback to another one."""
    return {**body, "provider": {**(body.get("provider") or {}), "order": [tag], "allow_fallbacks": False}}


def same_host(answered: object, expected: object) -> bool:
    """The response's `provider` against the endpoint's provider name, ignoring case, spaces and punctuation ("Amazon Bedrock", "amazon-bedrock")."""
    def norm(x: object) -> str:
        return "".join(ch for ch in str(x or "").lower() if ch.isalnum())

    a, e = norm(answered), norm(expected)
    return bool(a) and bool(e) and (a == e or a in e or e in a)


def call_ok(kind: str, tool_name: str | None, arguments: str | None) -> bool:
    """Did the call do what the agent needs from it: the router a RoutingDecision, the knowledge agent a call to one of its tools, with parseable arguments."""
    if not tool_name or arguments is None:
        return False
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return False
    if kind == "router":
        from app.agents.router import RoutingDecision

        try:
            RoutingDecision.model_validate(parsed)
        except ValueError:
            return False
        return tool_name == "RoutingDecision"
    return tool_name in KNOWLEDGE_TOOLS and isinstance(parsed, dict)


def measure_call(client: httpx.Client, key: str, spec: dict, endpoint: dict) -> dict:
    """ONE attempt. Records latency, the host that answered, whether the tool call is usable and the real cost. Never the key, a header or a text."""
    body = pin(spec["body"], endpoint["tag"])
    result = {"kind": spec["kind"], "case": spec["case"], "status": None, "latency_ms": None, "host": None, "host_ok": False,
              "finish_reason": None, "tool": None, "tool_ok": False, "completion_tokens": None, "cost_usd": None, "error": None}
    started = time.perf_counter()
    try:
        response = client.post(f"{BASE_URL}/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"}, timeout=CALL_TIMEOUT_S)
        result["latency_ms"] = round((time.perf_counter() - started) * 1000)
        result["status"] = response.status_code
        data = response.json()
    except httpx.TimeoutException:
        result.update(latency_ms=round((time.perf_counter() - started) * 1000), error="timeout")
        return result
    except (httpx.HTTPError, ValueError) as exc:
        result.update(latency_ms=round((time.perf_counter() - started) * 1000), error=type(exc).__name__)
        return result
    if result["status"] != 200:
        result["error"] = str((data.get("error") or {}).get("message", data))[:160] if isinstance(data, dict) else "bad body"
        return result
    choice = data["choices"][0]
    calls = (choice.get("message") or {}).get("tool_calls") or []
    first = (calls[0].get("function") or {}) if calls else {}
    usage = data.get("usage") or {}
    result.update(host=data.get("provider"), finish_reason=choice.get("finish_reason"), tool=first.get("name"),
                  completion_tokens=usage.get("completion_tokens"), cost_usd=usage.get("cost"))
    result["host_ok"] = same_host(data.get("provider"), endpoint.get("provider_name"))
    result["tool_ok"] = call_ok(spec["kind"], first.get("name"), first.get("arguments"))
    return result


# ---------------------------------------------------------------------------------------------- q01 through the real graph

def run_q01(endpoint: dict) -> dict:
    """`q01` complete, twice, through eval.run_eval with the host pinned. Reads what the rows recorded, including the host that answered."""
    label = f"host {endpoint['tag']}"
    before = set(REPORTS.glob("2*.json"))
    env = dict(os.environ, AGENT_PROVIDER="openrouter", FALLBACK_PROVIDER="", PYTHONUTF8="1",
               OPENROUTER_PROVIDER_ORDER=json.dumps({MODEL: [endpoint["tag"]], "qwen/qwen3.8-27b": ["alibaba"]}))
    cmd = [sys.executable, "-X", "utf8", "-m", "eval.run_eval", "--regular-only", "--no-judge", "--only", "q01", "--repeats", "2",
           "--label", label, "--keep-latest", "--require-clean", "--no-cost-guard"]  # this harness has its own cap
    done = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)  # nosec B603
    out: dict = {"ran": done.returncode == 0, "latencies_ms": [], "cost_usd": 0.0, "hosts": [], "ok": False, "files": [], "error": None}
    if done.returncode != 0:
        out["error"] = (done.stderr or done.stdout)[-300:]
        return out
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(set(REPORTS.glob("2*.json")) - before)]
    runs = [r for r in runs if r.get("summary", {}).get("label") == label]
    rows = [next(x for x in r["rows"] if x["id"].startswith("q01")) for r in runs]
    out["files"] = sorted(p.name for p in set(REPORTS.glob("2*.json")) - before)
    out["latencies_ms"] = [r.get("latency_ms") for r in rows]
    out["cost_usd"] = sum((r.get("usage") or {}).get("cost_usd") or 0 for r in rows)
    out["hosts"] = sorted({g["host"] for r in rows for g in (r.get("usage") or {}).get("by_node", []) if g.get("host")})
    # a case that hit a provider error has no usage and no plan: the host failed, and the message says how
    out["errors"] = [str(r["provider_error"])[:220] for r in rows if r.get("provider_error")]
    out["ok"] = (len(rows) == 2 and all(r.get("plan") == Q01_PLAN and Q01_TOOL in (r.get("tools_called") or []) and not r.get("degraded") and r.get("usage") for r in rows)
                 and not any(r.get("summary", {}).get("provider_errors") for r in runs) and not out["errors"]
                 and all(same_host(h, endpoint["provider_name"]) for h in out["hosts"]) and bool(out["hosts"]))
    return out


# ---------------------------------------------------------------------------------------------- the rule

def p50(values: list[float]) -> float:
    return float(statistics.median(values))


def summarize_host(endpoint: dict, calls: list[dict], q01: dict, reference: bool = False) -> dict:
    latencies = [c["latency_ms"] for c in calls if c["latency_ms"] is not None]
    costs = [c["cost_usd"] for c in calls if c["cost_usd"] is not None]
    return {
        "tag": endpoint["tag"], "provider_name": endpoint["provider_name"], "quantization": quantization_of(endpoint),
        "reference": reference, "listed_per_mtok": [float(endpoint["pricing"]["prompt"]) * 1e6, float(endpoint["pricing"]["completion"]) * 1e6],
        "uptime_last_30m": endpoint.get("uptime_last_30m"), "measured": True, "calls": len(calls),
        "calls_tool_ok": sum(1 for c in calls if c["tool_ok"]), "calls_host_ok": sum(1 for c in calls if c["host_ok"]),
        "calls_error": sum(1 for c in calls if c["error"]),
        "p50_ms": p50(latencies) if latencies else None, "max_ms": max(latencies) if latencies else None,
        "mean_cost_usd": statistics.mean(costs) if costs else None,
        "q01": q01, "tools_work": bool(calls) and len(calls) == 10 and all(c["tool_ok"] and c["host_ok"] and not c["error"] for c in calls) and bool(q01.get("ok")),
        "spent_usd": sum(costs) + (q01.get("cost_usd") or 0),
    }


def decide(hosts: list[dict], incumbent: str | None = None) -> dict:
    """The rule. Pool = the measured candidates (never the reference) whose tools work in every call. The fastest is the lowest p50 in the pool. Among the pool hosts within 1.5 times the fastest, the cheapest wins;
    costs within 5 % of the cheapest are a tie and the faster p50 wins. No host in the pool = the default stays.

    With an `incumbent` (amendment 1b): it STAYS if it passes the rule, that is, it is in the pool and its p50 is within 1.5 times the fastest of the pool, even when another host is cheaper or faster.
    If it fails, the host is the rule's choice among the others; if none passes, the incumbent stays."""
    if incumbent:
        unconditional = decide(hosts)
        pool = [h for h in hosts if h.get("measured") and not h.get("reference") and h["tools_work"] and h["p50_ms"] is not None and h["mean_cost_usd"] is not None]
        mine = next((h for h in pool if h["tag"] == incumbent), None)
        fastest = min(pool, key=lambda h: h["p50_ms"]) if pool else None
        if mine is not None and fastest is not None and mine["p50_ms"] <= TOLERANCE * fastest["p50_ms"]:
            return {**unconditional, "winner": incumbent, "kept": None, "incumbent": incumbent, "incumbent_passes": True, "unconditional_winner": unconditional["winner"],
                    "reason": f"the incumbent {incumbent} passes the rule (tools work in every call, p50 {mine['p50_ms']:.0f} ms is within {TOLERANCE}x of the fastest, "
                              f"{fastest['tag']} at {fastest['p50_ms']:.0f} ms): it stays; the unconditional winner would have been {unconditional['winner']}"}
        known = next((h for h in hosts if h["tag"] == incumbent), None)
        if known is None or not known.get("measured"):
            why = "it was not measured (not a candidate that day, or beyond the cap)"
        elif known.get("reference"):
            why = "it is not a candidate that day (quantization, tools or uptime of the filter)"
        elif not known["tools_work"]:
            why = "its tools do not work in every call"
        else:
            why = f"its p50 ({known['p50_ms']:.0f} ms) is above {TOLERANCE}x the fastest ({fastest['tag'] if fastest else '-'})"
        rest = decide([h for h in hosts if h["tag"] != incumbent])
        base = {"incumbent": incumbent, "incumbent_passes": False, "unconditional_winner": unconditional["winner"]}
        if rest["winner"] is None:
            return {**rest, **base, "winner": None, "kept": incumbent,
                    "reason": f"the incumbent {incumbent} fails the rule ({why}) and no other host passes: it stays"}
        return {**rest, **base, "reason": f"the incumbent {incumbent} fails the rule ({why}); {rest['reason']}"}
    pool = [h for h in hosts if h.get("measured") and not h.get("reference") and h["tools_work"] and h["p50_ms"] is not None and h["mean_cost_usd"] is not None]
    if not pool:
        return {"winner": None, "kept": DEFAULT_HOST, "fastest": None, "reason": "no measured host has tools working in every call: the pinned default stays",
                "within": [], "tied": []}
    fastest = min(pool, key=lambda h: h["p50_ms"])
    within = [h for h in pool if h["p50_ms"] <= TOLERANCE * fastest["p50_ms"]]
    cheapest = min(h["mean_cost_usd"] for h in within)
    tied = [h for h in within if h["mean_cost_usd"] <= cheapest * (1 + TIE)]
    winner = min(tied, key=lambda h: h["p50_ms"])
    return {"winner": winner["tag"], "kept": None, "fastest": fastest["tag"],
            "reason": f"the cheapest of the {len(within)} host(s) within {TOLERANCE}x of the fastest ({fastest['tag']}, p50 {fastest['p50_ms']:.0f} ms); "
                      f"{len(tied)} within {TIE:.0%} of the lowest cost, the faster of them",
            "within": [h["tag"] for h in within], "tied": [h["tag"] for h in tied]}


def can_afford(spent: float, per_host_estimate: float, cap: float = CAP_USD) -> bool:
    return spent + per_host_estimate <= cap


# ---------------------------------------------------------------------------------------------- report

def render(report: dict) -> str:
    cap = report["cap_usd"]
    L = [f"# Host latency of `{MODEL}` (generated)", "",
         f"_Generated by `python -m eval.host_latency` on {report['at_utc']} UTC, code at commit `{report['commit'][:12]}` (tree clean: {report['tree_clean']}). The rule is amendment 1 of "
         "`docs/PREREG_PLAN96.md`, committed before this ran. Nothing below is typed by hand"
         + (", except the Notes at the end: written by hand after the run and kept in the JSON._" if report.get("notes") else "._"), "",
         f"- Real cost of the whole measurement: **US${report['spent_usd']:.4f}** (cap US${cap:.2f})"
         + (f", of which US${report['prior_spend_usd']:.6f} is an earlier attempt of the same measurement that crashed before saving anything (counted, not repeated)." if report.get("prior_spend_usd") else "."),
         f"- Candidates: {len(report['candidates'])} of {len(report['endpoints'])} endpoints listed; measured: {sum(1 for h in report['hosts'] if h['measured'])}.", ""]
    d = report["decision"]
    L += ["## Decision", "", f"**Winner: `{d['winner']}`**" if d["winner"] else f"**No winner: `{d['kept']}` stays.**", "", f"_{d['reason']}._", ""]
    if d.get("incumbent"):
        L += [f"Incumbent (amendment 1b): `{d['incumbent']}`, **{'passes the rule and stays' if d['incumbent_passes'] else 'fails the rule'}**. "
              f"The unconditional winner of the rule would have been `{d.get('unconditional_winner')}`.", ""]
    L += ["## Measured hosts (candidates cheapest listed price first, then the reference)", "",
          "_A host marked (reference) is the previously pinned one, measured only to compare: it is not a candidate, never sets the bar and cannot win (amendment 1a)._", "",
          "| host | quant | listed in/out per M | uptime | tool ok /10 | host ok /10 | errors | p50 ms | max ms | mean cost per call | q01 x2 ms | q01 ok | tools work | within 1.5x | tied |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for h in report["hosts"]:
        if not h["measured"]:
            continue
        q = h["q01"]
        cost = "-" if h["mean_cost_usd"] is None else "US$%.6f" % h["mean_cost_usd"]
        L.append(f"| `{h['tag']}`{' (reference)' if h.get('reference') else ''} | {h['quantization']} | {h['listed_per_mtok'][0]:.3f} / {h['listed_per_mtok'][1]:.3f} | {h['uptime_last_30m']:.1f} | {h['calls_tool_ok']} | {h['calls_host_ok']} "
                 f"| {h['calls_error']} | {h['p50_ms'] if h['p50_ms'] is not None else '-'} | {h['max_ms'] if h['max_ms'] is not None else '-'} "
                 f"| {cost} | {', '.join(str(x) for x in q.get('latencies_ms', [])) or '-'} | {'yes' if q.get('ok') else 'no'} "
                 f"| {'yes' if h['tools_work'] else 'no'} | {'yes' if h['tag'] in d['within'] else 'no'} | {'yes' if h['tag'] in d['tied'] else 'no'} |")
    failures = [(h["tag"], h["q01"].get("errors") or [], [c["error"] for c in h.get("calls_detail", []) if c.get("error")]) for h in report["hosts"] if h["measured"]]
    failures = [f for f in failures if f[1] or f[2]]
    if failures:
        L += ["", "## Errors a host returned", "", "| host | q01 runs | direct calls |", "|---|---|---|",
              *[f"| `{tag}` | {'; '.join(q).replace('|', '/') or '-'} | {'; '.join(sorted(set(c))).replace('|', '/') or '-'} |" for tag, q, c in failures]]
    skipped = [h for h in report["hosts"] if not h["measured"]]
    if skipped:
        L += ["", "## Candidates not measured", "", "| host | why |", "|---|---|", *[f"| `{h['tag']}` | {h['why_not_measured']} |" for h in skipped]]
    L += ["", "## Every endpoint listed, and why it is in or out of the candidate list", "", "| host | quant | uptime | tools | in | reason |", "|---|---|---|---|---|---|"]
    for e in report["endpoints"]:
        L.append(f"| `{e['tag']}` | {e['quantization']} | {e['uptime']} | {e['tools']} | {'yes' if e['eligible'] else 'no'} | {e['reason']} |")
    L += ["", "## What it does and does not say", "",
          "- 10 calls per host, one attempt each, no client retries, the real router and knowledge prompts and tool schemas, regular cases only; `q01` complete twice through `eval.run_eval`.",
          "- A p50 over 10 calls taken within a few minutes is a snapshot of a service whose load moves: it ranks the hosts today, it does not promise their latency tomorrow.",
          "- The host that answered is read from each response (`provider`) and from the eval rows; a call answered by another host counts against the candidate.", ""]
    if report.get("notes"):
        L += ["## Notes (written by hand after the run)", "", *[f"- {note}" for note in report["notes"]], ""]
    return "\n".join(L)


def endpoint_rows(endpoints: list[dict]) -> list[dict]:
    rows = []
    for e in endpoints:
        ok, why = eligibility(e)
        rows.append({"tag": e.get("tag"), "quantization": quantization_of(e), "uptime": e.get("uptime_last_30m"), "tools": "tools" in (e.get("supported_parameters") or []),
                     "eligible": ok, "reason": why})
    return rows


def fetch_endpoints() -> list[dict]:
    response = httpx.get(f"{BASE_URL}/models/{MODEL}/endpoints", timeout=30)
    response.raise_for_status()
    return response.json()["data"]["endpoints"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="list the candidates and stop (public endpoint, no key, no cost)")
    parser.add_argument("--decide", metavar="FILE", help="apply the rule to a saved measurement (no calls)")
    parser.add_argument("--incumbent", metavar="TAG", default=None,
                        help="the host that stays if it passes the rule (amendment 1b); default: the host pinned in the settings; 'none' = the unconditional winner")
    parser.add_argument("--rerender", metavar="FILE", help="write the .md of a saved measurement again from its JSON (no calls); use it after editing `notes`")
    parser.add_argument("--cap", type=float, default=CAP_USD, help="spending cap in USD (default 0.10)")
    parser.add_argument("--prior-spend", type=float, default=0.0, metavar="USD",
                        help="real cost already spent by an earlier attempt of this measurement that crashed (it counts against the cap and is stated in the report)")
    args = parser.parse_args(argv)

    if args.rerender:
        path = Path(args.rerender)
        path.with_suffix(".md").write_text(render(json.loads(path.read_text(encoding="utf-8"))) + "\n", encoding="utf-8")
        print(f"wrote {path.with_suffix('.md')}")
        return 0
    incumbent = None if (args.incumbent or "").lower() == "none" else (args.incumbent or default_incumbent())
    if args.decide:
        report = json.loads(Path(args.decide).read_text(encoding="utf-8"))
        report["decision"] = decide(report["hosts"], incumbent)
        print(json.dumps(report["decision"], indent=2))
        return 0
    endpoints = fetch_endpoints()
    cands = candidates(endpoints)
    plan = plan_hosts(endpoints, incumbent)
    if args.list:
        for row in endpoint_rows(endpoints):
            print(f"{'IN ' if row['eligible'] else 'out'} {row['tag']:34s} quant={row['quantization']:8s} uptime={row['uptime']} tools={row['tools']} ({row['reason']})")
        print(f"\n{len(cands)} candidate(s), cheapest listed first: {[c['tag'] for c in cands]}")
        print(f"reference (not a candidate, outside the rule): {[e['tag'] for e, ref in plan if ref]}")
        return 0

    from app.config import get_settings
    from eval.provenance import repo_state

    key = get_settings().openrouter_api_key
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is empty: nothing was sent.")
    state = repo_state()
    if state["tree_clean"] is not True:
        raise SystemExit(f"the working tree is not clean ({state['dirty_files'][:5]}): commit first, so the run can cite its commit")
    bodies = build_bodies()
    hosts, spent, per_host = [], args.prior_spend, FIRST_HOST_ESTIMATE_USD
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    def save() -> None:  # after every host: a crash must not lose what was measured
        report = {"at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "commit": state["commit"], "tree_clean": state["tree_clean"], "cap_usd": args.cap,
                  "model": MODEL, "rule": "docs/PREREG_PLAN96.md, Enmienda 1", "spent_usd": spent, "prior_spend_usd": args.prior_spend, "endpoints": endpoint_rows(endpoints),
                  "candidates": [c["tag"] for c in cands], "reference": [e["tag"] for e, ref in plan if ref], "hosts": hosts, "incumbent": incumbent, "decision": decide(hosts, incumbent)}
        (REPORTS / f"HOST-LATENCY-{stamp}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (REPORTS / f"HOST-LATENCY-{stamp}.md").write_text(render(report) + "\n", encoding="utf-8")
    with httpx.Client() as client:
        for endpoint, is_reference in plan:
            if not can_afford(spent, per_host, args.cap):
                hosts.append({"tag": endpoint["tag"], "measured": False, "reference": is_reference, "why_not_measured": f"the next host could exceed the US${args.cap:.2f} cap (spent US${spent:.4f})"})
                continue
            print(f"measuring {endpoint['tag']} ...", flush=True)
            calls = [measure_call(client, key, spec, endpoint) for spec in bodies]
            q01 = run_q01(endpoint)
            host = summarize_host(endpoint, calls, q01, is_reference)
            host["calls_detail"] = calls
            spent += host["spent_usd"]
            hosts.append(host)
            per_host = max(h["spent_usd"] for h in hosts if h.get("measured"))  # the dearest host so far is the estimate for the next one
            print(f"   p50 {host['p50_ms']} ms, tools work: {host['tools_work']}, cost so far US${spent:.4f}", flush=True)
            save()
    save()
    decision = decide(hosts, incumbent)
    print(f"\nwrote eval/reports/HOST-LATENCY-{stamp}.json and .md: total real cost US${spent:.4f}; decision: {decision['winner'] or decision['kept']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
