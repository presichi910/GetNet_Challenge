"""Aggregate N runs of the same cases: the spread of every metric and how often each case passes.

Used by `python -m eval.run_eval --repeats N`; it can also aggregate saved reports:

    python -m eval.repeats eval/reports/A.json eval/reports/B.json eval/reports/C.json

A case "passes" a run when its routing and tool checks are OK, its handoff check is not a failure and the
judge said pass (or there was no judge). A run where the case hit a provider error does not count for it.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
SKIP = {"cases", "provider_errors", "tokens_by_model", "holdout"}


def case_passes(row: dict) -> bool | None:
    """None if the case did not really run (provider error)."""
    if row.get("provider_error"):
        return None
    if row.get("error"):
        return False
    judge = row.get("judge")
    judged_ok = not judge or judge.get("verdict") in ("pass", "error")
    return bool(row.get("routing_ok")) and bool(row.get("tools_ok")) and row.get("handoff_ok") is not False and judged_ok


def spread(summaries: list[dict]) -> dict[str, dict]:
    """{metric: {mean, min, max, n}} over the numeric metrics the runs have."""
    out = {}
    keys = sorted({k for s in summaries for k, v in s.items() if k not in SKIP and isinstance(v, (int, float))})
    for k in keys:
        vals = [s[k] for s in summaries if isinstance(s.get(k), (int, float))]
        if vals:
            out[k] = {"mean": sum(vals) / len(vals), "min": min(vals), "max": max(vals), "n": len(vals)}
    return out


def aggregate(runs: list[list[dict]], summaries: list[dict]) -> dict:
    """runs: the rows of each run; summaries: summarize_all() of each run."""
    ids = list(dict.fromkeys(r["id"] for rows in runs for r in rows))
    cases = {}
    for cid in ids:
        results = [case_passes(r) for rows in runs for r in rows if r["id"] == cid]
        holdout = any(r.get("holdout") for rows in runs for r in rows if r["id"] == cid)
        ran = [x for x in results if x is not None]
        cases[cid] = {"passed": sum(ran), "ran": len(ran), "provider_errors": len(results) - len(ran), "holdout": holdout}
    unstable = [c for c, v in cases.items() if 0 < v["passed"] < v["ran"]]
    always_fail = [c for c, v in cases.items() if v["ran"] and v["passed"] == 0]
    return {"runs": len(runs), "regular": spread(summaries),
            "holdout": spread([s["holdout"] for s in summaries if s.get("holdout")]),
            "cases": cases, "unstable": unstable, "always_fail": always_fail,
            "provider_errors": sum(s.get("provider_errors", 0) for s in summaries)}


def _fmt(metric: str, v: float) -> str:
    if metric.startswith("avg_"):
        return f"{v / 5 * 100:.0f}%"
    if metric.endswith(("_accuracy", "_rate")):
        return f"{v * 100:.0f}%"
    if metric.endswith("_cost_usd"):
        return f"${v:.4f}"
    return f"{v:.0f}"


def render(agg: dict, stamp: str, models: dict | None = None, sources: list[str] | None = None) -> str:
    lines = [f"# Repeated runs ({agg['runs']} runs)", "", f"_Generated {stamp}_", ""]
    if models:
        lines += ["_Models: " + ", ".join(f"{k} = `{v}`" for k, v in models.items()) + "_", ""]
    if sources:
        lines += ["_Runs: " + ", ".join(f"`{s}`" for s in sources) + "_", ""]
    lines += [f"Provider errors over all runs: {agg['provider_errors']} (those case-runs are left out).", ""]
    for title, key in (("Regular cases", "regular"), ("Holdout cases", "holdout")):
        if not agg[key]:
            continue
        lines += [f"## {title}: spread of each metric", "", "| metric | mean | min | max | runs |", "|---|---|---|---|---|"]
        for m, s in agg[key].items():
            lines.append(f"| {m} | {_fmt(m, s['mean'])} | {_fmt(m, s['min'])} | {_fmt(m, s['max'])} | {s['n']} |")
        lines.append("")
    lines += ["## Cases: how many runs each one passes", "",
              "_Pass = routing OK, tools OK, handoff not failed and judge pass._", "",
              "| case | passed / ran | provider errors |", "|---|---|---|"]
    for cid, v in agg["cases"].items():
        mark = " (holdout)" if v["holdout"] else ""
        lines.append(f"| {cid}{mark} | {v['passed']}/{v['ran']} | {v['provider_errors']} |")
    lines += ["", "**Unstable (pass in some runs, not all):** " + (", ".join(agg["unstable"]) or "none"),
              "", "**Fail in every run:** " + (", ".join(agg["always_fail"]) or "none"), ""]
    return "\n".join(lines)


def write(agg: dict, models: dict | None = None, sources: list[str] | None = None, out_dir: Path | None = None) -> Path:
    out = out_dir or ROOT / "reports"
    out.mkdir(exist_ok=True)
    now = datetime.now()
    path = out / f"REPEATS-{now:%Y%m%d-%H%M%S}.md"
    path.write_text(render(agg, now.strftime("%Y%m%d-%H%M%S"), models, sources), encoding="utf-8")
    return path


def main() -> None:
    from eval.run_eval import summarize_all

    ap = argparse.ArgumentParser()
    ap.add_argument("reports", nargs="+", help="eval report JSON files of the same cases")
    args = ap.parse_args()
    runs = [json.loads(Path(p).read_text(encoding="utf-8"))["rows"] for p in args.reports]
    path = write(aggregate(runs, [summarize_all(r) for r in runs]), sources=[Path(p).name for p in args.reports])
    print(f"Report: {path}")


if __name__ == "__main__":
    main()
