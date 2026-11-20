"""Score with the judge the rows of eval runs that were made WITHOUT it (`run_eval --no-judge`), without touching `eval/judge.py`.

    python -m eval.run_eval --repeats 5 --no-judge --pause 3 --retries 3      # the agents run: no judge tokens
    python -m eval.rejudge eval/reports/A.json eval/reports/B.json ...        # the judge, later, repetition by repetition
    python -m eval.rejudge --dry-run A.json B.json                            # how many calls and tokens, spending nothing
    python -m eval.repeats A.judged.json B.judged.json ...                    # the spread over the repetitions, as before

The files are scored IN THE ORDER GIVEN: repetition 1 completely, then repetition 2, and so on, so a stop always leaves whole
repetitions plus at most one partial one. Each input `X.json` is never modified: the scores go to `X.judged.json` (rows with
`judge`, a recomputed summary) and `X.judged.md`, saved after EVERY row, so a stop or a crash loses at most the call in flight. Run it
again and it resumes where it stopped (a row already scored is skipped; a row that ended as a judge error is tried again).

The judge is the frozen one (`eval.judge.Judge`, `JUDGE_PROMPT`, same scoring): this only decides WHEN to call it. `eval/judge_budget.py`
paces the calls under Groq's per-minute limit, waits out a per-minute 429, retries an unparseable answer a limited number of times and
STOPS before the daily cap (the rolling 24 h total is kept in a ledger on disk, shared with `eval.replan_ab judge`). A row the judge could
not score ends as `verdict: "error"`: counted apart in `judge_errors`, never as a fail.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

from eval.judge_budget import DAILY_LIMIT, MARGIN, BudgetedScorer, Ledger

from eval.run_eval import _prices, judge_question, load_cases, render_report, summarize_all

SKIP_KEYS = ("provider_error", "error")  # a row with no answer has nothing to judge
NO_DAILY_LIMIT = 10**12  # tokens: OpenRouter has no daily cap on the judge; the spending is limited by the cost guard (eval/cost_guard.py), not by this ledger


def judged_path(path: str | Path) -> Path:
    p = Path(path)
    return p.with_name(p.stem + ".judged.json")


def load_rows(path: str | Path) -> tuple[dict, list[dict]]:
    """The input run, or its `.judged.json` if a previous pass already started it (the progress)."""
    mine = judged_path(path)
    data = json.loads((mine if mine.exists() else Path(path)).read_text(encoding="utf-8"))
    return data, data["rows"]


def needs_judging(row: dict) -> bool:
    if any(row.get(k) for k in SKIP_KEYS) or not row.get("answer"):
        return False
    return row.get("judge") is None or row["judge"].get("verdict") == "error"


def save(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)  # never a half-written file


def finish(path: Path, data: dict) -> None:
    """Recompute the summary and write the report for what is scored so far."""
    rows = data["rows"]
    data["summary"] = {**data.get("summary", {}), **summarize_all(rows)}
    save(path, data)
    path.with_suffix(".md").write_text(render_report(rows, data["summary"], path.stem), encoding="utf-8")


def score_file(path: str, scorer: BudgetedScorer, cases: dict[str, dict], *, only: list[str] | None = None,
               print_fn=print) -> str:
    """Score one run. Returns "done" (every row has a verdict) or "stopped" (the daily cap says stop)."""
    data, rows = load_rows(path)
    out = judged_path(path)
    pending = [r for r in rows if needs_judging(r) and (not only or any(r["id"].startswith(p) for p in only))]
    print_fn(f"{Path(path).name}: {len(pending)} row(s) to score")
    for n, row in enumerate(pending, 1):
        case = cases.get(row["id"])
        if case is None:
            print_fn(f"   {row['id']}: not in the dataset, skipped")
            continue
        result, status = scorer.score(judge_question(case), row["answer"], row.get("evidence", []), case.get("criteria", ""),
                                      label=f"{Path(path).stem}:{row['id']}")
        if status == "daily_cap":
            finish(out, data)
            print_fn(f"   STOP before the daily cap (scored {n - 1} of {len(pending)} here). Run it again when the window frees.")
            return "stopped"
        row["judge"] = result
        save(out, data)  # after EVERY row
        print_fn(f"   [{n}/{len(pending)}] {row['id']}: {result.get('verdict')} (judge tokens so far in 24h: {scorer.ledger.spent()})")
    finish(out, data)
    return "done"  # a row that ended as a judge error is reported (judge_errors) and tried again by the next pass


def score_all(paths: list[str], scorer: BudgetedScorer, cases: dict[str, dict], *, only: list[str] | None = None,
              wait_until: float | None = None, wake_rows: int = 8, sleep=time.sleep, print_fn=print) -> dict:
    """Score the files in order. With `wait_until` (epoch seconds), a stop at the daily limit is not the end: it waits until the ledger shows room
    for `wake_rows` more rows, resumes where it stopped, and gives up only at the deadline. Returns {"complete": bool, "waited_s": float}."""
    from eval.judge_budget import next_room_time

    waited = 0.0
    while True:
        stopped = False
        for p in paths:
            if score_file(p, scorer, cases, only=only, print_fn=print_fn) == "stopped":
                stopped = True
                break
        if not stopped:
            return {"complete": True, "waited_s": waited}
        if wait_until is None:
            return {"complete": False, "waited_s": waited}
        when = next_room_time(scorer.ledger, wake_rows * scorer.ledger.recent_average(), scorer.ceiling)
        now = scorer.ledger.clock()
        if when is None or when >= wait_until:
            print_fn(f"   the window does not free enough before the deadline: stopped clean, resume with the same command.")
            return {"complete": False, "waited_s": waited}
        pause = max(1.0, when - now) + 5
        print_fn(f"   daily limit: waiting {pause / 60:.0f} min for the window to free room for {wake_rows} rows, then resuming...")
        sleep(pause)
        waited += pause


def plan(paths: list[str], estimate: int) -> list[tuple[str, int]]:
    return [(p, sum(needs_judging(r) for r in load_rows(p)[1])) for p in paths]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("runs", nargs="+", help="run JSON files, in the order they must be scored (repetition 1 first)")
    ap.add_argument("--only", nargs="*", help="case id prefixes")
    ap.add_argument("--sample", metavar="MANIFEST", help="score only the rows of a calibration sample (eval/calibration/sample_v3.json); a row of it "
                    "that the FIRST run lacks is replaced by the replacement decided in advance")
    ap.add_argument("--wait-until", metavar="'YYYY-MM-DD HH:MM'", help="at the daily limit, wait for the window to free and resume, until this local time")
    ap.add_argument("--wake-rows", type=int, default=8, help="with --wait-until: resume when there is room for this many rows")
    ap.add_argument("--dry-run", action="store_true", help="count the calls and the tokens, call nothing")
    ap.add_argument("--daily-limit", type=int, default=None,
                    help=f"tokens per rolling 24 h. Default: {DAILY_LIMIT} when the judge runs on Groq (its free-tier cap), practically unlimited on OpenRouter")
    ap.add_argument("--no-cost-guard", action="store_true", help="skip the cost guard (only for tests and for a judge that is not on OpenRouter)")
    ap.add_argument("--margin", type=int, default=MARGIN, help="stop this many tokens below the daily limit")
    ap.add_argument("--ledger", default=None, help="the ledger file (default eval/reports/judge_ledger.json)")
    args = ap.parse_args(argv)
    if args.daily_limit is None:
        from app.config import get_settings

        args.daily_limit = DAILY_LIMIT if get_settings().judge_provider.lower() == "groq" else NO_DAILY_LIMIT

    only = args.only
    if args.sample:
        from eval.calibration.sample import check, choose
        from eval.calibration.sample import load_cases as sample_cases

        manifest = choose(sample_cases())
        saved = json.loads(Path(args.sample).read_text(encoding="utf-8"))
        if saved["ids"] != manifest["ids"]:  # the dataset or the sampler changed after the sample was fixed
            raise SystemExit(f"{args.sample} does not match what the sampler gives now: the sample must not change after it is fixed")
        res = check(manifest, args.runs[0])  # the first file given is repetition 1
        only = res["replacement"]
        print(f"sample of {len(only)} rows" + (f"; missing from {Path(args.runs[0]).name}: {res['missing']} (replaced in advance)" if res["missing"] else ""))
    ledger = Ledger(args.ledger)
    est = ledger.recent_average()
    todo = plan(args.runs, est) if not only else [(p, sum(needs_judging(r) and any(r["id"].startswith(o) for o in only) for r in load_rows(p)[1])) for p in args.runs]
    total = sum(n for _, n in todo)
    print(f"{total} row(s) to score in {len(todo)} file(s); ~{est} tokens each = ~{total * est} tokens. "
          f"Spent in the last 24 h: {ledger.spent()} of {args.daily_limit} (stop line {args.daily_limit - args.margin}).")
    for p, n in todo:
        print(f"   {Path(p).name}: {n}")
    if args.dry_run or not total:
        return 0

    guard = ticket = None
    if not args.no_cost_guard:  # the judge's calls cost money on OpenRouter: the estimate must fit the budget before the first one
        from eval.cost_guard import CostGuard

        guard = CostGuard()
        ticket = guard.start(block="rejudge", command=" ".join(["python", "-m", "eval.rejudge", *(argv if argv is not None else __import__("sys").argv[1:])]),
                             n_case_runs=total, include_agents=False)

    from eval.judge import Judge  # imported late: a dry run needs no key

    cases = {c["id"]: c for c in load_cases(None)}
    scorer = BudgetedScorer(Judge(), ledger, daily_limit=args.daily_limit, margin=args.margin, sleep=time.sleep,
                            collector_factory=lambda: __import__("app.observability", fromlist=["TurnCollector"]).TurnCollector(_prices()))
    started = time.time()
    deadline = datetime.strptime(args.wait_until, "%Y-%m-%d %H:%M").timestamp() if args.wait_until else None
    outcome = score_all(args.runs, scorer, cases, only=only, wait_until=deadline, wake_rows=args.wake_rows, sleep=time.sleep)
    print(f"\n{'ALL SCORED' if outcome['complete'] else 'STOPPED CLEAN: some rows are not scored yet (the window frees; run the same command again)'}")
    print(f"\njudge calls this run: {scorer.calls}, tokens: {scorer.tokens}, 24 h total: {ledger.spent()} of {args.daily_limit}, "
          f"{time.time() - started:.0f}s")
    if guard is not None and ticket is not None:
        guard.finish(ticket)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
