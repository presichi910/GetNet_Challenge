"""EVAL_DATASET / --dataset: run (and judge) another cases file with the schema of eval/dataset.jsonl. Unset, `load_cases` returns exactly what it always did,
the report of a normal run has the keys it always had, and latest.md is replaced under the same conditions as before."""
import json
from pathlib import Path

import pytest

from eval import run_eval
from eval.run_eval import ROOT, custom_dataset, dataset_path, load_cases, run_config, should_update_latest

REPO = Path(__file__).resolve().parent.parent
PROBE = REPO / "eval" / "probe" / "replan_probe.jsonl"


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv("EVAL_DATASET", raising=False)


def test_unset_or_blank_load_cases_returns_exactly_the_dataset_as_before(monkeypatch):
    expected = lines(ROOT / "dataset.jsonl")
    assert custom_dataset() is None and dataset_path() == ROOT / "dataset.jsonl"
    assert load_cases(None) == expected
    assert load_cases(["inj"]) == [c for c in expected if c["id"].startswith("inj")]
    assert load_cases(None, regular_only=True) == [c for c in expected if not c.get("holdout")]
    monkeypatch.setenv("EVAL_DATASET", "   ")  # blank is unset
    assert custom_dataset() is None and load_cases(None) == expected


def test_a_path_in_the_variable_is_what_is_loaded_with_the_same_filters(monkeypatch, tmp_path):
    f = tmp_path / "cases.jsonl"
    cases = [{"id": "z1_a", "message": "m1"}, {"id": "z2_b", "message": "m2"}, {"id": "ho99_c", "message": "m3", "holdout": True}]
    f.write_text("".join(json.dumps(c) + "\n" for c in cases) + "\n", encoding="utf-8")
    monkeypatch.setenv("EVAL_DATASET", str(f))
    assert custom_dataset() == f and load_cases(None) == cases
    assert load_cases(["z2"]) == [cases[1]] and load_cases(None, regular_only=True) == cases[:2]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EVAL_DATASET", "cases.jsonl")  # relative to where it is run
    assert load_cases(None) == cases


def test_a_variable_that_points_nowhere_stops_the_run_it_does_not_fall_back_to_the_dataset(monkeypatch, tmp_path):
    monkeypatch.setenv("EVAL_DATASET", str(tmp_path / "nope.jsonl"))
    with pytest.raises(SystemExit, match="does not exist"):
        load_cases(None)


def test_the_run_config_of_a_normal_run_has_the_keys_it_always_had_and_the_dataset_only_when_it_is_not_the_default(monkeypatch):
    base = {"commit", "tree_clean", "replan_max", "rag_mode", "verifier_mode", "approvals", "fallback_provider",
            "agent_provider", "judge_provider", "openrouter_provider_order", "openrouter_allow_fallbacks"}
    assert set(run_config()) == base and set(run_config("replan on")) == base | {"label"}
    monkeypatch.setenv("EVAL_DATASET", str(PROBE))
    assert run_config()["dataset"] == "replan_probe.jsonl" and set(run_config()) == base | {"dataset"}


def test_latest_md_is_replaced_only_by_a_full_unlabeled_run_on_the_default_dataset(monkeypatch):
    assert should_update_latest(None, False, None) is True
    assert not should_update_latest(["q01"], False, None) and not should_update_latest(None, True, None) and not should_update_latest(None, False, "replan on")
    monkeypatch.setenv("EVAL_DATASET", str(PROBE))
    assert should_update_latest(None, False, None) is False  # a run on the probe never replaces the report of the main run


def test_the_flag_is_in_the_command_line_and_sets_the_variable_the_judge_tool_reads():
    src = Path(run_eval.__file__).read_text(encoding="utf-8")
    assert '"--dataset"' in src and 'os.environ["EVAL_DATASET"]' in src


def test_same_behavior_does_not_read_a_dataset_so_it_cannot_change():
    src = (REPO / "eval" / "same_behavior.py").read_text(encoding="utf-8")
    assert "load_cases" not in src and "EVAL_DATASET" not in src and "dataset.jsonl" not in src


def test_rejudge_judges_probe_rows_with_their_own_criteria_when_the_variable_is_set_and_skips_them_when_it_is_not(monkeypatch, tmp_path):
    from eval.rejudge import load_rows, score_file
    from tests.test_rejudge import FakeJudge, scorer

    probe = lines(PROBE)
    rows = [{"id": c["id"], "message": c["message"], "answer": f"answer {c['id']}", "evidence": ["e"], "plan": [], "tools_called": [], "replans": [], "routing_ok": True, "tools_ok": True, "handoff_ok": None, "latency_ms": 5, "degraded": False}
            for c in probe[:3]]
    path = tmp_path / "probe_run.json"
    path.write_text(json.dumps({"summary": {}, "rows": rows}), encoding="utf-8")

    class Recording(FakeJudge):
        def score(self, question, answer, evidence, criteria="", callbacks=None):
            self.seen = [*getattr(self, "seen", []), criteria]
            return super().score(question, answer, evidence, criteria, callbacks)

    said: list[str] = []
    sc, *_ = scorer(tmp_path, Recording([]))
    score_file(str(path), sc, {c["id"]: c for c in load_cases(None)}, print_fn=said.append)  # no variable: the dataset has no such ids
    assert sum("not in the dataset, skipped" in s for s in said) == 3
    assert all("judge" not in r for r in load_rows(path)[1])  # nothing was scored, and nothing stopped: the silent omission the preflight exists for

    monkeypatch.setenv("EVAL_DATASET", str(PROBE))
    judge = Recording([])
    sc, *_ = scorer(tmp_path, judge)
    said.clear()
    assert score_file(str(path), sc, {c["id"]: c for c in load_cases(None)}, print_fn=said.append) == "done"
    assert not any("not in the dataset" in s for s in said) and judge.seen == [c["criteria"] for c in probe[:3]]
    assert [(r["judge"] or {}).get("verdict") for r in load_rows(path)[1]] == ["pass"] * 3


def test_with_holdout_v3_appends_its_cases_in_their_own_group_and_default_is_unchanged(monkeypatch):
    from eval.run_eval import HOLDOUT_V3, holdout_group, summarize_all, with_holdout_v3

    base = lines(ROOT / "dataset.jsonl")
    monkeypatch.delenv("EVAL_WITH_HOLDOUT_V3", raising=False)
    assert not with_holdout_v3() and load_cases(None) == base and "with_holdout_v3" not in run_config()
    monkeypatch.setenv("EVAL_WITH_HOLDOUT_V3", "1")
    v3 = lines(HOLDOUT_V3)
    assert load_cases(None) == base + v3 and len(v3) == 20
    assert load_cases(None, regular_only=True) == [c for c in base if not c.get("holdout")]  # the v3 cases are holdout: --regular-only leaves them out
    assert load_cases(["hv3_0"]) == [c for c in v3 if c["id"].startswith("hv3_0")]
    assert {holdout_group(c["id"]) for c in v3} == {"v3"} and holdout_group("ho07_x") == "blind" and holdout_group("ho01_x") == "original"
    assert run_config()["with_holdout_v3"] is True
    base_row = {"routing_ok": True, "tools_ok": True, "latency_ms": 1000, "degraded": False}
    rows = [{**base_row, "id": "q01"}, {**base_row, "id": "hv3_01", "holdout": True, "routing_ok": False},
            {**base_row, "id": "ho07", "holdout": True}]
    groups = summarize_all(rows)["holdout_groups"]
    assert set(groups) == {"blind", "v3"} and groups["v3"]["cases"] == 1 and groups["v3"]["routing_accuracy"] == 0.0
