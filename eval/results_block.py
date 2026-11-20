"""The results table of the README, generated from saved runs: nothing in it is typed by hand.

    python -m eval.results_block A.judged.json B.judged.json ...                 # print the block
    python -m eval.results_block A.judged.json B.judged.json --readme README.md  # replace the block between the markers

Takes the run files (a `.judged.json` made by `eval/rejudge.py`, or a plain run JSON) and pools their rows. A rate that needs the judge (judge
pass rate, groundedness, completeness) is over the rows the judge SCORED, and says how many: with a partial scoring (the daily limit stopped
`rejudge`) that is fewer than the rows that ran, and a rate without its n would pass for a full one. A row not scored yet is never a pass.
Between the markers `<!-- results:start -->` and `<!-- results:end -->` the README holds only this output.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from eval.repeats import aggregate
from eval.run_eval import HOLDOUT_GROUPS, holdout_group, summarize_all

START, END = "<!-- results:start -->", "<!-- results:end -->"
# Holdout cases whose behavior the prompt rules 5 and 8 target and whose baseline had been read when those rules were written: they are NOT blind.
# Fixed here by hand on purpose: never derived from results.
SEEN_BASELINE = ("ho06", "ho08", "ho12")


def is_seen_baseline(case_id: str) -> bool:
    return case_id.split("_")[0] in SEEN_BASELINE


def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def pct(v) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


def cell(group: dict | None, key: str, n_key: str = "cases", scale5: bool = False) -> str:
    if not group or group.get(key) is None or not group.get(n_key):
        return "-"
    v = group[key]
    return f"{pct(v / 5 if scale5 else v)} (n={group[n_key]})"


def counts(part: list[dict]) -> dict:
    part = [r for r in part if not r.get("provider_error")]
    return {"handoff_n": sum(r.get("handoff_ok") is not None for r in part), "memory_n": sum(r.get("memory_ok") is not None for r in part)}


def seen_column(seen_rows: list[dict]) -> dict | None:
    """The summary of the baseline-seen holdout cases, with the n of the rates over fewer cases (None when there are none)."""
    if not seen_rows:
        return None
    return {**summarize_all(seen_rows)["holdout_groups"]["blind"], **counts(seen_rows)}  # ids ho06+ land in the "blind" group of that call


def annotate(pooled: dict, rows: list[dict]) -> dict:
    """Add the n of the rates that are over fewer rows than `cases`: the handoff check (only cases that expect something) and the
    memory check (only multi-turn cases)."""
    main = [r for r in rows if not r.get("holdout")]
    out = {**pooled, **counts(main)}
    out["holdout_groups"] = {g: {**v, **counts([r for r in rows if r.get("holdout") and holdout_group(r["id"]) == g])}
                             for g, v in (pooled.get("holdout_groups") or {}).items()}
    return out


def rows_table(pooled: dict, seen: dict | None = None, case_counts: dict[str, int] | None = None) -> list[str]:
    groups = pooled.get("holdout_groups") or {}
    n = case_counts or {}
    label = {"original": "holdout, original", "blind": "holdout, new, not targeted", "v3": "holdout v3"}
    assert set(label) == set(HOLDOUT_GROUPS)
    cols = [("regular", pooled)]
    for g in HOLDOUT_GROUPS:
        if g == "v3" and not groups.get(g):
            continue  # its own column only when the run has hv3 cases: without them the table is what it always was
        cols.append((label[g] + (f" ({n[g]} cases)" if n.get(g) else ""), groups.get(g)))
    cols.append(("holdout, baseline seen" + (f" ({n['seen']} cases)" if n.get("seen") else ""), seen))
    out = ["| | " + " | ".join(name for name, _ in cols) + " |", "|---|" + "---|" * len(cols)]
    spec = [("Routing", "routing_accuracy", "cases", False), ("Tool use", "tool_use_accuracy", "cases", False),
            ("Handoff accuracy", "handoff_accuracy", "handoff_n", False), ("Memory after restart", "memory_accuracy", "memory_n", False),
            ("Degraded", "degraded_rate", "cases", False),
            ("Groundedness (judge)", "avg_groundedness", "judged_cases", True), ("Completeness (judge)", "avg_completeness", "judged_cases", True),
            ("**Judge pass rate**", "judge_pass_rate", "judged_cases", False)]
    for label, key, n_key, scale5 in spec:
        cells = []
        for _, g in cols:
            cells.append(cell(g, key, n_key or "cases", scale5))
        if any(c != "-" for c in cells):
            out.append(f"| {label} | " + " | ".join(cells) + " |")
    out.append("| Latency p50 / p95 | " + " | ".join(
        (f"{g['latency_ms_p50'] / 1000:.1f} s / {g['latency_ms_p95'] / 1000:.1f} s" if g and g.get("latency_ms_p50") else "-") for _, g in cols) + " |")
    out.append("| Cases x runs | " + " | ".join(str(g.get("cases", 0)) if g else "0" for _, g in cols) + " |")
    return out


def block(paths: list[str], now: datetime | None = None) -> str:
    runs = [load(p) for p in paths]
    rows_by_run = [r["rows"] for r in runs]
    all_rows = [row for rows in rows_by_run for row in rows]
    is_seen = lambda r: bool(r.get("holdout")) and is_seen_baseline(r["id"])  # noqa: E731
    seen_rows = [r for r in all_rows if is_seen(r)]
    rest = [r for r in all_rows if not is_seen(r)]  # the holdout cases that are not "baseline seen" keep their two groups
    pooled = annotate(summarize_all(rest), rest)
    seen = seen_column(seen_rows)
    case_counts = {"seen": len({r["id"] for r in seen_rows})}
    for g in HOLDOUT_GROUPS:
        case_counts[g] = len({r["id"] for r in rest if r.get("holdout") and holdout_group(r["id"]) == g})
    agg = aggregate(rows_by_run, [summarize_all(r) for r in rows_by_run])
    first = runs[0].get("summary", {})
    commits = sorted({r.get("summary", {}).get("commit", "unknown") for r in runs})
    clean = sorted({str(r.get("summary", {}).get("tree_clean", "unknown")) for r in runs})
    config = ", ".join([f"commit={'/'.join(c[:12] for c in commits)}", f"tree_clean={'/'.join(clean)}"]
                       + [f"{k}={first[k]}" for k in ("label", "replan_max", "rag_mode", "verifier_mode") if k in first])
    scored = agg["judged_per_run"]
    lines = [START, "",
             f"_Generated by `python -m eval.results_block` on {(now or datetime.now()):%Y-%m-%d %H:%M} from {len(paths)} run file(s): "
             + ", ".join(f"`{Path(p).name}`" for p in paths) + ". Nothing below is typed by hand._", ""]
    if config:
        lines += [f"_Configuration: {config}._", ""]
    if any(scored):
        lines += [f"**Judge: scored {sum(scored)} of {sum(agg['rows_per_run'])} rows** (per run: "
                  + ", ".join(f"{j} of {n}" for j, n in zip(scored, agg["rows_per_run"]))
                  + f"; {agg.get('pending', 0)} still pending). Every judge-based rate is over the rows scored, its n is in the cell.", ""]
    else:
        lines += ["**The judge has not scored these runs yet: only the routing, tool, handoff and memory checks are shown.**", ""]
    manifest = Path(__file__).parent / "calibration" / "sample_v3.json"
    if manifest.exists():
        lines += [f"_Calibration sample (rows a human labels, fixed before any judge note existed): {json.loads(manifest.read_text(encoding='utf-8')).get('note', '')}_", ""]
    if seen_rows:
        lines += [f"_Holdout, baseline seen ({', '.join(SEEN_BASELINE)}): they measure the behavior that prompt rules 5 and 8 target, and the baseline of the holdout had been read "
                  "when those rules were written. They are not blind and are not pooled with the other new holdout cases._", ""]
    lines += rows_table(pooled, seen, case_counts) + ["",
              f"Pooled over {len(paths)} run(s). Unstable cases (pass in some runs, not all): {', '.join(agg['unstable']) or 'none'}. "
              f"Fail in every scored run: {', '.join(agg['always_fail']) or 'none'}.", "", END]
    return "\n".join(lines)


def replace_in(readme: str, text: str, start: str = START, end: str = END) -> str:
    a, b = readme.find(start), readme.find(end)
    if a < 0 or b < a:
        raise ValueError(f"the document has no {start} ... {end} block to replace")
    return readme[:a] + text + readme[b + len(end):]


def fill(doc: str, name: str, body: str) -> str:
    """Replace what is between `<!-- NAME:start -->` and `<!-- NAME:end -->` with `body` (a generated block: nothing in it is typed by hand)."""
    start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
    return replace_in(doc, f"{start}\n{body.strip()}\n{end}", start, end)


def calibration_body(path: str | Path) -> str:
    """The calibration report as a block of the final report: its own numbers, copied by the program, without its top-level heading."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.startswith("# "))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--readme", "--report", dest="readme", help="replace the block between the markers in this file (the README or docs/FINAL_REPORT.md)")
    ap.add_argument("--v2", action="store_true", help="also fill the <!-- v2:start --> block with the V2 re-score report (eval/calibration/v2_rescore.py)")
    ap.add_argument("--calibration", metavar="FILE", help="also fill the <!-- calibration:start --> block with this calibration report")
    args = ap.parse_args(argv)
    text = block(args.runs)
    if args.readme:
        path = Path(args.readme)
        doc = replace_in(path.read_text(encoding="utf-8"), text)
        done = ["results"]
        if args.v2:
            from eval.calibration.v2_rescore import report_text

            doc, done = fill(doc, "v2", report_text()), done + ["v2"]
        if args.calibration:
            doc, done = fill(doc, "calibration", calibration_body(args.calibration)), done + ["calibration"]
        path.write_text(doc, encoding="utf-8")
        print(f"{path}: filled {', '.join(done)} ({len(args.runs)} run file(s))")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
