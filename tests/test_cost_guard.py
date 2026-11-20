"""The cost guard (plan 9.6, M2): the rule, the key's counter as the truth, host prices, tokens of recent runs, the ledger. No network."""
import json
import os
import time
from pathlib import Path

import pytest

from app.config import Settings
from eval import cost_guard as cg

ROOT = Path(__file__).resolve().parent.parent
ENDPOINTS = {
    "openai/gpt-oss-120b": [{"tag": "groq", "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}},
                            {"tag": "deepinfra/bf16", "pricing": {"prompt": "0.000000037", "completion": "0.00000017"}},
                            {"tag": "cerebras/fp16", "pricing": {"prompt": "0.00000035", "completion": "0.00000075"}}],
    "qwen/qwen3.8-27b": [{"tag": "alibaba", "pricing": {"prompt": "0.000000425", "completion": "0.00000255"}},
                         {"tag": "chutes/fp8", "pricing": {"prompt": "0.000000039", "completion": "0.00000125"}}],
    "anthropic/claude-haiku-5.5": [{"tag": "anthropic", "pricing": {"prompt": "0.0000001", "completion": "0.0000005"}},
                                   {"tag": "bedrock", "pricing": {"prompt": "0.0000002", "completion": "0.000001"}}],
}


def fetch(model):
    if model not in ENDPOINTS:
        raise ValueError("unknown model")
    return ENDPOINTS[model]


def settings(**kw):
    base = dict(_env_file=None, agent_provider="openrouter", judge_provider="openrouter", light_model="openai/gpt-oss-120b", llm_model="openai/gpt-oss-120b",
                openrouter_provider_order={"openai/gpt-oss-120b": ["groq"], "qwen/qwen3.8-27b": ["alibaba"]})
    return Settings(**{**base, **kw})


# ---- the rule ----

def test_the_numbers_of_the_rule_are_the_ones_decided():
    assert (cg.SAFETY, cg.ABORT_LINE_USD, cg.SINGLE_RUN_CAP_USD, cg.KEY_LIMIT_USD) == (1.5, 8.5, 3.0, 9.0)


@pytest.mark.parametrize("estimate, spent, ok", [
    (1.0, 0.0, True), (2.0, 0.0, True),  # 1.5 x 2.0 = 3.0 is not above 3.0
    (2.01, 0.0, False),  # one run: 1.5 x 2.01 = 3.015 > 3.0
    (1.0, 7.0, True),  # 7.0 + 1.5 = 8.5 is not above 8.5
    (1.0, 7.01, False), (0.1, 8.4, False),  # 8.4 + 0.15 = 8.55
    (0.0, 8.5, True), (0.0, 8.51, False),
])
def test_the_rule_aborts_only_above_the_line_and_the_single_run_cap(estimate, spent, ok):
    assert cg.check(estimate, spent, None).ok is ok


def test_both_reasons_are_reported_and_say_the_numbers():
    v = cg.check(3.0, 8.0, None)
    assert not v.ok and len(v.reasons) == 2 and "8.0000 + 1.5 x estimate US$3.0000" in v.reasons[0] and "above US$3.00 for one run" in v.reasons[1]


def test_a_key_without_a_limit_is_a_warning_and_a_key_with_a_lower_limit_lowers_the_line():
    assert "NO limit" in cg.check(0.1, 0.0, None).warnings[0]
    v = cg.check(1.0, 6.0, 9.0)
    assert v.ok and v.abort_line == 8.5 and not v.warnings
    v = cg.check(1.0, 6.5, 8.0)  # the line is 8.0 - 0.5 = 7.5: 6.5 + 1.5 = 8.0 is above it
    assert not v.ok and v.abort_line == 7.5


# ---- the counter after a run ----

class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def sleep(self, s):
        self.t += s
        self.sleeps.append(s)


def counter(clock, schedule, default):
    """A counter that reads `default` unless `schedule` (time -> value, the last one at or before now) says otherwise."""
    def read():
        value = default
        for t, v in sorted(schedule.items()):
            if clock.t >= t:
                value = v
        return value
    return read


def test_a_counter_that_lags_is_waited_out_until_it_is_stable():
    c = Clock()
    value, settled = cg.settle_counter(counter(c, {30: 2.0}, 1.0), c.sleep)
    assert settled and value == 2.0  # it jumped at 30 s; three equal reads (30, 45, 60 s) after that


def test_a_counter_is_never_accepted_before_the_minimum_wait():
    c = Clock()
    value, settled = cg.settle_counter(counter(c, {}, 5.0), c.sleep)
    assert settled and value == 5.0 and sum(c.sleeps) >= cg.SETTLE_MIN_WAIT_S


def test_a_counter_that_keeps_moving_is_provisional_at_the_timeout():
    c = Clock()
    ticks = {t: float(t) for t in range(0, 400, 5)}
    value, settled = cg.settle_counter(counter(c, ticks, 0.0), c.sleep)
    assert not settled and sum(c.sleeps) >= cg.SETTLE_TIMEOUT_S


# ---- prices of the pinned host ----

def test_the_price_is_the_pinned_hosts_not_the_cheapest_or_the_listed_one():
    p = cg.price_of("openai/gpt-oss-120b", ["groq"], fetch)
    assert (p["in"], p["out"]) == pytest.approx((0.15, 0.60)) and "pinned host groq" in p["source"]
    p = cg.price_of("qwen/qwen3.8-27b", ["alibaba"], fetch)
    assert (p["in"], p["out"]) == pytest.approx((0.425, 2.55))  # not the 0.039/1.25 of the cheapest endpoint


def test_a_base_slug_matches_its_variants_and_the_dearest_of_them_is_taken():
    p = cg.price_of("openai/gpt-oss-120b", ["deepinfra"], fetch)
    assert p["in"] == pytest.approx(0.037)


def test_a_model_that_is_not_pinned_is_priced_at_its_dearest_endpoint():
    p = cg.price_of("anthropic/claude-haiku-5.5", [], fetch)
    assert (p["in"], p["out"]) == pytest.approx((0.2, 1.0)) and "dearest" in p["source"]


def test_an_unreadable_listing_falls_back_to_the_table_and_says_so_and_with_no_table_price_it_aborts():
    def broken(model):
        import httpx
        raise httpx.ConnectError("down")

    p = cg.price_of("openai/gpt-oss-120b", ["groq"], broken, table={"openai/gpt-oss-120b@groq": (0.15, 0.60)})
    assert (p["in"], p["out"]) == (0.15, 0.60) and "could not be read" in p["source"]
    with pytest.raises(cg.CostGuardAbort, match="no price"):
        cg.price_of("x/unknown", [], broken, table={})


# ---- tokens of the recent runs ----

def write_run(path, rows):
    path.write_text(json.dumps({"summary": {}, "rows": rows}), encoding="utf-8")


def row(by_node, judge=None):
    r = {"usage": {"by_node": [{"node": n, "model": m, "calls": 1, "input_tokens": i, "output_tokens": o} for n, m, i, o in by_node]}}
    if judge:
        r["judge_usage"] = {"by_node": [{"node": "judge", "model": m, "calls": 1, "input_tokens": i, "output_tokens": o} for m, i, o in judge]}
    return r


def test_tokens_are_averaged_per_case_run_with_fireworks_ids_normalized(tmp_path):
    write_run(tmp_path / "20261115-000001.json", [row([("router", "accounts/fireworks/models/gpt-oss-120b", 1000, 50)]),
                                                  row([("router", "accounts/fireworks/models/gpt-oss-120b", 1200, 70), ("knowledge", "accounts/fireworks/models/gpt-oss-120b", 3000, 300)])])
    t = cg.recent_tokens(tmp_path)
    assert t["by_node_model"][("router", "openai/gpt-oss-120b")]["in"] == 1100 and t["by_node_model"][("router", "openai/gpt-oss-120b")]["out"] == 60
    assert t["by_node_model"][("knowledge", "openai/gpt-oss-120b")]["in"] == 1500  # 3000 over the 2 rows of the file: the role is not in every row
    assert t["files"] == ["20261115-000001.json"]


def test_a_judged_copy_and_a_file_without_usage_are_ignored(tmp_path):
    write_run(tmp_path / "20261115-000001.json", [row([("router", "m", 100, 10)])])
    write_run(tmp_path / "20261115-000001.judged.json", [row([("router", "m", 999999, 9)])])
    write_run(tmp_path / "20261115-000002.json", [{"id": "x"}])
    t = cg.recent_tokens(tmp_path)
    assert t["by_node_model"][("router", "m")]["in"] == 100 and t["files"] == ["20261115-000001.json"]


def test_the_judges_tokens_come_from_a_wider_window_than_the_agents(tmp_path):
    now = time.time()
    for i in range(5):  # five newer runs without a judge
        p = tmp_path / f"2026111{i}-000000.json"
        write_run(p, [row([("router", "m", 100, 10)])])
        os.utime(p, (now - 10 + i, now - 10 + i))
    old = tmp_path / "20261101-000000.json"
    write_run(old, [row([("router", "m", 100, 10)], judge=[("qwen/qwen3.8-27b", 1500, 100)])])
    os.utime(old, (now - 99999, now - 99999))
    t = cg.recent_tokens(tmp_path, max_files=3)
    assert len(t["files"]) == 3 and t["judge"]["qwen/qwen3.8-27b"] == {"in": 1500, "out": 100}


# ---- the estimate ----

TOKENS = {"by_node_model": {("router", "openai/gpt-oss-120b"): {"in": 1000.0, "out": 100.0, "rows": 50, "files": 2},
                            ("knowledge", "openai/gpt-oss-120b"): {"in": 2000.0, "out": 200.0, "rows": 50, "files": 2},
                            ("verify", "openai/gpt-oss-120b"): {"in": 1500.0, "out": 500.0, "rows": 50, "files": 2}},
          "by_node": {"router": {"in": 1000.0, "out": 100.0}, "knowledge": {"in": 2000.0, "out": 200.0}, "verify": {"in": 1500.0, "out": 500.0}},
          "judge": {"qwen/qwen3.8-27b": {"in": 1500.0, "out": 100.0}}, "files": ["a.json"]}


def per_case_run(inp, out, pin, pout):
    return (inp * pin + out * pout) / 1e6


def test_observed_tokens_at_the_pinned_hosts_prices_give_the_estimate():
    est = cg.estimate_run(settings(), 10, TOKENS, include_judge=True, fetch=fetch)
    agents = per_case_run(1000, 100, 0.15, 0.6) + per_case_run(2000, 200, 0.15, 0.6) + per_case_run(1500, 500, 0.15, 0.6)
    judge = per_case_run(1500, 100, 0.425, 2.55)
    assert est.total_usd == pytest.approx((agents + judge) * 10)
    assert est.assumed_roles == [] and {ln.role for ln in est.lines} == {"router", "knowledge", "verify", "judge"}
    assert "support: no call in the recent runs" in est.skipped and "synthesizer: no call in the recent runs" in est.skipped


def test_a_model_with_no_tokens_of_its_own_is_assumed_from_the_role_under_another_model_with_explicit_factors():
    est = cg.estimate_run(settings(knowledge_model="anthropic/claude-haiku-5.5", openrouter_provider_order={"openai/gpt-oss-120b": ["groq"]}),
                          1, TOKENS, include_judge=False, fetch=fetch)
    k = next(ln for ln in est.lines if ln.role == "knowledge")
    assert k.assumed and (k.tokens_in, k.tokens_out) == (2000 * 1.25, 200 * 3.0) and "x(1.25, 3)" in k.basis
    assert (k.price_in, k.price_out) == pytest.approx((0.2, 1.0)) and "dearest" in k.price_source  # not pinned: the dearest endpoint
    assert est.assumed_roles == ["knowledge (anthropic/claude-haiku-5.5)"]


def test_a_role_that_was_never_seen_uses_the_fixed_table_only_for_the_judge_and_observe():
    no_judge = {**TOKENS, "judge": {}}
    est = cg.estimate_run(settings(replan_max=2), 1, no_judge, include_judge=True, fetch=fetch)
    judge = next(ln for ln in est.lines if ln.role == "judge")
    observe = next(ln for ln in est.lines if ln.role == "observe")
    assert judge.assumed and (judge.tokens_in, judge.tokens_out) == cg.FALLBACK_TOKENS["judge"] and "fixed table" in judge.basis
    assert observe.assumed and (observe.tokens_in, observe.tokens_out) == cg.FALLBACK_TOKENS["observe"]


def test_a_role_not_served_by_openrouter_is_not_on_the_key_and_is_left_out():
    est = cg.estimate_run(settings(agent_provider="groq"), 1, TOKENS, include_judge=True, fetch=fetch)
    assert {ln.role for ln in est.lines} == {"judge"} and any("served by groq" in t for t in est.skipped)


def test_agents_only_and_judge_only():
    assert {ln.role for ln in cg.estimate_run(settings(), 1, TOKENS, include_judge=False, fetch=fetch).lines} == {"router", "knowledge", "verify"}
    assert {ln.role for ln in cg.estimate_run(settings(), 1, TOKENS, include_agents=False, fetch=fetch).lines} == {"judge"}


# ---- the guard around a run ----

def guard(tmp_path, usage=0.04, limit=None, **kw):
    reads = {"n": 0}
    out = []

    def read_key():
        reads["n"] += 1
        return {"usage": usage if not callable(usage) else usage(reads["n"]), "limit": limit}

    clock = Clock()
    g = cg.CostGuard(kw.pop("s", settings()), read_key=read_key, fetch=fetch, reports_dir=tmp_path, ledger_path=tmp_path / "ledger.json", md_path=tmp_path / "LEDGER.md",
                     sleep=clock.sleep, out=out.append, **kw)
    return g, out, reads


def seed_runs(tmp_path):
    write_run(tmp_path / "20261115-000001.json", [row([("router", "openai/gpt-oss-120b", 1000, 100), ("knowledge", "openai/gpt-oss-120b", 2000, 200),
                                                      ("verify", "openai/gpt-oss-120b", 1500, 500)], judge=[("qwen/qwen3.8-27b", 1500, 100)])])


def test_a_run_that_fits_starts_and_the_output_shows_the_estimate_and_the_warning(tmp_path):
    seed_runs(tmp_path)
    g, out, _ = guard(tmp_path)
    ticket = g.start(block="B", command="cmd", n_case_runs=10)
    text = "\n".join(out)
    assert ticket is not None and ticket.before == 0.04 and "-> OK" in text and "pinned host groq" in text and "NO limit" in text


def test_a_run_that_does_not_fit_does_not_start_and_says_why(tmp_path):
    seed_runs(tmp_path)
    g, out, _ = guard(tmp_path, usage=8.45)
    with pytest.raises(cg.CostGuardAbort) as exc:
        g.start(block="B", command="cmd", n_case_runs=1000)
    assert "COST GUARD" in str(exc.value) and "The run did not start" in str(exc.value) and "-> ABORT" in "\n".join(out)


def test_it_is_the_keys_counter_that_decides_not_what_the_runs_reported(tmp_path):
    seed_runs(tmp_path)
    g, _, reads = guard(tmp_path, usage=8.49)  # the counter says almost nothing is left
    with pytest.raises(cg.CostGuardAbort):
        g.start(block="B", command="cmd", n_case_runs=100)
    assert reads["n"] >= 1


def test_without_a_readable_counter_the_run_does_not_start(tmp_path):
    seed_runs(tmp_path)

    def broken():
        raise cg.CostGuardAbort("cannot read the key's usage counter")

    g = cg.CostGuard(settings(), read_key=broken, fetch=fetch, reports_dir=tmp_path, ledger_path=tmp_path / "l.json", md_path=tmp_path / "L.md", out=lambda *_: None)
    with pytest.raises(cg.CostGuardAbort, match="usage counter"):
        g.start(block="B", command="cmd", n_case_runs=1)


def test_a_run_with_no_role_on_openrouter_is_not_guarded(tmp_path):
    g, out, reads = guard(tmp_path, s=settings(agent_provider="groq", judge_provider="groq"))
    assert g.start(block="B", command="cmd", n_case_runs=10) is None and reads["n"] == 0 and "not applicable" in out[0]


def test_finish_writes_the_estimate_against_the_real_cost_from_the_settled_counter(tmp_path):
    seed_runs(tmp_path)
    g, out, _ = guard(tmp_path, usage=lambda n: 0.04 if n <= 2 else 0.1000)  # the counter moves after the run (it lags): start reads 0.04, then it settles at 0.10
    ticket = g.start(block="final", command="python -m eval.run_eval", n_case_runs=10)
    entry = g.finish(ticket, reported_cost_usd=0.03)
    assert entry["real_usd"] == pytest.approx(0.06) and entry["settled"] and entry["spent_before_usd"] == 0.04 and entry["counter_after_usd"] == pytest.approx(0.10)
    assert entry["estimate_usd"] == pytest.approx(ticket.estimate.total_usd) and entry["reported_cost_usd"] == 0.03 and entry["outcome"] == "completed"
    ledger = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    assert len(ledger) == 1 and ledger[0]["n"] == 1 and (tmp_path / "LEDGER.md").exists() and "estimate US$" in "\n".join(out)


def test_a_failed_run_is_recorded_as_failed(tmp_path):
    seed_runs(tmp_path)
    g, _, _ = guard(tmp_path)
    entry = g.finish(g.start(block="B", command="cmd", n_case_runs=1), outcome="failed")
    assert entry["outcome"] == "failed"


# ---- the ledger ----

def entry(n, block, est, real, settled=True, reported=None, text=None):
    return {"n": n, "at_utc": "2026-11-19 22:00:00", "block": block, "command": "cmd", "estimate_usd": est, "real_usd": real, "settled": settled,
            "reported_cost_usd": reported, "assumed_roles": [], **({"text": text} if text else {})}


def test_the_ledger_states_the_rule_the_deviation_and_its_reason():
    text = cg.render_ledger([])
    assert "spent + 1.5 x estimate > 8.5" in text and "The plan said" in text and "2026-11-19" in text and "counter" in text and "_No entries yet._" in text


def test_the_ledger_sums_per_block_and_calibrates_the_factor():
    entries = [entry(1, "M1", None, 0.042, text="before the guard"), entry(2, "C1", 1.0, 1.2, reported=0.6), entry(3, "C1", 1.0, 1.8), entry(4, "C3", 1.0, 2.0, settled=False)]
    text = cg.render_ledger(entries)
    assert "| M1 | US$0.0420 |" in text and "| C1 | US$3.0000 |" in text and "| C3 | US$2.0000 |" in text and "US$5.0420" in text
    assert "Runs with an estimate and a settled real cost: 2" in text and "median 1.50, largest 1.80" in text and "the factor is too low" in text
    assert "real / reported" in text and "2.00" in text and "- #1 (M1): before the guard" in text  # real/reported = 1.2 / 0.6
    assert "| no |" in text  # the unsettled one is marked


def test_the_ledger_with_every_ratio_under_the_factor_says_so():
    assert "every run stayed under it" in cg.render_ledger([entry(1, "B", 1.0, 1.2)])
    assert "not calibrated" in cg.render_ledger([entry(1, "B", 1.0, 1.2, settled=False)])


def test_note_records_a_spend_outside_a_guarded_run(tmp_path, monkeypatch):
    monkeypatch.setattr(cg, "LEDGER_JSON", tmp_path / "cost_ledger.json")
    monkeypatch.setattr(cg, "LEDGER_MD", tmp_path / "COST_LEDGER.md")
    assert cg.main(["note", "--block", "M1", "--real", "0.042", "--text", "V2, smokes and the host measurement, before the guard"]) == 0
    saved = json.loads((tmp_path / "cost_ledger.json").read_text(encoding="utf-8"))
    assert saved[0]["real_usd"] == 0.042 and saved[0]["outcome"] == "note" and "before the guard" in (tmp_path / "COST_LEDGER.md").read_text(encoding="utf-8")
    assert cg.main(["ledger"]) == 0


# ---- how it is wired ----

def test_run_eval_calls_the_guard_before_building_the_service_and_has_the_flag():
    src = (ROOT / "eval" / "run_eval.py").read_text(encoding="utf-8")
    assert '"--no-cost-guard"' in src and src.index("guard.start(") < src.index("service = ChatService(build_default_graph") and "guard.finish(" in src
    assert 'outcome="failed"' in src


def test_rejudge_calls_the_guard_for_the_judge_only_and_the_daily_limit_is_unbounded_off_groq():
    from eval import rejudge

    src = (ROOT / "eval" / "rejudge.py").read_text(encoding="utf-8")
    assert "include_agents=False" in src and '"--no-cost-guard"' in src and rejudge.NO_DAILY_LIMIT >= 10**9
    assert 'DAILY_LIMIT if get_settings().judge_provider.lower() == "groq"' in src


def test_the_host_harness_has_its_own_cap_and_does_not_start_a_guarded_run_inside():
    assert '"--no-cost-guard"' in (ROOT / "eval" / "host_latency.py").read_text(encoding="utf-8")
