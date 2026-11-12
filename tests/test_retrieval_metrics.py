"""Retrieval metrics on synthetic rankings with known values. No network."""
import math
from types import SimpleNamespace

import pytest

from eval.run_retrieval import (average, evaluate, hit_at, load, ndcg_at, ranked_urls, recall_at, reciprocal_rank,
                                render, score)

R = ["a", "b", "c", "d", "e"]


def test_mrr_is_one_over_the_rank_of_the_first_relevant():
    assert reciprocal_rank(R, {"a"}) == 1.0
    assert reciprocal_rank(R, {"c"}) == pytest.approx(1 / 3)
    assert reciprocal_rank(R, {"e", "d"}) == pytest.approx(1 / 4)  # the first relevant one counts
    assert reciprocal_rank(R, {"z"}) == 0.0
    # mean over three queries with first relevant at ranks 1, 2 and none -> (1 + 0.5 + 0) / 3 = 0.5
    rows = [{"scores": score(R, ["a"])}, {"scores": score(R, ["b"])}, {"scores": score(R, ["z"])}]
    assert average(rows)["mrr"] == pytest.approx(0.5)


def test_hit_and_recall_at_k():
    assert (hit_at(R, {"c"}, 1), hit_at(R, {"c"}, 3), hit_at(R, {"c"}, 5)) == (0.0, 1.0, 1.0)
    assert recall_at(R, {"a", "e", "z"}, 5) == pytest.approx(2 / 3)
    assert recall_at(R, {"a", "e"}, 3) == pytest.approx(0.5)


def test_ndcg_with_known_values():
    assert ndcg_at(R, {"a"}, 5) == pytest.approx(1.0)
    assert ndcg_at(R, {"b"}, 5) == pytest.approx(1 / math.log2(3))  # 0.6309
    # relevant at ranks 2 and 4, two relevant: (1/log2 3 + 1/log2 5) / (1 + 1/log2 3)
    assert ndcg_at(R, {"b", "d"}, 5) == pytest.approx((1 / math.log2(3) + 1 / math.log2(5)) / (1 + 1 / math.log2(3)))
    assert ndcg_at(R, {"z"}, 5) == 0.0
    assert ndcg_at(R, {"a", "b"}, 5) == pytest.approx(1.0)


def doc(url):
    return SimpleNamespace(metadata={"url": url})


def test_ranking_is_the_distinct_urls_in_order_and_evaluate_scores_and_isolates_errors():
    results = [(doc("u1"), 0.1), (doc("u1"), 0.2), (doc("u2"), 0.3), (doc(""), 0.4), (doc("u3"), 0.5)]
    assert ranked_urls(results) == ["u1", "u2", "u3"]

    def retrieve(q, k):
        if q == "boom":
            raise RuntimeError("RateLimitError 429")
        return results

    cases = [{"id": "q1", "question": "x", "expected_urls": ["u2"]},
             {"id": "q2", "question": "y", "expected_urls": ["u9"], "holdout": True},
             {"id": "q3", "question": "boom", "expected_urls": ["u1"]}]
    rows = evaluate(cases, retrieve, 5)
    assert rows[0]["scores"]["mrr"] == 0.5 and rows[1]["scores"]["hit@5"] == 0.0 and "error" in rows[2]
    text = render(rows, 5, "stamp")
    assert "| regular | 1 | 0.00 | 1.00 | 1.00 | 1.00 | 0.50 |" in text
    assert "| holdout | 1 |" in text and "1 question(s) failed" in text


def test_the_dataset_has_about_twenty_questions_and_a_third_holdout():
    cases = load()
    held = [c for c in cases if c.get("holdout")]
    assert 18 <= len(cases) <= 25 and 0.25 <= len(held) / len(cases) <= 0.35
    assert all(c["expected_urls"] and c["question"] for c in cases)
    assert len({c["id"] for c in cases}) == len(cases)
