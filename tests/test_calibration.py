"""Judge calibration tools: export of a run to a labels CSV and agreement metrics. No network."""
import csv
import json

import pytest

from eval.calibration.agreement import agreement, cohen_kappa, confusion, mae, render, to_bool
from eval.calibration.export import COLUMNS, to_label_rows, write_csv


def pairs(tp, fp, fn, tn):
    """(judge_pass, human_pass) pairs for a given confusion matrix."""
    return [(True, True)] * tp + [(True, False)] * fp + [(False, True)] * fn + [(False, False)] * tn


@pytest.mark.parametrize("tp, fp, fn, tn, kappa", [
    (20, 10, 5, 15, 0.4),       # p_o = 0.7, p_e = 0.6*0.5 + 0.4*0.5 = 0.5 -> 0.4
    (10, 0, 0, 10, 1.0),        # perfect agreement
    (0, 10, 10, 0, -1.0),       # always opposite, balanced
    (45, 5, 5, 45, 0.8),        # p_o = 0.9, p_e = 0.5
    (40, 5, 5, 0, -0.1111111),  # p_o = 0.8, p_e = 0.82 -> high raw agreement, worse than chance
])
def test_cohen_kappa_matches_hand_computed_values(tp, fp, fn, tn, kappa):
    assert cohen_kappa(pairs(tp, fp, fn, tn)) == pytest.approx(kappa, abs=1e-6)


def test_kappa_is_undefined_when_there_is_nothing_or_one_label_only():
    assert cohen_kappa([]) is None
    assert cohen_kappa(pairs(7, 0, 0, 0)) is None  # chance agreement is 1


def test_confusion_and_mae():
    assert confusion(pairs(2, 1, 3, 4)) == {"tp": 2, "fp": 1, "fn": 3, "tn": 4}
    assert mae([(5, 4), (3, 5), (4, 4)]) == pytest.approx(1.0)
    assert mae([]) is None
    assert to_bool("Yes") is True and to_bool("não") is False and to_bool("") is None


def label_row(id_, verdict, human_pass, jg=5, hg="", jc=5, hc=""):
    return {"id": id_, "judge_verdict": verdict, "human_pass": human_pass, "judge_grounded": jg,
            "human_grounded": hg, "judge_complete": jc, "human_complete": hc}


def test_agreement_skips_unlabeled_rows_and_reports_every_metric():
    rows = [label_row("a", "pass", "yes", 5, "4", 4, "4"), label_row("b", "pass", "no", 5, "2", 5, "3"),
            label_row("c", "fail", "no", 2, "1", 3, ""), label_row("d", "pass", "yes", 4, "4", 5, "5"),
            label_row("e", "pass", "")]  # not labeled yet
    r = agreement(rows)
    assert (r["rows"], r["labeled"]) == (5, 4)
    assert r["confusion"] == {"tp": 2, "fp": 1, "fn": 0, "tn": 1}
    assert r["pass_agreement"] == pytest.approx(0.75)
    # p_o 0.75; judge pass 0.75, human pass 0.5 -> p_e = 0.375 + 0.125 = 0.5 -> kappa 0.5
    assert r["cohen_kappa"] == pytest.approx(0.5)
    assert r["mae_grounded"] == pytest.approx((1 + 3 + 1 + 0) / 4) and r["n_grounded"] == 4
    assert r["mae_complete"] == pytest.approx((0 + 2 + 0) / 3) and r["n_complete"] == 3
    assert r["disagreements"] == ["b"]
    text = render(r, "labels.csv")
    assert "Cohen's kappa: 0.50" in text and "| pass | 2 | 1 |" in text


REPORT = {"rows": [
    {"id": "q03", "message": "When?", "answer": "Tomorrow.", "handoff": None,
     "tool_calls": [{"agent": "support", "name": "get_payout_schedule", "args": {}}],
     "judge": {"relevance": 5, "groundedness": 4, "completeness": 5, "tone": 5, "verdict": "pass", "reason": "ok"}},
    {"id": "m01", "message": "What color?", "answer": "Green.", "history": [{"role": "user", "content": "green"}],
     "holdout": True, "tool_calls": [],
     "judge": {"relevance": 5, "groundedness": 5, "completeness": 5, "tone": 5, "verdict": "pass", "reason": ""}},
    {"id": "h01", "message": "human", "answer": "Passed on.", "handoff": "user_requested", "tool_calls": [],
     "judge": {"relevance": 5, "groundedness": 5, "completeness": 5, "tone": 5, "verdict": "fail", "reason": "x"}},
    {"id": "q99", "provider_error": "router: RateLimitError", "latency_ms": 0},
    {"id": "q98", "answer": "a", "judge": {"verdict": "error", "reason": "judge_error"}},
]}


def test_export_keeps_only_judged_cases_with_empty_human_columns(tmp_path):
    rows = to_label_rows(REPORT)
    assert [r["id"] for r in rows] == ["q03", "m01", "h01"]
    assert rows[0]["evidence_summary"] == "support.get_payout_schedule({})"
    assert rows[1]["question"].startswith("[earlier: user: green]") and rows[1]["holdout"] == "yes"
    assert rows[2]["evidence_summary"] == "no tools; handoff=user_requested"
    assert all(r["human_pass"] == "" and r["human_grounded"] == "" for r in rows)
    out = tmp_path / "labels.csv"
    write_csv(rows, out)
    with out.open(newline="", encoding="utf-8") as f:
        back = list(csv.DictReader(f))
    assert list(back[0]) == COLUMNS and back[0]["judge_grounded"] == "4"
    # filled by a "human" and fed back: the round trip works
    back[0]["human_pass"], back[2]["human_pass"] = "yes", "yes"
    r = agreement(back)
    assert r["labeled"] == 2 and r["disagreements"] == ["h01"]


def test_export_reads_a_report_file_from_the_command_line(tmp_path, monkeypatch, capsys):
    from eval.calibration import export

    src = tmp_path / "20261112-000000.json"
    src.write_text(json.dumps(REPORT), encoding="utf-8")
    out = tmp_path / "t.csv"
    monkeypatch.setattr("sys.argv", ["export", str(src), "-o", str(out)])
    export.main()
    assert "3 cases" in capsys.readouterr().out and out.exists()
    assert export.newest_report(tmp_path) == src
