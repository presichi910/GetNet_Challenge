import csv

from eval.calibration.judge_equiv import compare, load_human, vs_human


def J(v, g=4):
    return {"verdict": v, "relevance": 5, "groundedness": g, "completeness": 4, "tone": 5, "language_match": True}


def test_compare_agreement_and_disagreements():
    groq = {"a": J("pass"), "b": J("fail", 3), "c": J("pass"), "d": J("error")}
    new = {"a": J("pass"), "b": J("pass", 5), "c": J("pass"), "d": J("pass")}
    c = compare(groq, new)
    assert c["n"] == 3 and c["skipped"] == ["d"]
    assert abs(c["agreement"] - 2 / 3) < 1e-9
    assert c["disagreements"] == [("b", "fail", "pass")]
    assert c["mae"]["groundedness"] == (2 / 3, 3)


def test_vs_human_false_pass_and_fail(tmp_path):
    p = tmp_path / "h.csv"
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "human_pass"])
        w.writerows([["a", "yes"], ["b", "no"], ["c", "yes"]])
    h = load_human(str(p))
    r = vs_human({"a": J("pass"), "b": J("pass"), "c": J("fail")}, h)
    assert r["false_pass"] == ["b"] and r["false_fail"] == ["c"] and r["n"] == 3
