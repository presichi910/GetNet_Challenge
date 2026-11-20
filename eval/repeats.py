"""Aggregate N runs of the same cases: the spread of every metric and how often each case passes.

Used by `python -m eval.run_eval --repeats N`; it can also aggregate saved reports:

    python -m eval.repeats eval/reports/A.json eval/reports/B.json eval/reports/C.json

A case "passes" a run when its routing and tool checks are OK, its handoff check is not a failure and the
judge said pass (or there was no judge). A run where the case hit a provider error does not count for it, and
neither does a run whose checks passed but whose JUDGE failed (verdict "error"): that says nothing about the system,
so it is counted apart (`judge_errors`), never as a pass and never as a fail.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
SKIP = {"cases", "provider_errors", "tokens_by_model", "holdout", "holdout_groups", "judge_passes", "replan_max"}
JUDGE_METRICS = ("judge_pass_rate", "avg_relevance", "avg_groundedness", "avg_completeness", "avg_tone")  # need the judge: have an n


def judge_failed(row: dict) -> bool:
    """The judge itself failed on this row (not a verdict on the system)."""
    return not row.get("provider_error") and (row.get("judge") or {}).get("verdict") == "error"


def case_passes(row: dict, judging: bool = False) -> bool | None:
    """True / False, or None if the run says nothing about the case: a provider error, or a judge that failed
    while the routing, tool and handoff checks passed. A failed check is a fail even if the judge also failed.
    `judging`: the run is one that the judge is scoring (some rows have a score): a row it has NOT scored yet is pending, not a pass."""
    if row.get("provider_error"):
        return None
    if row.get("error"):
        return False
    if not (bool(row.get("routing_ok")) and bool(row.get("tools_ok")) and row.get("handoff_ok") is not False):
        return False
    judge = row.get("judge")
    if judge and judge.get("verdict") == "error":
        return None
    if judging and not judge:
        return None  # not scored yet (a partial scoring): it says nothing about the case, so it is not counted as a pass
    return not judge or judge.get("verdict") == "pass"


def spread(summaries: list[dict]) -> dict[str, dict]:
    """{metric: {mean, min, max, n}} over the numeric metrics the runs have. A metric that needs the judge also carries `judged`:
    how many rows the judge scored in each run (and `pooled`, the same rate over all the scored rows together for the pass rate)."""
    out = {}
    keys = sorted({k for s in summaries for k, v in s.items() if k not in SKIP and isinstance(v, (int, float))})
    for k in keys:
        have = [s for s in summaries if isinstance(s.get(k), (int, float))]
        vals = [s[k] for s in have]
        if vals:
            out[k] = {"mean": sum(vals) / len(vals), "min": min(vals), "max": max(vals), "n": len(vals)}
            if k in JUDGE_METRICS:
                out[k]["judged"] = [s.get("judged_cases", 0) for s in have]
                if k == "judge_pass_rate":
                    total = sum(out[k]["judged"])
                    out[k]["pooled"] = (sum(s.get("judge_passes", 0) for s in have) / total) if total else None
    return out


def aggregate(runs: list[list[dict]], summaries: list[dict]) -> dict:
    """runs: the rows of each run; summaries: summarize_all() of each run."""
    ids = list(dict.fromkeys(r["id"] for rows in runs for r in rows))
    judging = [any(r.get("judge") for r in rows) for rows in runs]  # a run that has no score at all is a --no-judge run: checks only
    cases = {}
    for cid in ids:
        mine = [(r, j) for rows, j in zip(runs, judging) for r in rows if r["id"] == cid]
        results = [case_passes(r, j) for r, j in mine]
        holdout = any(r.get("holdout") for r, _ in mine)
        ran = [x for x in results if x is not None]
        cases[cid] = {"passed": sum(ran), "ran": len(ran), "provider_errors": sum(1 for r, _ in mine if r.get("provider_error")),
                      "judge_errors": sum(1 for r, _ in mine if judge_failed(r)), "holdout": holdout,
                      "pending": sum(1 for r, j in mine if j and not r.get("judge") and not r.get("provider_error")
                                     and case_passes(r, False) is True)}
    unstable = [c for c, v in cases.items() if 0 < v["passed"] < v["ran"]]
    always_fail = [c for c, v in cases.items() if v["ran"] and v["passed"] == 0]
    return {"runs": len(runs), "regular": spread(summaries),
            "holdout": spread([s["holdout"] for s in summaries if s.get("holdout")]),
            # the original holdout (seen during development) and the new one (key name kept: saved summaries use it), never averaged together
            "holdout_original": spread([s["holdout_groups"]["original"] for s in summaries
                                        if s.get("holdout_groups", {}).get("original")]),
            "holdout_blind": spread([s["holdout_groups"]["blind"] for s in summaries
                                     if s.get("holdout_groups", {}).get("blind")]),
            "holdout_v3": spread([s["holdout_groups"]["v3"] for s in summaries
                                  if s.get("holdout_groups", {}).get("v3")]),
            "cases": cases, "unstable": unstable, "always_fail": always_fail,
            "provider_errors": sum(s.get("provider_errors", 0) for s in summaries),
            "judge_errors": sum(v["judge_errors"] for v in cases.values()),
            "pending": sum(v["pending"] for v in cases.values()),
            "judged_per_run": [sum(1 for r in rows if (r.get("judge") or {}).get("verdict") not in (None, "error")) for rows in runs],
            "rows_per_run": [sum(1 for r in rows if not r.get("provider_error")) for rows in runs]}


def _fmt(metric: str, v: float) -> str:
    if metric.startswith("avg_"):
        return f"{v / 5 * 100:.0f}%"
    if metric.endswith(("_accuracy", "_rate")):
        return f"{v * 100:.0f}%"
    if metric.endswith("_cost_usd"):
        return f"${v:.4f}"
    return f"{v:.0f}"


def render(agg: dict, stamp: str, models: dict | None = None, sources: list[str] | None = None, config: dict | None = None) -> str:
    lines = [f"# Repeated runs ({agg['runs']} runs)", "", f"_Generated {stamp}_", ""]
    if config:  # what the system was set to do in these runs (REPLAN_MAX, a label such as "replan on"...)
        lines += ["_Configuration: " + ", ".join(f"{k}={v}" for k, v in config.items()) + "_", ""]
    if models:
        lines += ["_Models: " + ", ".join(f"{k} = `{v}`" for k, v in models.items()) + "_", ""]
    if sources:
        lines += ["_Runs: " + ", ".join(f"`{s}`" for s in sources) + "_", ""]
    if any(agg.get("judged_per_run", [])):
        lines += ["**Scored by the judge, per run: " + ", ".join(f"{j} of {n}" for j, n in zip(agg["judged_per_run"], agg["rows_per_run"]))
                  + ".** Every rate that needs the judge is over the rows it scored (its `judged` column), not over all the rows; "
                  f"{agg.get('pending', 0)} case-run(s) are still pending and are neither a pass nor a fail.", ""]
    lines += [f"Provider errors over all runs: {agg['provider_errors']} (those case-runs are left out).",
              f"Judge errors over all runs: {agg.get('judge_errors', 0)} (a case-run whose checks passed but whose judge failed "
              "is left out of passed / ran: not a pass, not a fail).", ""]
    sections = [("Regular cases", "regular"),
                ("Holdout - original (ho01-ho05), seen during development", "holdout_original"),
                ("Holdout - new (ho06 and later; ho06, ho08 and ho12 are baseline seen)", "holdout_blind"),
                ("Holdout v3 (hv3_01-hv3_20)", "holdout_v3")]
    if not (agg.get("holdout_original") or agg.get("holdout_blind") or agg.get("holdout_v3")):
        sections.append(("Holdout cases", "holdout"))  # summaries without groups
    for title, key in sections:
        if not agg.get(key):
            continue
        lines += [f"## {title}: spread of each metric", "",
                  "| metric | mean | min | max | runs | judged per run (n) |", "|---|---|---|---|---|---|"]
        for m, s in agg[key].items():
            n = ", ".join(str(x) for x in s["judged"]) if "judged" in s else "-"
            if s.get("pooled") is not None:
                n += f" (pooled {s['pooled'] * 100:.0f}% of {sum(s['judged'])})"
            lines.append(f"| {m} | {_fmt(m, s['mean'])} | {_fmt(m, s['min'])} | {_fmt(m, s['max'])} | {s['n']} | {n} |")
        lines.append("")
    lines += ["## Cases: how many runs each one passes", "",
              "_Pass = routing OK, tools OK, handoff not failed and judge pass._", "",
              "| case | passed / ran | provider errors | not scored yet |", "|---|---|---|---|"]
    for cid, v in agg["cases"].items():
        from eval.run_eval import HOLDOUT_MARK, holdout_group  # lazy: run_eval imports this module only inside run_repeats

        mark = f" (holdout, {HOLDOUT_MARK[holdout_group(cid)]})" if v["holdout"] else ""
        lines.append(f"| {cid}{mark} | {v['passed']}/{v['ran']} | {v['provider_errors']} | {v.get('pending', 0)} |")
    lines += ["", "**Unstable (pass in some runs, not all):** " + (", ".join(agg["unstable"]) or "none"),
              "", "**Fail in every run:** " + (", ".join(agg["always_fail"]) or "none"), ""]
    return "\n".join(lines)


def write(agg: dict, models: dict | None = None, sources: list[str] | None = None, out_dir: Path | None = None,
          config: dict | None = None) -> Path:
    out = out_dir or ROOT / "reports"
    out.mkdir(exist_ok=True)
    now = datetime.now()
    path = out / f"REPEATS-{now:%Y%m%d-%H%M%S}.md"
    path.write_text(render(agg, now.strftime("%Y%m%d-%H%M%S"), models, sources, config), encoding="utf-8")
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
