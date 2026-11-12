"""Agreement between the LLM judge and a human, from a filled labels CSV (see export.py). Pure Python.

    python -m eval.calibration.agreement eval/calibration/labels.csv [-o report.md]

Rows with an empty human_pass are skipped (not labeled yet). Reports:
- pass/fail agreement (%) and Cohen's kappa between judge_verdict and human_pass,
- the 2x2 confusion matrix (rows: judge, columns: human),
- mean absolute error, judge vs human, for groundedness and completeness (1-5), over the rows that have both.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

YES = {"yes", "y", "pass", "true", "1", "si", "sí", "sim"}
NO = {"no", "n", "fail", "false", "0", "nao", "não"}


def to_bool(value: str) -> bool | None:
    v = (value or "").strip().lower()
    if v in YES:
        return True
    if v in NO:
        return False
    return None


def to_score(value) -> int | None:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 5 else None


def confusion(pairs: list[tuple[bool, bool]]) -> dict[str, int]:
    """pairs of (judge_pass, human_pass) -> counts of tp (both pass), tn (both fail), fp (judge pass, human
    fail) and fn (judge fail, human pass)."""
    c = {"tp": 0, "tn": 0, "fp": 0, "fn": 0}
    for j, h in pairs:
        c["tp" if j and h else "tn" if not j and not h else "fp" if j else "fn"] += 1
    return c


def cohen_kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    """(p_o - p_e) / (1 - p_e). None when it is undefined (no pairs, or both raters always gave one same
    label, so chance agreement is 1)."""
    n = len(pairs)
    if not n:
        return None
    c = confusion(pairs)
    p_o = (c["tp"] + c["tn"]) / n
    judge_pass = (c["tp"] + c["fp"]) / n
    human_pass = (c["tp"] + c["fn"]) / n
    p_e = judge_pass * human_pass + (1 - judge_pass) * (1 - human_pass)
    if p_e == 1:
        return None
    return (p_o - p_e) / (1 - p_e)


def mae(pairs: list[tuple[int, int]]) -> float | None:
    return sum(abs(a - b) for a, b in pairs) / len(pairs) if pairs else None


def agreement(rows: list[dict]) -> dict:
    labeled = [r for r in rows if to_bool(r.get("human_pass", "")) is not None]
    pairs = [(str(r.get("judge_verdict", "")).strip().lower() == "pass", to_bool(r["human_pass"])) for r in labeled]
    out = {"rows": len(rows), "labeled": len(labeled), "confusion": confusion(pairs),
           "pass_agreement": (sum(j == h for j, h in pairs) / len(pairs)) if pairs else None,
           "cohen_kappa": cohen_kappa(pairs)}
    for dim, judge_col, human_col in (("grounded", "judge_grounded", "human_grounded"),
                                      ("complete", "judge_complete", "human_complete")):
        both = [(to_score(r.get(judge_col)), to_score(r.get(human_col))) for r in labeled]
        both = [(a, b) for a, b in both if a is not None and b is not None]
        out[f"mae_{dim}"] = mae(both)
        out[f"n_{dim}"] = len(both)
    out["disagreements"] = [r["id"] for r, (j, h) in zip(labeled, pairs) if j != h]
    return out


def render(result: dict, source: str) -> str:
    def fmt(v, pct=False):
        if v is None:
            return "n/a"
        return f"{v * 100:.0f}%" if pct else f"{v:.2f}"

    c = result["confusion"]
    return "\n".join([
        "# Judge calibration: agreement with a human", "", f"_Labels: `{source}`_", "",
        f"- labeled rows: {result['labeled']} of {result['rows']}",
        f"- pass/fail agreement: {fmt(result['pass_agreement'], pct=True)}",
        f"- Cohen's kappa: {fmt(result['cohen_kappa'])}",
        f"- MAE groundedness (1-5): {fmt(result['mae_grounded'])} over {result['n_grounded']} rows",
        f"- MAE completeness (1-5): {fmt(result['mae_complete'])} over {result['n_complete']} rows",
        "", "| judge \\ human | pass | fail |", "|---|---|---|",
        f"| pass | {c['tp']} | {c['fp']} |", f"| fail | {c['fn']} | {c['tn']} |", "",
        "Disagreements: " + (", ".join(result["disagreements"]) or "none"), ""])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("labels", help="the filled CSV (columns of export.py)")
    ap.add_argument("-o", "--out", help="also write the markdown report here")
    args = ap.parse_args()
    with open(args.labels, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    text = render(agreement(rows), args.labels)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
