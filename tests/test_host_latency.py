"""The host-choice rule of docs/PREREG_PLAN96.md (amendment 1) and its harness, with no network."""
import json

import httpx
import pytest

from eval import host_latency as hl


def endpoint(tag, quant="bf16", uptime=100.0, tools=True, prompt=0.00000003, completion=0.00000015, name=None):
    return {"tag": tag, "provider_name": name or tag.split("/")[0].title(), "quantization": quant, "uptime_last_30m": uptime,
            "supported_parameters": ["tools", "max_tokens"] if tools else ["max_tokens"], "pricing": {"prompt": str(prompt), "completion": str(completion)}}


# ---- the candidate filter ----

@pytest.mark.parametrize("ep, ok", [
    (endpoint("a/bf16"), True), (endpoint("a/fp16", quant="fp16"), True), (endpoint("a", quant=None), True), (endpoint("a", quant="unknown"), True),
    (endpoint("a/fp4", quant="fp4"), False), (endpoint("a/int4", quant="int4"), False), (endpoint("a/fp8", quant="fp8"), False),
    (endpoint("a", tools=False), False), (endpoint("a", uptime=98.9), False), (endpoint("a", uptime=99.0), True), (endpoint("a", uptime=None), False),
])
def test_the_candidate_filter(ep, ok):
    assert hl.eligibility(ep)[0] is ok


def test_candidates_are_one_per_tag_cheapest_listed_first():
    eps = [endpoint("dear", prompt=1e-6, completion=1e-6), endpoint("cheap", prompt=1e-8, completion=1e-8), endpoint("cheap", prompt=1e-8, completion=1e-8),
           endpoint("bad/fp4", quant="fp4", prompt=1e-9, completion=1e-9), endpoint("mid", prompt=1e-7, completion=1e-7)]
    assert [c["tag"] for c in hl.candidates(eps)] == ["cheap", "mid", "dear"]


# ---- the rule ----

def host(tag, p50, cost, tools_work=True, measured=True):
    return {"tag": tag, "measured": measured, "tools_work": tools_work, "p50_ms": p50, "mean_cost_usd": cost}


def test_the_cheapest_host_within_one_and_a_half_times_the_fastest_wins():
    hosts = [host("fast", 1000, 0.0010), host("cheap_but_slow", 1600, 0.0001), host("cheap_enough", 1500, 0.0004), host("dear", 1000, 0.0020)]
    d = hl.decide(hosts)
    assert d["winner"] == "cheap_enough" and d["fastest"] == "fast"  # 1500 is exactly 1.5x: inside; 1600 is outside
    assert d["within"] == ["fast", "cheap_enough", "dear"]


def test_a_host_whose_tools_fail_never_sets_the_bar_and_never_wins():
    hosts = [host("broken_but_fast", 100, 0.0001, tools_work=False), host("ok", 1000, 0.0010), host("ok_slower", 1400, 0.0005)]
    d = hl.decide(hosts)
    assert d["fastest"] == "ok" and d["winner"] == "ok_slower"


def test_costs_within_five_percent_are_a_tie_and_the_faster_wins():
    hosts = [host("a", 1200, 0.00100), host("b", 1000, 0.001049), host("c", 1100, 0.00106)]
    assert hl.decide(hosts)["winner"] == "b"  # a and b tie (b is 4.9 % above a), b is faster; c is 6 % above
    hosts = [host("a", 1200, 0.00100), host("b", 1000, 0.001051)]
    assert hl.decide(hosts)["winner"] == "a"  # 5.1 %: not a tie, the cheaper wins


def test_with_no_host_in_the_pool_the_default_stays():
    d = hl.decide([host("x", 1000, 0.001, tools_work=False), host("y", None, None), host("z", 1, 1, measured=False)])
    assert d["winner"] is None and d["kept"] == hl.DEFAULT_HOST
    assert hl.decide([])["kept"] == hl.DEFAULT_HOST


def test_a_single_host_in_the_pool_wins_by_default():
    assert hl.decide([host("only", 5000, 0.01)])["winner"] == "only"


def test_the_reference_is_measured_last_never_decides_and_cannot_win():
    eps = [endpoint("cand/bf16", prompt=2e-7, completion=2e-7), endpoint(hl.DEFAULT_HOST, uptime=96.7, prompt=1e-9, completion=1e-9)]
    assert [(e["tag"], ref) for e, ref in hl.plan_hosts(eps)] == [("cand/bf16", False), (hl.DEFAULT_HOST, True)]
    ref = {**host(hl.DEFAULT_HOST, 100, 0.00001), "reference": True}
    d = hl.decide([host("cand/bf16", 5000, 0.001), ref])
    assert d["winner"] == "cand/bf16" and d["fastest"] == "cand/bf16"  # the reference is faster and cheaper and still sets no bar
    assert hl.decide([ref])["kept"] == hl.DEFAULT_HOST and hl.decide([ref])["winner"] is None


def test_the_default_host_is_one_measurement_when_it_is_a_candidate_and_no_reference_when_it_does_not_exist():
    eps = [endpoint(hl.DEFAULT_HOST, uptime=100.0), endpoint("cand/bf16")]
    assert [ref for _, ref in hl.plan_hosts(eps)] == [False, False] and sum(1 for e, _ in hl.plan_hosts(eps) if e["tag"] == hl.DEFAULT_HOST) == 1
    assert hl.plan_hosts([endpoint("cand/bf16")]) == [(endpoint("cand/bf16"), False)]


# ---- the incumbent (amendment 1b and its clarification): it stays if it passes, another replaces it only if it fails ----

def test_the_incumbent_stays_when_it_passes_even_if_another_host_is_cheaper_and_faster():
    hosts = [host("incumbent", 1400, 0.0010), host("fast_cheap", 1000, 0.0002)]
    d = hl.decide(hosts, "incumbent")
    assert d["winner"] == "incumbent" and d["incumbent_passes"] is True and d["unconditional_winner"] == "fast_cheap" and "it stays" in d["reason"]
    assert hl.decide(hosts)["winner"] == "fast_cheap"  # without an incumbent the rule picks the other one


def test_the_incumbent_at_exactly_one_and_a_half_times_the_fastest_still_passes():
    assert hl.decide([host("inc", 1500, 0.01), host("fast", 1000, 0.0001)], "inc")["winner"] == "inc"
    assert hl.decide([host("inc", 1501, 0.01), host("fast", 1000, 0.0001)], "inc")["winner"] == "fast"


def test_the_incumbent_whose_tools_fail_is_replaced_by_the_rules_choice_among_the_others():
    hosts = [host("inc", 100, 0.0001, tools_work=False), host("dear", 1000, 0.0020), host("cheap_enough", 1200, 0.0005), host("too_slow", 1600, 0.0001)]
    d = hl.decide(hosts, "inc")
    assert d["winner"] == "cheap_enough" and d["incumbent_passes"] is False and "tools do not work" in d["reason"]


def test_a_too_slow_incumbent_is_replaced_and_the_bar_is_the_fastest_of_the_hosts_that_pass():
    d = hl.decide([host("inc", 3000, 0.0001), host("fast", 1000, 0.0010), host("mid", 1400, 0.0005)], "inc")
    assert d["winner"] == "mid" and "above 1.5x the fastest (fast)" in d["reason"]


def test_an_incumbent_that_was_not_a_candidate_or_not_measured_fails_the_rule():
    ref = {**host("inc", 100, 0.00001), "reference": True}
    assert "not a candidate that day" in hl.decide([ref, host("other", 1000, 0.001)], "inc")["reason"]
    assert hl.decide([ref, host("other", 1000, 0.001)], "inc")["winner"] == "other"
    d = hl.decide([host("other", 1000, 0.001)], "absent")
    assert d["winner"] == "other" and "not measured" in d["reason"]


def test_if_the_incumbent_fails_and_nothing_else_passes_it_stays():
    d = hl.decide([host("inc", 100, 0.001, tools_work=False), host("x", 100, 0.001, tools_work=False)], "inc")
    assert d["winner"] is None and d["kept"] == "inc" and "no other host passes" in d["reason"]
    assert hl.decide([], "inc")["kept"] == "inc"


def test_no_incumbent_is_the_unconditional_rule_of_the_19th():
    hosts = [host("a", 1000, 0.002), host("b", 1100, 0.001)]
    assert hl.decide(hosts) == hl.decide(hosts, None) and hl.decide(hosts)["winner"] == "b"


def test_the_incumbent_defaults_to_the_pinned_host_and_none_turns_it_off(tmp_path, monkeypatch, capsys):
    from app.config import get_settings

    monkeypatch.setenv("OPENROUTER_PROVIDER_ORDER", '{"openai/gpt-oss-120b": ["crusoe/bf16"]}')
    get_settings.cache_clear()
    assert hl.default_incumbent() == "crusoe/bf16" and hl.default_incumbent("qwen/qwen3.8-27b") is None
    saved = tmp_path / "m.json"
    saved.write_text(json.dumps({"hosts": [host("crusoe/bf16", 1400, 0.001), host("faster", 1000, 0.0002)]}), encoding="utf-8")
    assert hl.main(["--decide", str(saved)]) == 0
    assert json.loads(capsys.readouterr().out)["winner"] == "crusoe/bf16"  # the incumbent of the settings passes
    assert hl.main(["--decide", str(saved), "--incumbent", "none"]) == 0
    assert json.loads(capsys.readouterr().out)["winner"] == "faster"
    assert hl.main(["--decide", str(saved), "--incumbent", "faster"]) == 0
    assert json.loads(capsys.readouterr().out)["winner"] == "faster"
    get_settings.cache_clear()


def test_the_incumbent_is_measured_as_a_reference_when_the_filter_leaves_it_out():
    eps = [endpoint("cand/bf16"), endpoint("inc", uptime=90.0), endpoint(hl.DEFAULT_HOST, uptime=90.0)]
    plan = [(e["tag"], ref) for e, ref in hl.plan_hosts(eps, "inc")]
    assert plan == [("cand/bf16", False), (hl.DEFAULT_HOST, True), ("inc", True)]
    assert [(e["tag"], r) for e, r in hl.plan_hosts([endpoint("inc"), endpoint("cand/bf16")], "inc")] == [("inc", False), ("cand/bf16", False)]  # a candidate is measured once, not as a reference


def test_the_report_says_whether_the_incumbent_passes_and_what_the_unconditional_winner_would_have_been():
    hosts = [hl.summarize_host(endpoint("inc", name="Inc"), ten_calls([1400] * 10), Q01_OK), hl.summarize_host(endpoint("fast", name="Fast"), ten_calls([1000] * 10), Q01_OK)]
    report = {"at_utc": "2026-11-21 10:00:00", "commit": "a" * 40, "tree_clean": True, "cap_usd": 0.1, "spent_usd": 0.01, "endpoints": [], "candidates": [], "hosts": hosts,
              "decision": hl.decide(hosts, "inc")}
    text = hl.render(report)
    assert "Incumbent (amendment 1b): `inc`, **passes the rule and stays**" in text and "would have been `fast`" in text


def test_the_spending_cap():
    assert hl.can_afford(0.0, 0.01) and hl.can_afford(0.09, 0.01) and not hl.can_afford(0.095, 0.01)


# ---- the requests ----

@pytest.fixture(scope="module")
def bodies():
    return hl.build_bodies()


def test_there_are_ten_bodies_five_router_and_five_knowledge_on_the_regular_cases(bodies):
    assert [b["kind"] for b in bodies] == ["router"] * 5 + ["knowledge"] * 5
    assert [b["case"] for b in bodies] == list(hl.CASES) * 2
    assert not any(c.startswith("ho") or c.startswith("hv3") for c in hl.CASES)


def test_the_router_body_forces_a_routing_decision_with_the_real_prompt(bodies):
    from app.prompts import ROUTER_PROMPT

    body = bodies[0]["body"]
    assert body["tool_choice"]["function"]["name"] == "RoutingDecision" and [t["function"]["name"] for t in body["tools"]] == ["RoutingDecision"]
    assert body["messages"][0]["content"] == ROUTER_PROMPT and "Get Cl" in body["messages"][1]["content"]  # the message of q01


def test_the_knowledge_body_has_the_three_tools_and_the_real_prompt(bodies):
    body = bodies[5]["body"]
    assert [t["function"]["name"] for t in body["tools"]] == list(hl.KNOWLEDGE_TOOLS) and body["tool_choice"] == "auto"
    from app.agents.common import format_prior_context
    from app.prompts import KNOWLEDGE_PROMPT

    assert body["messages"][0]["content"] == KNOWLEDGE_PROMPT.format(language="English", prior_context=format_prior_context({}))
    assert body["model"] == hl.MODEL and body["reasoning"] == {"effort": "low"}


def test_pin_sets_the_host_and_forbids_fallbacks_without_touching_the_original(bodies):
    original = bodies[0]["body"]
    pinned = hl.pin(original, "crusoe/bf16")
    assert pinned["provider"]["order"] == ["crusoe/bf16"] and pinned["provider"]["allow_fallbacks"] is False
    assert original["provider"]["order"] != ["crusoe/bf16"]


def test_no_body_carries_a_key_or_a_header(bodies):
    text = json.dumps(bodies)
    assert "Authorization" not in text and "Bearer" not in text and "sk-or" not in text


# ---- what a call must do, and what is recorded ----

ROUTER_ARGS = json.dumps({"reasoning": "kb", "agents": ["knowledge"], "language": "English", "wants_human": False})


@pytest.mark.parametrize("kind, name, args, ok", [
    ("router", "RoutingDecision", ROUTER_ARGS, True), ("router", "RoutingDecision", json.dumps({"agents": ["nobody"], "reasoning": "x"}), False),
    ("router", "other", ROUTER_ARGS, False), ("router", "RoutingDecision", "{not json", False), ("router", None, None, False),
    ("knowledge", "search_getnet_kb", '{"query": "plans"}', True), ("knowledge", "web_search", "{}", True), ("knowledge", "drop_database", "{}", False),
    ("knowledge", "search_getnet_kb", "[1]", False), ("knowledge", "search_getnet_kb", "nope", False),
])
def test_call_ok(kind, name, args, ok):
    assert hl.call_ok(kind, name, args) is ok


@pytest.mark.parametrize("answered, expected, ok", [("Crusoe", "Crusoe", True), ("Amazon Bedrock", "Amazon Bedrock", True), ("amazon-bedrock", "Amazon Bedrock", True),
                                                    ("Together", "Crusoe", False), (None, "Crusoe", False), ("Crusoe", None, False)])
def test_same_host(answered, expected, ok):
    assert hl.same_host(answered, expected) is ok


def client_returning(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def response_of(host="Crusoe", name="RoutingDecision", args=ROUTER_ARGS, cost=0.0002):
    return {"id": "gen-1", "provider": host, "usage": {"completion_tokens": 30, "cost": cost},
            "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{"function": {"name": name, "arguments": args}}]}}]}


SPEC = {"kind": "router", "case": "q01", "body": {"model": hl.MODEL, "messages": [{"role": "user", "content": "SECRET QUESTION TEXT"}]}}
EP = endpoint("crusoe/bf16", name="Crusoe")


def test_a_good_call_is_recorded_without_the_key_a_header_or_the_text():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=response_of())

    r = hl.measure_call(client_returning(handler), "sk-or-the-key", SPEC, EP)
    assert seen["auth"] == "Bearer sk-or-the-key" and seen["body"]["provider"] == {"order": ["crusoe/bf16"], "allow_fallbacks": False}
    assert r["tool_ok"] and r["host_ok"] and r["status"] == 200 and r["cost_usd"] == 0.0002 and r["latency_ms"] >= 0
    dumped = json.dumps(r)
    assert "sk-or" not in dumped and "Bearer" not in dumped and "SECRET QUESTION" not in dumped


def test_a_response_from_another_host_does_not_count_for_the_candidate():
    r = hl.measure_call(client_returning(lambda req: httpx.Response(200, json=response_of(host="Together"))), "k", SPEC, EP)
    assert r["tool_ok"] and not r["host_ok"]


def test_errors_are_data_not_exceptions():
    r = hl.measure_call(client_returning(lambda req: httpx.Response(429, json={"error": {"message": "rate limited"}})), "k", SPEC, EP)
    assert r["status"] == 429 and "rate limited" in r["error"] and not r["tool_ok"]

    def timeout(req):
        raise httpx.ReadTimeout("slow")

    r = hl.measure_call(client_returning(timeout), "k", SPEC, EP)
    assert r["error"] == "timeout" and r["latency_ms"] is not None and not r["tool_ok"]
    r = hl.measure_call(client_returning(lambda req: httpx.Response(200, content=b"<html>")), "k", SPEC, EP)
    assert r["error"] == "JSONDecodeError" and not r["tool_ok"]


def test_a_call_is_made_once_with_no_retry():
    count = {"n": 0}

    def handler(request):
        count["n"] += 1
        return httpx.Response(503, json={"error": {"message": "down"}})

    hl.measure_call(client_returning(handler), "k", SPEC, EP)
    assert count["n"] == 1


# ---- the summary of a host ----

def ten_calls(latencies, ok=True, host_ok=True):
    return [{"latency_ms": ms, "tool_ok": ok, "host_ok": host_ok, "error": None, "cost_usd": 0.0002} for ms in latencies]


Q01_OK = {"ok": True, "latencies_ms": [5000, 6000], "cost_usd": 0.0007}


def test_summarize_host_takes_the_p50_of_all_ten_calls_and_the_mean_real_cost():
    h = hl.summarize_host(EP, ten_calls([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]), Q01_OK)
    assert h["p50_ms"] == 550 and h["max_ms"] == 1000 and h["mean_cost_usd"] == pytest.approx(0.0002) and h["tools_work"] is True
    assert h["spent_usd"] == pytest.approx(0.002 + 0.0007)


@pytest.mark.parametrize("calls, q01", [
    (ten_calls([1] * 10, ok=False), Q01_OK), (ten_calls([1] * 10, host_ok=False), Q01_OK), (ten_calls([1] * 9), Q01_OK), (ten_calls([1] * 10), {"ok": False}),
])
def test_tools_work_needs_every_call_the_pinned_host_and_q01(calls, q01):
    assert hl.summarize_host(EP, calls, q01)["tools_work"] is False


def test_a_timeout_counts_in_the_p50_with_the_time_it_took_and_an_error_breaks_the_tools():
    calls = ten_calls([1000] * 9) + [{"latency_ms": 120000, "tool_ok": False, "host_ok": False, "error": "timeout", "cost_usd": None}]
    h = hl.summarize_host(EP, calls, Q01_OK)
    assert h["p50_ms"] == 1000 and h["max_ms"] == 120000 and h["calls_error"] == 1 and h["tools_work"] is False


def fake_run_eval(monkeypatch, tmp_path, rows_of_each_run):
    """eval.run_eval replaced by a function that writes the JSON files a run would write."""
    monkeypatch.setattr(hl, "REPORTS", tmp_path)

    def run(cmd, **_kw):
        label = cmd[cmd.index("--label") + 1]
        for i, row in enumerate(rows_of_each_run):
            summary = {"label": label, "provider_errors": 1 if "provider_error" in row else 0}
            (tmp_path / f"20261119-00000{i}.json").write_text(json.dumps({"summary": summary, "rows": [row]}), encoding="utf-8")
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(hl.subprocess, "run", run)


GOOD_ROW = {"id": "q01_compare_plans", "latency_ms": 5000, "plan": ["knowledge"], "tools_called": ["search_getnet_kb"], "degraded": False,
            "usage": {"cost_usd": 0.0004, "by_node": [{"node": "router", "host": "Crusoe"}]}}


def test_q01_with_a_provider_error_is_data_not_a_crash(monkeypatch, tmp_path):
    error_row = {"id": "q01_compare_plans", "latency_ms": 3800, "provider_error": "router: OpenAIModelNotFoundError: Error code: 404 - No endpoints found ... Filter by Tool Compatibility"}
    fake_run_eval(monkeypatch, tmp_path, [error_row, error_row])
    out = hl.run_q01(EP)
    assert out["ran"] and out["ok"] is False and out["cost_usd"] == 0 and out["latencies_ms"] == [3800, 3800] and out["hosts"] == []
    assert len(out["errors"]) == 2 and "404" in out["errors"][0]


def test_q01_that_runs_twice_on_the_pinned_host_with_the_base_plan_and_tool_is_ok(monkeypatch, tmp_path):
    fake_run_eval(monkeypatch, tmp_path, [GOOD_ROW, GOOD_ROW])
    out = hl.run_q01(EP)
    assert out["ok"] is True and out["hosts"] == ["Crusoe"] and out["cost_usd"] == pytest.approx(0.0008) and out["latencies_ms"] == [5000, 5000] and out["errors"] == []


@pytest.mark.parametrize("change", [{"plan": ["support"]}, {"tools_called": []}, {"degraded": True},
                                    {"usage": {"cost_usd": 0.0004, "by_node": [{"node": "router", "host": "Together"}]}}])
def test_q01_is_not_ok_when_the_plan_the_tool_or_the_host_differ(monkeypatch, tmp_path, change):
    fake_run_eval(monkeypatch, tmp_path, [GOOD_ROW, {**GOOD_ROW, **change}])
    assert hl.run_q01(EP)["ok"] is False


def test_notes_written_by_hand_are_kept_in_the_json_and_shown_and_the_header_says_so(tmp_path, monkeypatch):
    h = hl.summarize_host(EP, ten_calls([100] * 10), Q01_OK)
    report = {"at_utc": "2026-11-19 22:00:00", "commit": "a" * 40, "tree_clean": True, "cap_usd": 0.1, "spent_usd": 0.0027, "endpoints": [], "candidates": [],
              "hosts": [h], "decision": hl.decide([h])}
    plain = hl.render(report)
    assert "Nothing below is typed by hand._" in plain and "## Notes" not in plain
    report["notes"] = ["an earlier attempt was deleted"]
    text = hl.render(report)
    assert "except the Notes at the end" in text and "## Notes (written by hand after the run)" in text and "- an earlier attempt was deleted" in text
    saved = tmp_path / "HOST-LATENCY-x.json"
    saved.write_text(json.dumps(report), encoding="utf-8")
    assert hl.main(["--rerender", str(saved)]) == 0
    assert (tmp_path / "HOST-LATENCY-x.md").read_text(encoding="utf-8").strip() == text.strip()


def test_the_report_is_generated_from_the_measurement_and_names_the_winner():
    h = hl.summarize_host(EP, ten_calls([100] * 10), Q01_OK)
    endpoints = [endpoint("crusoe/bf16", name="Crusoe"), endpoint("x/fp4", quant="fp4")]
    report = {"at_utc": "2026-11-19 22:00:00", "commit": "a" * 40, "tree_clean": True, "cap_usd": 0.1, "spent_usd": 0.0027, "endpoints": hl.endpoint_rows(endpoints),
              "candidates": ["crusoe/bf16"], "hosts": [h, {"tag": "late/bf16", "measured": False, "why_not_measured": "cap"}], "decision": hl.decide([h])}
    text = hl.render(report)
    assert "**Winner: `crusoe/bf16`**" in text and "| `late/bf16` | cap |" in text and "quantization fp4" in text and "sk-or" not in text
    assert "US$0.0027" in text and "`aaaaaaaaaaaa`" in text
