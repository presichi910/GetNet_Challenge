"""Judge-equivalence report (plan 9.6, block C2): the OpenRouter judge against the Groq judge and the human V3 labels.

    python -m eval.calibration.judge_equiv --groq G.judged.json --new-sample S.judged.json --new-full F.judged.json \
        --human eval/calibration/labels_v3_blind.csv -o eval/calibration/JUDGE_EQUIV.md

Reads results only; it never calls a judge and never opens the key file. Rows where either judge errored are left out of
the agreement and counted apart. Acceptance (docs/PLAN_96.md, C2): verdict agreement with Groq >= 0.90 and no false pass
against the human that Groq did not already have.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from eval.calibration.agreement import cohen_kappa, confusion, mae, to_bool

DIMS = ("relevance", "groundedness", "completeness", "tone")


def load_judged(path: str) -> dict[str, dict]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))["rows"]
    return {r["id"]: r["judge"] for r in rows if r.get("judge")}


def load_human(path: str) -> dict[str, bool]:
    with open(path, encoding="utf-8", newline="") as f:
        out = {}
        for r in csv.DictReader(f):
            b = to_bool(r.get("human_pass", ""))
            if b is not None:
                out[r["id"]] = b
    return out


def _ok(j: dict | None) -> bool:
    return bool(j) and j.get("verdict") in ("pass", "fail")


def compare(groq: dict, new: dict) -> dict:
    ids = sorted(i for i in new if i in groq and _ok(groq[i]) and _ok(new[i]))
    pairs = [(groq[i]["verdict"] == "pass", new[i]["verdict"] == "pass") for i in ids]
    out = {"n": len(ids), "skipped": sorted(set(new) - set(ids)),
           "agreement": (sum(a == b for a, b in pairs) / len(pairs)) if pairs else None,
           "kappa": cohen_kappa(pairs),
           "disagreements": [(i, groq[i]["verdict"], new[i]["verdict"]) for i in ids if groq[i]["verdict"] != new[i]["verdict"]],
           "mae": {}}
    for d in DIMS:
        ps = [(groq[i][d], new[i][d]) for i in ids if isinstance(groq[i].get(d), int) and isinstance(new[i].get(d), int)]
        out["mae"][d] = (mae(ps), len(ps))
    lg = [(i, groq[i].get("language_match"), new[i].get("language_match")) for i in ids]
    out["language_disagreements"] = [i for i, a, b in lg if a != b]
    return out


def vs_human(judge: dict, human: dict) -> dict:
    ids = sorted(i for i in human if i in judge and _ok(judge[i]))
    pairs = [(judge[i]["verdict"] == "pass", human[i]) for i in ids]
    return {"n": len(ids), "confusion": confusion(pairs), "kappa": cohen_kappa(pairs),
            "agreement": (sum(a == b for a, b in pairs) / len(pairs)) if pairs else None,
            "false_pass": [i for i in ids if judge[i]["verdict"] == "pass" and not human[i]],
            "false_fail": [i for i in ids if judge[i]["verdict"] == "fail" and human[i]]}


def _f(x, nd=2):
    return "n/a" if x is None else f"{x:.{nd}f}"


def section(title: str, groq: dict, new: dict) -> list[str]:
    c = compare(groq, new)
    L = [f"## {title}", "", f"- rows compared: {c['n']}" + (f" (left out, a judge errored or missing: {', '.join(c['skipped'])})" if c["skipped"] else ""),
         f"- verdict agreement with Groq: {_f(c['agreement'] and c['agreement'] * 100, 1)} %",
         f"- Cohen's kappa (new vs Groq): {_f(c['kappa'])}",
         "- MAE per dimension (new vs Groq, 1-5): " + ", ".join(f"{d} {_f(m)} (n={n})" for d, (m, n) in c["mae"].items()),
         "- language_match disagreements: " + (", ".join(c["language_disagreements"]) or "none"), ""]
    if c["disagreements"]:
        L += ["| id | Groq | OpenRouter |", "|---|---|---|"] + [f"| {i} | {a} | {b} |" for i, a, b in c["disagreements"]] + [""]
    else:
        L += ["No verdict disagreements.", ""]
    return L


CONCLUSION = (
    "C2 did not meet its acceptance (sample_v3: 86.7 % verdict agreement with Groq; one new false pass against the human, q02_weather). "
    "No alternative host is tried: only fp8 hosts remain and the author's unquantized host did not reproduce Groq; this deviates from the plan "
    "(amendment 2, `docs/PREREG_PLAN96.md`). The OpenRouter judge (Qwen3.8 27B, Alibaba, reasoning none) is declared a NEW judge; calibration V4 "
    "(C4/C5) is the only valid one, and every before/after comparison uses it, with `C2-full.judged.json` (repetition 1 re-judged) as baseline. "
    "q02_weather and m04_followup_product are reported with a note."
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--groq", required=True)
    ap.add_argument("--new-sample", required=True)
    ap.add_argument("--new-full", required=True)
    ap.add_argument("--human", required=True)
    ap.add_argument("-o", required=True)
    a = ap.parse_args()
    groq, ns, nf = load_judged(a.groq), load_judged(a.new_sample), load_judged(a.new_full)
    human = load_human(a.human)
    hg, hn = vs_human(groq, human), vs_human(ns, human)
    new_fp = sorted(set(hn["false_pass"]) - set(hg["false_pass"]))
    full = compare(groq, nf)
    samp = compare(groq, ns)
    ok_agree = full["agreement"] is not None and full["agreement"] >= 0.90 and samp["agreement"] is not None and samp["agreement"] >= 0.90
    verdict = "MET" if ok_agree and not new_fp else "NOT MET"
    L = ["# Judge equivalence: OpenRouter (Qwen3.8 27B, Alibaba) against Groq", "",
         "_Generated by `eval/calibration/judge_equiv.py`; nothing typed by hand. Same answers and evidence in both judges (repetition 1, "
         "`20261115-202155`); the judge prompt and parser are unchanged._", "",
         f"**Acceptance (>= 90 % verdict agreement with Groq on both sets, 0 new false passes against the human): {verdict}.**", ""]
    L += section("(b) The 40 rows of repetition 1", groq, nf)
    L += section("(a) The 15 rows of sample_v3", groq, ns)
    L += ["## Against the human V3 labels (the 15 rows)", "",
          "| judge | n | agreement | kappa | pass/pass | false pass | false fail |", "|---|---|---|---|---|---|---|"]
    for name, h in (("Groq", hg), ("OpenRouter", hn)):
        c = h["confusion"]
        L.append(f"| {name} | {h['n']} | {_f(h['agreement'] and h['agreement'] * 100, 0)} % | {_f(h['kappa'])} | {c['tp']} | "
                 f"{c['fp']} ({', '.join(h['false_pass']) or '-'}) | {c['fn']} ({', '.join(h['false_fail']) or '-'}) |")
    L += ["", f"New false passes (OpenRouter pass, human fail, and Groq did not): {', '.join(new_fp) or 'none'}", ""]
    L += ["## Conclusion", "", CONCLUSION, ""]
    Path(a.o).write_text("\n".join(L), encoding="utf-8")
    print(f"wrote {a.o}: acceptance {verdict}")


if __name__ == "__main__":
    main()
