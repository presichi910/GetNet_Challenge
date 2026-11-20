"""Cost guard (plan 9.6, block M2): no run that spends on OpenRouter starts without passing it, and every run leaves its estimate against its real cost.

    python -m eval.cost_guard estimate --cases 27 --repeats 5          # what the guard would say about a run (reads the key's counter and the public prices)
    python -m eval.cost_guard ledger                                   # write eval/reports/COST_LEDGER.md again from eval/reports/cost_ledger.json
    python -m eval.cost_guard note --block B --real 0.04 --text "..."   # a spend that happened before the guard existed or outside a guarded run

THE RULE (it deviates from docs/PLAN_96.md, which said: abort if spent + estimate > 9.0, or one run's estimate > 3.0)::

    abort if  spent + 1.5 x estimate > 8.5     or     1.5 x estimate > 3.0 for one run

* **The truth about the spend is the key's own counter** (`GET /api/v1/key`, `data.usage`), not the sum of the `usage.cost` of the responses. The measurement of 2026-11-19
  showed why: the responses summed to US$0.0211 and the counter moved by about twice that, because a row that hits a provider error records no usage although the graph kept running.
* **The factor 1.5** is that understatement turned into a margin on the ESTIMATE; every guarded run writes its estimate against its real cost to `COST_LEDGER.md`, to calibrate it.
* **The line is 8.5, not 9.0**: the key is limited to US$9, and OpenRouter cutting a run in the middle would leave partial data. If the key reports a limit of its own,
  the line is the lower of 8.5 and that limit minus 0.5. A key with NO limit is reported as a warning: then nothing but this guard stops the spending.
* **Prices are those of the pinned host** (`OPENROUTER_PROVIDER_ORDER`: the agents on Groq, the judge on Alibaba), read from the public `/models/<id>/endpoints`; a model that is not pinned is priced
  at the DEAREST of its endpoints. If the listing cannot be read, the table of app/observability.py is used and the report says so.
* **Tokens per role come from the recent runs** (the newest JSON files of eval/reports that recorded usage). A model with no tokens of its own (a candidate of the model comparison) is estimated from the tokens
  its role used under another model, times an explicit and conservative factor (input x1.25, output x3); a role never seen in a recent run uses a fixed table. Every assumed line is marked in the output and the ledger.
* **The counter lags** (a read right after a run missed the whole run; a later one did not; how long it takes was not measured), so a run's real cost is read once the counter has not moved for three
  reads in a row and at least 45 s have passed; the ledger says whether it settled.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "eval" / "reports"
LEDGER_JSON = REPORTS / "cost_ledger.json"
LEDGER_MD = REPORTS / "COST_LEDGER.md"
BASE_URL = "https://openrouter.ai/api/v1"

SAFETY = 1.5
ABORT_LINE_USD = 8.5
SINGLE_RUN_CAP_USD = 3.0
KEY_LIMIT_USD = 9.0  # the limit the plan puts on the key (the key itself may not have one: see the warning)
LIMIT_HEADROOM_USD = 0.5  # with a limit on the key, the line is limit - this, when that is below 8.5
UNKNOWN_MODEL_INPUT_FACTOR = 1.25
UNKNOWN_MODEL_OUTPUT_FACTOR = 3.0
# (input, output) tokens per case-run of a role that no recent run shows: deliberately above what was measured (router 1.1K/60, knowledge 3K/280, verify 1.7K/640, judge 1.5K/110)
FALLBACK_TOKENS = {"router": (1200, 100), "knowledge": (3500, 500), "support": (3000, 400), "synthesizer": (1500, 250), "verify": (2000, 700),
                   "summarize": (900, 150), "observe": (2500, 500), "judge": (1600, 130)}
MAX_FILES = 20  # how many of the newest run files the token averages come from
SETTLE_INTERVAL_S, SETTLE_TIMEOUT_S, SETTLE_STABLE_READS, SETTLE_MIN_WAIT_S = 15.0, 180.0, 3, 45.0
FIREWORKS_PREFIX = "accounts/fireworks/models/"


class CostGuardAbort(SystemExit):
    """The run must not start. Exit code 3, with the numbers."""

    def __init__(self, message: str):
        super().__init__(f"COST GUARD: {message}")


# ---------------------------------------------------------------------------------------------- reading the key and the prices

def read_key_usage(api_key: str, client: httpx.Client | None = None) -> dict:
    """{"usage": USD spent on the key so far, "limit": USD or None, ...} from GET /api/v1/key: the counter that is the truth about the spend."""
    own = client is None
    client = client or httpx.Client()
    try:
        response = client.get(f"{BASE_URL}/key", headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
        response.raise_for_status()
        data = response.json()["data"]
        return {"usage": float(data["usage"]), "limit": None if data.get("limit") is None else float(data["limit"])}
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        raise CostGuardAbort(f"cannot read the key's usage counter ({type(exc).__name__}): a run is not started without it "
                             "(--no-cost-guard skips the guard, for tests)") from exc
    finally:
        if own:
            client.close()


def fetch_endpoints(model: str) -> list[dict]:
    response = httpx.get(f"{BASE_URL}/models/{model}/endpoints", timeout=30)
    response.raise_for_status()
    return response.json()["data"]["endpoints"]


def normalize_model(name: str) -> str:
    """A Fireworks id is the same model as its OpenRouter id (the runs of 2026-11-15 were made there)."""
    return "openai/" + name[len(FIREWORKS_PREFIX):] if name.startswith(FIREWORKS_PREFIX) else name


def price_of(model: str, pinned: list[str], fetch=None, table: dict | None = None) -> dict:
    """USD per million tokens {in, out, source}: the pinned host's price; the dearest endpoint when the model is not pinned; the table of app/observability.py if the listing is unreadable."""
    try:
        endpoints = (fetch or fetch_endpoints)(model)
        mine = [e for e in endpoints if any(e.get("tag") == slug or str(e.get("tag", "")).startswith(slug + "/") for slug in pinned)] if pinned else []
        pool, source = (mine, f"pinned host {pinned[0]}") if mine else (endpoints, "dearest endpoint (the model is not pinned)")
        if pool:
            best = max(pool, key=lambda e: float((e.get("pricing") or {}).get("prompt") or 0) + float((e.get("pricing") or {}).get("completion") or 0))
            return {"in": float(best["pricing"]["prompt"]) * 1e6, "out": float(best["pricing"]["completion"]) * 1e6, "source": source}
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        pass
    from app.observability import DEFAULT_PRICES

    table = table if table is not None else DEFAULT_PRICES
    for key in ([f"{model}@{pinned[0]}"] if pinned else []) + [model]:
        if key in table:
            inp, out = table[key]
            return {"in": inp, "out": out, "source": f"table of app/observability.py ({key}): the public listing could not be read"}
    raise CostGuardAbort(f"no price for {model}: the public listing is unreadable and the table has none")


# ---------------------------------------------------------------------------------------------- tokens of the recent runs

def recent_tokens(reports_dir: Path | None = None, max_files: int = MAX_FILES) -> dict:
    """Average tokens per case-run of each (node, model) over the newest run files that recorded usage, and per node over any model.

    {"by_node_model": {(node, model): {"in", "out", "rows", "files"}}, "by_node": {node: {"in", "out"}}, "judge": {model: {"in", "out"}}, "files": [names]}
    A (node, model) pair is averaged over the rows of the files in which that model served that node, so a model that served a role in only a few runs is not diluted by the others."""
    reports_dir = reports_dir or REPORTS
    files = [p for p in sorted(reports_dir.glob("2*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if ".judged" not in p.name][:max_files * 3]
    acc: dict[tuple[str, str], dict] = {}
    judge: dict[str, dict] = {}
    used: list[str] = []
    for path in files:  # the agents' tokens come from the newest `max_files` runs with usage; the judge's from the whole window (a judged run is rarer)
        try:
            rows = json.loads(path.read_text(encoding="utf-8")).get("rows", [])
        except (OSError, ValueError):
            continue
        for r in rows:
            for g in (r.get("judge_usage") or {}).get("by_node", []):
                j = judge.setdefault(normalize_model(g["model"]), {"in": 0, "out": 0, "calls": 0})
                j["in"] += g["input_tokens"]
                j["out"] += g["output_tokens"]
                j["calls"] += g["calls"]
        with_usage = [r for r in rows if (r.get("usage") or {}).get("by_node")]
        if not with_usage or len(used) >= max_files:
            continue
        used.append(path.name)
        per_file: dict[tuple[str, str], list[int]] = {}
        for r in with_usage:
            for g in r["usage"]["by_node"]:
                k = (g["node"], normalize_model(g["model"]))
                t = per_file.setdefault(k, [0, 0])
                t[0] += g["input_tokens"]
                t[1] += g["output_tokens"]
        for k, (tin, tout) in per_file.items():
            a = acc.setdefault(k, {"in": 0, "out": 0, "rows": 0, "files": 0})
            a["in"] += tin
            a["out"] += tout
            a["rows"] += len(with_usage)
            a["files"] += 1
    by_node_model = {k: {"in": a["in"] / a["rows"], "out": a["out"] / a["rows"], "rows": a["rows"], "files": a["files"]} for k, a in acc.items()}
    by_node: dict[str, dict] = {}
    for (node, _), a in by_node_model.items():
        n = by_node.setdefault(node, {"in": 0.0, "out": 0.0, "weight": 0})
        n["in"] += a["in"] * a["rows"]
        n["out"] += a["out"] * a["rows"]
        n["weight"] += a["rows"]
    by_node = {node: {"in": n["in"] / n["weight"], "out": n["out"] / n["weight"]} for node, n in by_node.items()}
    return {"by_node_model": by_node_model, "by_node": by_node, "files": used,
            "judge": {m: {"in": j["in"] / j["calls"], "out": j["out"] / j["calls"]} for m, j in judge.items() if j["calls"]}}


# ---------------------------------------------------------------------------------------------- the estimate and the verdict

@dataclass
class Line:
    role: str
    model: str
    host: str
    tokens_in: float
    tokens_out: float
    price_in: float
    price_out: float
    price_source: str
    basis: str  # "observed" or "assumed: ..."
    cost_per_case_run: float

    @property
    def assumed(self) -> bool:
        return self.basis.startswith("assumed")


@dataclass
class Estimate:
    lines: list[Line]
    n_case_runs: int
    total_usd: float
    skipped: list[str] = field(default_factory=list)

    @property
    def assumed_roles(self) -> list[str]:
        return [f"{ln.role} ({ln.model})" for ln in self.lines if ln.assumed]


def role_models(s, include_agents: bool = True, include_judge: bool = True) -> list[tuple[str, str, str]]:
    """(role, model, provider) for the roles a run uses. The router, verifier, summarizer and observe step share LIGHT_MODEL; knowledge, support and synthesizer have their own settings."""
    roles: list[tuple[str, str, str]] = []
    if include_agents:
        agent = s.agent_provider.lower()
        roles += [("router", s.light_model, agent), ("knowledge", s.effective_knowledge_model, agent), ("support", s.effective_support_model, agent),
                  ("synthesizer", s.effective_synth_model, agent), ("verify", s.light_model, agent)]
        if s.replan_max >= 1:
            roles.append(("observe", s.light_model, agent))
    if include_judge:
        roles.append(("judge", s.judge_model, s.judge_provider.lower()))
    return roles


def estimate_run(s, n_case_runs: int, tokens: dict, *, include_agents: bool = True, include_judge: bool = True, fetch=None,
                 table: dict | None = None) -> Estimate:
    """Cost of n case-runs. Only the roles served by OpenRouter count: what goes elsewhere is not on this key."""
    lines: list[Line] = []
    skipped: list[str] = []
    for role, model, provider in role_models(s, include_agents, include_judge):
        model = normalize_model(model)
        if provider != "openrouter":
            skipped.append(f"{role}: served by {provider}, not on the OpenRouter key")
            continue
        if role == "judge":
            seen = tokens["judge"].get(model)
            if seen:
                tin, tout, basis = seen["in"], seen["out"], "observed (judge calls of recent runs)"
            elif tokens["judge"]:
                other = next(iter(tokens["judge"].values()))
                tin, tout, basis = other["in"] * UNKNOWN_MODEL_INPUT_FACTOR, other["out"] * UNKNOWN_MODEL_OUTPUT_FACTOR, "assumed: another judge model's tokens x(1.25, 3)"
            else:
                (tin, tout), basis = FALLBACK_TOKENS["judge"], "assumed: fixed table (no judge call in the recent runs)"
        else:
            seen = tokens["by_node_model"].get((role, model))
            if seen:
                tin, tout, basis = seen["in"], seen["out"], f"observed ({seen['rows']} rows)"
            elif role in tokens["by_node"]:
                other = tokens["by_node"][role]
                tin, tout, basis = other["in"] * UNKNOWN_MODEL_INPUT_FACTOR, other["out"] * UNKNOWN_MODEL_OUTPUT_FACTOR, f"assumed: the tokens of {role} under another model x(1.25, 3)"
            elif role == "observe":
                (tin, tout), basis = FALLBACK_TOKENS[role], "assumed: fixed table (the observe step has no recent run)"
            else:
                skipped.append(f"{role}: no call in the recent runs")
                continue
        pinned = s.openrouter_provider_order.get(model, [])
        price = price_of(model, pinned, fetch, table)
        cost = (tin * price["in"] + tout * price["out"]) / 1e6
        lines.append(Line(role, model, pinned[0] if pinned else "(not pinned)", tin, tout, price["in"], price["out"], price["source"], basis, cost))
    return Estimate(lines, n_case_runs, sum(ln.cost_per_case_run for ln in lines) * n_case_runs, skipped)


@dataclass
class Verdict:
    ok: bool
    reasons: list[str]
    spent: float
    estimate: float
    padded: float  # SAFETY x estimate
    abort_line: float
    warnings: list[str]


def check(estimate_usd: float, spent_usd: float, key_limit: float | None) -> Verdict:
    """THE RULE: abort if spent + 1.5 x estimate > 8.5 (or the key's limit minus 0.5 when that is lower), or if 1.5 x estimate > 3.0."""
    line = ABORT_LINE_USD if key_limit is None else min(ABORT_LINE_USD, key_limit - LIMIT_HEADROOM_USD)
    padded = SAFETY * estimate_usd
    reasons = []
    if spent_usd + padded > line:
        reasons.append(f"spent US${spent_usd:.4f} + {SAFETY} x estimate US${estimate_usd:.4f} = US${spent_usd + padded:.4f} is above the line of US${line:.2f}")
    if padded > SINGLE_RUN_CAP_USD:
        reasons.append(f"{SAFETY} x estimate US${estimate_usd:.4f} = US${padded:.4f} is above US${SINGLE_RUN_CAP_USD:.2f} for one run")
    warnings = []
    if key_limit is None:
        warnings.append(f"the key has NO limit set at OpenRouter: the US${KEY_LIMIT_USD:.0f} cap of the plan is enforced only by this guard (set the limit in the key's settings)")
    return Verdict(not reasons, reasons, spent_usd, estimate_usd, padded, line, warnings)


# ---------------------------------------------------------------------------------------------- the counter after a run

def settle_counter(read, sleep=time.sleep, interval: float = SETTLE_INTERVAL_S, timeout: float = SETTLE_TIMEOUT_S, stable_reads: int = SETTLE_STABLE_READS,
                   min_wait: float = SETTLE_MIN_WAIT_S) -> tuple[float, bool]:
    """The counter lags behind the spending (how long was not measured: a read right after a run missed all of it). Read it until it has not moved for `stable_reads` reads in a row AND
    at least `min_wait` seconds have passed, so a stale reading that has not jumped yet is not taken for the final one. (value, settled); settled False if `timeout` passed first."""
    value = read()
    waited, stable = 0.0, 0
    while waited < timeout:
        sleep(interval)
        waited += interval
        new = read()
        stable = stable + 1 if abs(new - value) < 1e-9 else 0
        value = new
        if waited >= min_wait and stable >= stable_reads - 1:
            return value, True
    return value, False


# ---------------------------------------------------------------------------------------------- the ledger

def load_ledger(path: Path | None = None) -> list[dict]:
    path = path or LEDGER_JSON
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def append_entry(entry: dict, path: Path | None = None, md_path: Path | None = None) -> list[dict]:
    path, md_path = path or LEDGER_JSON, md_path or LEDGER_MD
    entries = load_ledger(path)
    entries.append({**entry, "n": len(entries) + 1})
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_ledger(entries) + "\n", encoding="utf-8")
    return entries


def ratio(entry: dict) -> float | None:
    est, real = entry.get("estimate_usd"), entry.get("real_usd")
    return real / est if est and real is not None and entry.get("settled") else None


def render_ledger(entries: list[dict]) -> str:
    L = ["# Cost ledger (generated)", "",
         "_Written by `eval/cost_guard.py` after every guarded run, and by `python -m eval.cost_guard note` for spending outside one. Nothing below is typed by hand except the `text` of a note._", "",
         "## The rule, and how it differs from the plan", "",
         f"- Abort if `spent + {SAFETY} x estimate > {ABORT_LINE_USD}` or `{SAFETY} x estimate > {SINGLE_RUN_CAP_USD}` for one run. The plan said `spent + estimate > 9.0` or `estimate > 3.0`, with the estimate as it was.",
         "- **Why**: on 2026-11-19 the host measurement reported US$0.0211 from its responses and the key's counter moved about twice that (the rows of a run that hits a provider error record no usage, but the graph kept running). "
         f"So the estimate is padded by {SAFETY}, and the **spent** side is read from the key's counter (`GET /api/v1/key`), never summed from `usage.cost`.",
         f"- **Why 8.5 and not 9.0**: the key is limited to US${KEY_LIMIT_USD:.0f}, and OpenRouter cutting a run in the middle would leave partial data. If the key has a limit of its own, the line is the lower of 8.5 and that limit minus 0.5.",
         "- The counter lags behind the spending, so each run's real cost is read after the counter stopped moving for three reads in a row (at least 45 s) (`settled` below says whether it did; at least 45 s and three equal reads).", ""]
    if not entries:
        return "\n".join(L + ["_No entries yet._"])
    L += ["## Runs", "", "| # | when (UTC) | block | command | estimate | real (counter) | real / estimate | reported by the responses | real / reported | settled | assumed roles |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for e in entries:
        r = ratio(e)
        rep = e.get("reported_cost_usd")
        rr = e["real_usd"] / rep if rep and e.get("real_usd") is not None else None
        L.append(f"| {e['n']} | {e['at_utc']} | {e['block']} | `{(e.get('command') or '-')[:70]}` | {'-' if e.get('estimate_usd') is None else 'US$%.4f' % e['estimate_usd']} "
                 f"| {'-' if e.get('real_usd') is None else 'US$%.4f' % e['real_usd']} | {'-' if r is None else '%.2f' % r} | {'-' if rep is None else 'US$%.4f' % rep} "
                 f"| {'-' if rr is None else '%.2f' % rr} | {'yes' if e.get('settled') else 'no'} | {', '.join(e.get('assumed_roles') or []) or '-'} |")
    notes = [e for e in entries if e.get("text")]
    if notes:
        L += ["", "### Notes", "", *[f"- #{e['n']} ({e['block']}): {e['text']}" for e in notes]]
    by_block: dict[str, float] = {}
    for e in entries:
        by_block[e["block"]] = by_block.get(e["block"], 0.0) + (e.get("real_usd") or 0.0)
    L += ["", "## Spent per block (real, from the counter)", "", "| block | real |", "|---|---|", *[f"| {b} | US${v:.4f} |" for b, v in by_block.items()],
          f"| **total of the entries** | **US${sum(by_block.values()):.4f}** |"]
    ratios = [x for x in (ratio(e) for e in entries) if x is not None]
    L += ["", "## Calibration of the factor", ""]
    if ratios:
        L += [f"- Runs with an estimate and a settled real cost: {len(ratios)}. real / estimate: median {statistics.median(ratios):.2f}, largest {max(ratios):.2f}, smallest {min(ratios):.2f}.",
              f"- The factor in use is {SAFETY}: "
              + ("every run stayed under it." if max(ratios) <= SAFETY else f"**the largest ratio ({max(ratios):.2f}) is above it: the factor is too low for that run.**")]
    else:
        L += ["- No guarded run has a settled estimate yet, so the factor is not calibrated: it is the 2x understatement of 2026-11-19 turned into a margin, not a measured value."]
    return "\n".join(L)


# ---------------------------------------------------------------------------------------------- the guard around a run

@dataclass
class Ticket:
    block: str
    command: str
    estimate: Estimate
    verdict: Verdict
    before: float
    started: float


class CostGuard:
    def __init__(self, settings=None, *, read_key=None, fetch=None, reports_dir: Path | None = None, ledger_path: Path | None = None, md_path: Path | None = None,
                 sleep=time.sleep, clock=time.time, out=print, table: dict | None = None):
        from app.config import get_settings

        self.s = settings or get_settings()
        self.read_key = read_key or (lambda: read_key_usage(self.s.openrouter_api_key))
        self.fetch, self.reports_dir = fetch or fetch_endpoints, reports_dir or REPORTS
        self.ledger_path, self.md_path = ledger_path or LEDGER_JSON, md_path or LEDGER_MD
        self.sleep, self.clock, self.out, self.table = sleep, clock, out, table

    def applicable(self, include_agents: bool, include_judge: bool) -> bool:
        return any(provider == "openrouter" for _, _, provider in role_models(self.s, include_agents, include_judge))

    def start(self, *, block: str, command: str, n_case_runs: int, include_agents: bool = True, include_judge: bool = True) -> Ticket | None:
        if not self.applicable(include_agents, include_judge):
            self.out("COST GUARD: not applicable (no role of this run is served by OpenRouter).")
            return None
        tokens = recent_tokens(self.reports_dir)
        est = estimate_run(self.s, n_case_runs, tokens, include_agents=include_agents, include_judge=include_judge, fetch=self.fetch, table=self.table)
        key = self.read_key()
        verdict = check(est.total_usd, key["usage"], key["limit"])
        self.out(render_estimate(est, verdict, tokens["files"]))
        if not verdict.ok:
            raise CostGuardAbort("; ".join(verdict.reasons) + ". The run did not start.")
        return Ticket(block, command, est, verdict, key["usage"], self.clock())

    def finish(self, ticket: Ticket, *, reported_cost_usd: float | None = None, outcome: str = "completed") -> dict:
        after, settled = settle_counter(lambda: self.read_key()["usage"], self.sleep)
        real = after - ticket.before
        entry = {"at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "block": ticket.block, "command": ticket.command, "outcome": outcome,
                 "n_case_runs": ticket.estimate.n_case_runs, "estimate_usd": ticket.estimate.total_usd, "safety": SAFETY, "spent_before_usd": ticket.before,
                 "counter_after_usd": after, "real_usd": real, "settled": settled, "reported_cost_usd": reported_cost_usd, "assumed_roles": ticket.estimate.assumed_roles}
        append_entry(entry, self.ledger_path, self.md_path)
        self.out(f"COST GUARD: estimate US${ticket.estimate.total_usd:.4f}, real (key counter) US${real:.4f}"
                 + (f", {real / ticket.estimate.total_usd:.2f} of the estimate" if ticket.estimate.total_usd else "")
                 + ("" if settled else " (the counter had not settled: provisional)") + f"; ledger: {self.md_path.name}")
        return entry


def render_estimate(est: Estimate, v: Verdict, files: list[str]) -> str:
    L = [f"COST GUARD: {est.n_case_runs} case-run(s); tokens from {len(files)} recent run file(s).",
         "  role         model                       host        in/out tokens     price in/out per M     source of tokens / price"]
    for ln in est.lines:
        L.append(f"  {ln.role:12s} {ln.model:27s} {ln.host:11s} {ln.tokens_in:6.0f}/{ln.tokens_out:<6.0f}  {ln.price_in:6.3f}/{ln.price_out:<6.3f}  {ln.basis} | {ln.price_source}")
    for text in est.skipped:
        L.append(f"  (left out) {text}")
    if est.assumed_roles:
        L.append(f"  ASSUMED (no tokens of its own, conservative): {', '.join(est.assumed_roles)}")
    L.append(f"  estimate US${v.estimate:.4f}; x{SAFETY} = US${v.padded:.4f}; spent on the key (counter) US${v.spent:.4f}; line US${v.abort_line:.2f}; single-run cap US${SINGLE_RUN_CAP_USD:.2f} -> "
             + ("OK" if v.ok else "ABORT"))
    L += [f"  WARNING: {w}" for w in v.warnings]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("estimate", help="the estimate and the verdict for a run, without starting it")
    e.add_argument("--cases", type=int, required=True, help="number of cases in the run")
    e.add_argument("--repeats", type=int, default=1)
    e.add_argument("--no-judge", action="store_true")
    sub.add_parser("ledger", help="write COST_LEDGER.md again from the JSON ledger")
    n = sub.add_parser("note", help="record a spend that happened outside a guarded run")
    n.add_argument("--block", required=True)
    n.add_argument("--real", type=float, required=True, help="USD, from the key's counter")
    n.add_argument("--text", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "ledger":
        LEDGER_MD.write_text(render_ledger(load_ledger()) + "\n", encoding="utf-8")
        print(f"wrote {LEDGER_MD}")
        return 0
    if args.cmd == "note":
        append_entry({"at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "block": args.block, "command": None, "outcome": "note", "estimate_usd": None,
                      "real_usd": args.real, "settled": True, "reported_cost_usd": None, "assumed_roles": [], "text": args.text})
        print(f"noted in {LEDGER_MD}")
        return 0
    guard = CostGuard()
    try:
        guard.start(block="estimate", command="eval.cost_guard estimate", n_case_runs=args.cases * max(1, args.repeats), include_judge=not args.no_judge)
    except CostGuardAbort as exc:
        print(exc)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
