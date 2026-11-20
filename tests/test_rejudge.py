"""eval/rejudge.py and eval/judge_budget.py: scoring saved runs later, inside the judge's free-tier limits. A fake judge and a fake
clock: no network, no keys, no waiting."""
import json

import pytest

from eval.judge_budget import DAILY_LIMIT, MARGIN, BudgetedScorer, Ledger, is_daily, is_rate_limit, wait_hint
from eval.rejudge import judged_path, load_rows, main, needs_judging, score_file
from eval.run_eval import load_cases

IDS = [c["id"] for c in load_cases(None)][:4]
CASES = {c["id"]: c for c in load_cases(None)}
OK = {"relevance": 5, "groundedness": 5, "completeness": 5, "tone": 5, "verdict": "pass", "reason": "fine", "unsupported_claims": [],
      "language_match": True}
FAIL = {**OK, "verdict": "fail", "groundedness": 2}


def err(text):
    return {"relevance": 0, "groundedness": 0, "completeness": 0, "tone": 0, "verdict": "error", "reason": f"judge_error: {text}"}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class FakeJudge:
    """Answers from a script, one entry per call; every call 'costs' `tokens` (through the ledger the scorer keeps)."""

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def score(self, question, answer, evidence, criteria="", callbacks=None):
        self.calls.append(question)
        return self.script.pop(0) if self.script else OK


def scorer(tmp_path, judge, clock=None, **kw):
    clock = clock or Clock()
    ledger = Ledger(tmp_path / "ledger.json", clock=clock)
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock.now += s

    sc = BudgetedScorer(judge, ledger, sleep=sleep, **kw)
    sc.sleeps = sleeps
    return sc, ledger, clock


def run_file(tmp_path, name, ids=IDS, judged=()):
    rows = [{"id": i, "message": CASES[i]["message"], "answer": f"answer {i}", "evidence": ["e1"], "plan": [], "tools_called": [],
             "routing_ok": True, "tools_ok": True, "handoff_ok": None, "latency_ms": 5, "degraded": False} for i in ids]
    for r in rows:
        if r["id"] in judged:
            r["judge"] = dict(OK)
    rows.append({"id": "provider-error-row", "message": "x", "provider_error": "boom", "latency_ms": 0})
    path = tmp_path / name
    path.write_text(json.dumps({"summary": {"cases": len(rows)}, "rows": rows}), encoding="utf-8")
    return str(path)


def verdicts(path):
    return {r["id"]: (r.get("judge") or {}).get("verdict") for r in load_rows(path)[1]}


# ---------------- which rows ----------------

def test_only_rows_with_an_answer_and_no_good_verdict_are_scored():
    assert needs_judging({"id": "a", "answer": "x"})
    assert needs_judging({"id": "a", "answer": "x", "judge": err("timeout")})  # an error is tried again
    assert not needs_judging({"id": "a", "answer": "x", "judge": dict(OK)})
    assert not needs_judging({"id": "a", "provider_error": "429"}) and not needs_judging({"id": "a", "answer": ""})


# ---------------- order, progress, resume ----------------

def test_the_input_is_never_modified_and_the_scores_go_to_a_judged_file_saved_after_every_row(tmp_path):
    path = run_file(tmp_path, "r1.json")
    before = open(path, encoding="utf-8").read()
    sc, *_ = scorer(tmp_path, FakeJudge([OK, FAIL, OK, OK]))
    assert score_file(path, sc, CASES, print_fn=lambda *a: None) == "done"
    assert open(path, encoding="utf-8").read() == before
    got = verdicts(judged_path(path))
    assert [got[i] for i in IDS] == ["pass", "fail", "pass", "pass"] and got["provider-error-row"] is None
    out = json.loads(judged_path(path).read_text(encoding="utf-8"))
    assert out["summary"]["judge_pass_rate"] == 0.75 and judged_path(path).with_suffix(".md").exists()


def test_it_stops_before_the_cap_and_a_second_pass_resumes_without_repeating_a_row(tmp_path):
    path = run_file(tmp_path, "r1.json")
    judge = FakeJudge([OK, OK, OK, OK])
    # room for exactly two calls: ceiling = limit - margin; the ledger already holds most of it
    sc, ledger, clock = scorer(tmp_path, judge, daily_limit=12_000, margin=0)
    for _ in range(3):
        ledger.record(2_400, "earlier")  # 7,200 of 12,000 used: room for exactly two more calls of ~2,400
    assert score_file(path, sc, CASES, print_fn=lambda *a: None) == "stopped"
    assert len(judge.calls) == 2 and sum(v == "pass" for v in verdicts(judged_path(path)).values()) == 2
    clock.now += 86_400 + 5  # the window frees
    sc2, *_ = scorer(tmp_path, judge, clock=clock)
    assert score_file(path, sc2, CASES, print_fn=lambda *a: None) == "done"
    assert len(judge.calls) == 4  # the two remaining rows only
    assert all(verdicts(judged_path(path))[i] == "pass" for i in IDS)


def test_the_files_are_scored_in_the_order_given_repetition_one_before_two(tmp_path, capsys):
    a, b = run_file(tmp_path, "r1.json"), run_file(tmp_path, "r2.json")
    judge = FakeJudge([])
    sc, ledger, clock = scorer(tmp_path, judge, daily_limit=19_200, margin=0)
    for _ in range(2):
        ledger.record(2_400, "earlier")  # 4,800 of 19,200 used: room for 6 calls, all of r1 and half of r2
    import eval.rejudge as rj

    for p in (a, b):
        if rj.score_file(p, sc, CASES, print_fn=lambda *a: None) == "stopped":
            break
    assert all(v == "pass" for k, v in verdicts(judged_path(a)).items() if k in IDS)  # repetition 1 is complete
    assert sum(v == "pass" for v in verdicts(judged_path(b)).values()) == 2 and len(judge.calls) == 6


def test_a_dry_run_counts_and_spends_nothing(tmp_path, capsys, monkeypatch):
    a = run_file(tmp_path, "r1.json", judged=[IDS[0]])
    monkeypatch.setattr("eval.judge.Judge", lambda: (_ for _ in ()).throw(AssertionError("the judge must not be created")))
    assert main([a, "--dry-run", "--ledger", str(tmp_path / "l.json")]) == 0
    out = capsys.readouterr().out
    assert "3 row(s) to score" in out and "r1.json: 3" in out
    assert not judged_path(a).exists()


# ---------------- the limits ----------------

def test_a_per_minute_429_is_waited_out_and_the_row_is_scored(tmp_path):
    judge = FakeJudge([err("Error code: 429 - Rate limit reached ... on tokens per minute (TPM): Limit 8000. Please try again in 7.2s"), OK])
    sc, *_ = scorer(tmp_path, judge)
    result, status = sc.score("q", "a", ["e"])
    assert status == "ok" and result["verdict"] == "pass" and len(judge.calls) == 2
    assert any(abs(s - 9.2) < 0.01 for s in sc.sleeps)  # the hint (7.2 s) plus the margin


def test_a_daily_429_stops_the_run_and_is_never_retried(tmp_path):
    judge = FakeJudge([err("Error code: 429 - Rate limit reached on tokens per day (TPD): Limit 200000, Used 199000. Please try again in 9m")])
    sc, *_ = scorer(tmp_path, judge)
    assert sc.score("q", "a", ["e"]) == (None, "daily_cap") and len(judge.calls) == 1


def test_an_answer_that_never_parses_is_retried_a_few_times_then_reported_as_an_error_not_a_fail(tmp_path):
    judge = FakeJudge([err("no JSON in the answer")] * 3)
    sc, *_ = scorer(tmp_path, judge, parse_retries=2)
    result, status = sc.score("q", "a", ["e"])
    assert status == "error" and result["verdict"] == "error" and len(judge.calls) == 3  # the first try and two more


def test_in_the_summary_a_judge_error_is_counted_apart_and_never_averaged_in(tmp_path):
    path = run_file(tmp_path, "r1.json", ids=IDS[:2])
    sc, *_ = scorer(tmp_path, FakeJudge([OK, err("no JSON"), err("no JSON"), err("no JSON")]), parse_retries=2)
    score_file(path, sc, CASES, print_fn=lambda *a: None)
    summary = json.loads(judged_path(path).read_text(encoding="utf-8"))["summary"]
    assert summary["judge_errors"] == 1 and summary["judge_pass_rate"] == 1.0  # 1 pass of 1 scored; the error is not a fail
    # and a later pass tries that row again
    sc2, *_ = scorer(tmp_path, FakeJudge([FAIL]))
    score_file(path, sc2, CASES, print_fn=lambda *a: None)
    assert json.loads(judged_path(path).read_text(encoding="utf-8"))["summary"]["judge_pass_rate"] == 0.5


def test_calls_are_paced_so_the_per_minute_limit_is_never_hit(tmp_path):
    judge = FakeJudge([])
    sc, ledger, clock = scorer(tmp_path, judge)
    ledger.record(6_500, "just now")  # almost the whole minute already used
    sc.score("q", "a", ["e"])
    assert sc.sleeps and sc.sleeps[0] >= 1 and len(judge.calls) == 1  # it waited BEFORE the call, there was no 429 to wait out


def test_patterns_that_tell_the_two_limits_apart():
    assert is_daily("on tokens per day (TPD): Limit 200000") and not is_daily("on tokens per minute (TPM): Limit 8000")
    assert is_rate_limit("Error code: 429 - Too Many Requests") and not is_rate_limit("Expecting value: line 1 column 1")
    assert wait_hint("Please try again in 7.2s") == pytest.approx(9.2) and wait_hint("try again in 850ms") == 5.0 and wait_hint("") == 5.0


# ---------------- the ledger ----------------

def test_the_ledger_is_shared_between_runs_slides_with_the_clock_and_survives_damage(tmp_path):
    clock = Clock()
    a = Ledger(tmp_path / "l.json", clock=clock)
    a.record(1_900, "x")
    a.record(2_100, "y")
    b = Ledger(tmp_path / "l.json", clock=clock)  # another process
    assert b.spent() == 4_000 and b.spent(60) == 4_000 and b.recent_average() == 2_000
    clock.now += 3_600
    assert b.spent() == 4_000 and b.spent(60) == 0
    clock.now += 86_400
    assert b.spent() == 0  # the window slid past them
    (tmp_path / "l.json").write_text("not json", encoding="utf-8")
    assert Ledger(tmp_path / "l.json", clock=clock).spent() == 0  # a damaged ledger does not stop a run


def test_the_defaults_match_what_was_measured():
    assert DAILY_LIMIT == 200_000 and MARGIN == 10_000


# ---------------- when the window frees and how much fits before a deadline ----------------

def entries_at(*pairs):
    return [{"at": a, "tokens": t, "label": ""} for a, t in pairs]


def test_the_capacity_plan_spends_the_room_now_and_again_when_the_old_entries_leave_the_window():
    from eval.judge_budget import plan_capacity

    h, day = 3600.0, 86400.0
    t0 = 1_800_000_000.0
    ledger = entries_at((t0 - 2 * h, 37_000))  # spent two hours before the start
    plan = plan_capacity(ledger, start=t0, deadline=t0 + 40 * h, ceiling=190_000)
    # now: 190,000 - 37,000 = 153,000. When the 37,000 leave (22 h later): 37,000 more. When the 153,000 leave (24 h after the start):
    # the whole ceiling is free again, less what was spent 2 h after the first leaving (the 37,000 re-spent) which is still inside
    assert plan["steps"][0] == (t0, 153_000, "start")
    assert plan["steps"][1][0] == t0 + day - 2 * h and plan["steps"][1][1] == 37_000
    assert plan["steps"][2][0] == t0 + day and plan["steps"][2][1] == 153_000  # the 153,000 left; the 37,000 re-spent are still counted
    assert plan["total"] == 153_000 + 37_000 + 153_000
    assert plan["frees"][0][0] == t0 - 2 * h + day  # the replan tokens leave 24 h after they were spent


def test_the_plan_never_lets_the_trailing_window_exceed_the_ceiling():
    from eval.judge_budget import plan_capacity

    t0 = 1_800_000_000.0
    ledger = entries_at((t0 - 3600, 50_000), (t0 - 7200, 20_000))
    plan = plan_capacity(ledger, t0, t0 + 60 * 3600, ceiling=190_000)
    allspent = [(e["at"], e["tokens"]) for e in ledger] + [(t, k) for t, k, _ in plan["steps"]]
    for t, _, _ in plan["steps"]:
        assert sum(k for a, k in allspent if t - 86400 < a <= t) <= 190_000


def test_nothing_is_planned_when_the_window_is_full_until_something_leaves_it():
    from eval.judge_budget import plan_capacity

    t0 = 1_800_000_000.0
    plan = plan_capacity(entries_at((t0 - 600, 190_000)), t0, t0 + 3 * 3600, ceiling=190_000)
    assert plan["total"] == 0 and plan["steps"] == []


# ---------------- stop clean, resume when the window frees ----------------

def test_next_room_time_is_now_when_there_is_room_and_when_the_oldest_entries_leave_when_there_is_not(tmp_path):
    from eval.judge_budget import next_room_time

    clock = Clock()
    ledger = Ledger(tmp_path / "l.json", clock=clock)
    assert next_room_time(ledger, 10_000) == clock.now  # empty: room now
    clock.now += 100
    ledger.record(100_000, "a")
    clock.now += 3_600
    ledger.record(88_000, "b")  # 188,000 of the 190,000 line
    t_a, t_b = clock.now - 3_600 + 86_400, clock.now + 86_400
    assert next_room_time(ledger, 1_000) == clock.now  # room for 2,000 left
    assert next_room_time(ledger, 50_000) == t_a  # the 100,000 leaves first: 88,000 remain, 102,000 of room
    assert next_room_time(ledger, 150_000) == t_b  # needs both gone
    assert next_room_time(ledger, 190_001) is None


def test_with_wait_until_a_stop_at_the_limit_sleeps_until_the_window_frees_and_then_finishes(tmp_path):
    from eval.rejudge import score_all

    path = run_file(tmp_path, "r1.json")
    judge = FakeJudge([])
    sc, ledger, clock = scorer(tmp_path, judge, daily_limit=12_000, margin=0)
    for _ in range(3):
        ledger.record(2_400, "earlier")  # room for exactly two more rows now
    out = score_all([path], sc, CASES, wait_until=clock.now + 3 * 86_400, wake_rows=2, sleep=sc.sleep, print_fn=lambda *a: None)
    assert out["complete"] is True and len(judge.calls) == 4  # two before the stop, two after the window freed
    assert out["waited_s"] >= 86_400 - 60 and all(v == "pass" for k, v in verdicts(judged_path(path)).items() if k in IDS)


def test_without_wait_until_a_stop_is_clean_and_reports_that_it_is_not_complete(tmp_path):
    from eval.rejudge import score_all

    path = run_file(tmp_path, "r1.json")
    sc, ledger, clock = scorer(tmp_path, FakeJudge([]), daily_limit=12_000, margin=0)
    for _ in range(3):
        ledger.record(2_400, "earlier")
    out = score_all([path], sc, CASES, print_fn=lambda *a: None)
    assert out["complete"] is False and sum(v == "pass" for v in verdicts(judged_path(path)).values()) == 2


def test_a_deadline_that_comes_before_the_window_frees_stops_clean_instead_of_waiting_for_nothing(tmp_path):
    from eval.rejudge import score_all

    path = run_file(tmp_path, "r1.json")
    sc, ledger, clock = scorer(tmp_path, FakeJudge([]), daily_limit=12_000, margin=0)
    for _ in range(3):
        ledger.record(2_400, "earlier")
    out = score_all([path], sc, CASES, wait_until=clock.now + 3_600, wake_rows=2, sleep=sc.sleep, print_fn=lambda *a: None)
    assert out["complete"] is False and out["waited_s"] == 0


# ---------------- --sample: the 15 rows, with the replacement decided in advance ----------------

def test_sample_scores_only_the_fifteen_rows_and_replaces_one_the_run_lacks(tmp_path, monkeypatch):
    import json as _json

    from eval.calibration.sample import choose, load_cases

    manifest = choose(load_cases())
    gone = manifest["strata"]["regular_with_tools"]["chosen"][0]
    rows = [{**{"routing_ok": True, "tools_ok": True, "handoff_ok": None, "latency_ms": 5, "message": "m", "evidence": ["e"], "plan": [],
                "tools_called": []}, "id": c["id"], "answer": "a"} for c in load_cases() if c["id"] != gone]
    rows.append({"id": gone, "provider_error": "429", "message": "m", "latency_ms": 0})
    run = tmp_path / "rep1.json"
    run.write_text(_json.dumps({"summary": {}, "rows": rows}), encoding="utf-8")
    sample_file = tmp_path / "sample.json"
    sample_file.write_text(_json.dumps(manifest), encoding="utf-8")
    judge = FakeJudge([])
    monkeypatch.setattr("eval.judge.Judge", lambda: judge)
    monkeypatch.setattr("time.sleep", lambda s: None)  # the per-minute pacing is real in production; here nobody waits
    assert main([str(run), "--sample", str(sample_file), "--ledger", str(tmp_path / "l.json"), "--no-cost-guard"]) == 0
    got = {i for i, v in verdicts(judged_path(run)).items() if v}
    replacement = manifest["strata"]["regular_with_tools"]["reserve_in_order"][0]
    assert len(got) == 15 == len(judge.calls) and gone not in got and replacement in got
    assert got - {replacement} == set(manifest["ids"]) - {gone}  # nothing else moved


def test_sample_refuses_a_manifest_that_no_longer_matches_what_the_sampler_gives(tmp_path):
    import json as _json

    run = run_file(tmp_path, "rep1.json")
    bad = tmp_path / "sample.json"
    bad.write_text(_json.dumps({"ids": ["q01_compare_plans"]}), encoding="utf-8")
    with pytest.raises(SystemExit, match="must not change after it is fixed"):
        main([run, "--sample", str(bad), "--ledger", str(tmp_path / "l.json")])
