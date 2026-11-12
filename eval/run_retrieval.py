"""Retrieval metrics of the knowledge base search, against the real retriever (Chroma + Voyage query embeddings).

    python -m eval.run_retrieval              # all questions of eval/retrieval.jsonl -> eval/reports/RETRIEVAL-<stamp>.md
    python -m eval.run_retrieval --pause 21   # Voyage free tier (~3 requests/min)

Each question has the page(s) that answer it (`expected_urls`, checked to exist in the KB). The ranking is the
list of distinct URLs, in order, of the chunks `app.tools.knowledge_tools.retrieve` returns (k = RETRIEVAL_K,
web pages and at most 2 PDF chunks, exactly what the Knowledge agent gets). Binary relevance: a URL is
relevant if it is one of the expected ones.

Metrics per question, then averaged: hit@1/3/5 (an expected URL in the top k), recall@5 (share of the
expected URLs in the top 5), MRR (1 / rank of the first expected URL, 0 if none) and nDCG@5. Questions marked
`"holdout": true` are averaged apart; they are for checking a retrieval change, never for tuning one.
Only the query embedding is paid (Voyage); no LLM is called.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
DATASET = ROOT / "retrieval.jsonl"


def load(path: Path = DATASET) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def ranked_urls(results) -> list[str]:
    """Distinct URLs of [(doc, distance)] in order (they come sorted by distance)."""
    out = []
    for doc, _ in results:
        url = doc.metadata.get("url", "")
        if url and url not in out:
            out.append(url)
    return out


def hit_at(ranking: list[str], relevant: set[str], k: int) -> float:
    return 1.0 if any(u in relevant for u in ranking[:k]) else 0.0


def recall_at(ranking: list[str], relevant: set[str], k: int) -> float:
    return len(relevant & set(ranking[:k])) / len(relevant) if relevant else 0.0


def reciprocal_rank(ranking: list[str], relevant: set[str]) -> float:
    for i, u in enumerate(ranking, start=1):
        if u in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at(ranking: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 1) for i, u in enumerate(ranking[:k], start=1) if u in relevant)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else 0.0


def score(ranking: list[str], expected: list[str]) -> dict:
    rel = set(expected)
    return {"hit@1": hit_at(ranking, rel, 1), "hit@3": hit_at(ranking, rel, 3), "hit@5": hit_at(ranking, rel, 5),
            "recall@5": recall_at(ranking, rel, 5), "mrr": reciprocal_rank(ranking, rel), "ndcg@5": ndcg_at(ranking, rel, 5)}


METRICS = ("hit@1", "hit@3", "hit@5", "recall@5", "mrr", "ndcg@5")


def average(rows: list[dict]) -> dict:
    ok = [r for r in rows if "error" not in r]
    return {m: (sum(r["scores"][m] for r in ok) / len(ok)) if ok else None for m in METRICS} | {"n": len(ok)}


def evaluate(cases: list[dict], retrieve, k: int, pause: float = 0.0, sleep=time.sleep) -> list[dict]:
    rows = []
    for i, c in enumerate(cases):
        try:
            ranking = ranked_urls(retrieve(c["question"], k))
            rows.append({**c, "ranking": ranking, "scores": score(ranking, c["expected_urls"])})
        except Exception as e:  # noqa: BLE001 - a provider error is reported, not scored
            rows.append({**c, "error": f"{type(e).__name__}: {str(e)[:200]}"})
        if pause and i < len(cases) - 1:
            sleep(pause)
    return rows


def render(rows: list[dict], k: int, stamp: str) -> str:
    main, held = [r for r in rows if not r.get("holdout")], [r for r in rows if r.get("holdout")]
    lines = ["# Retrieval report", "", f"_Generated {stamp}. Retriever: `app.tools.knowledge_tools.retrieve`, k = {k} "
             "chunks (at most 2 PDF chunks), ranking = distinct URLs in order. Binary relevance._", "",
             "| set | n | hit@1 | hit@3 | hit@5 | recall@5 | MRR | nDCG@5 |", "|---|---|---|---|---|---|---|---|"]
    for name, part in (("regular", main), ("holdout", held), ("all", rows)):
        a = average(part)
        if a["n"]:
            lines.append(f"| {name} | {a['n']} | " + " | ".join(f"{a[m]:.2f}" for m in METRICS) + " |")
    errors = [r for r in rows if "error" in r]
    if errors:
        lines += ["", f"**{len(errors)} question(s) failed (provider error) and are not scored:** "
                  + ", ".join(r["id"] for r in errors)]
    lines += ["", "## Questions", "", "| id | first expected rank | MRR | nDCG@5 | top URLs |", "|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['id']} | error | - | - | {r['error'][:80]} |")
            continue
        rank = next((i for i, u in enumerate(r["ranking"], 1) if u in set(r["expected_urls"])), None)
        top = ", ".join(u.split("//", 1)[-1][:60] for u in r["ranking"][:3])
        lines.append(f"| {r['id']}{' (holdout)' if r.get('holdout') else ''} | {rank or 'not found'} "
                     f"| {r['scores']['mrr']:.2f} | {r['scores']['ndcg@5']:.2f} | {top} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    from app.config import get_settings
    from app.tools.knowledge_tools import retrieve

    ap = argparse.ArgumentParser()
    ap.add_argument("--pause", type=float, default=0.0, help="seconds between queries (Voyage rate limits)")
    ap.add_argument("--k", type=int, default=None, help="chunks to retrieve (default RETRIEVAL_K)")
    args = ap.parse_args()
    k = args.k or get_settings().retrieval_k
    rows = evaluate(load(), retrieve, k, pause=args.pause)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ROOT / "reports" / f"RETRIEVAL-{stamp}.md"
    out.write_text(render(rows, k, stamp), encoding="utf-8")
    (ROOT / "reports" / f"RETRIEVAL-{stamp}.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(render(rows, k, stamp).split("## Questions")[0])
    print(f"Report: {out}")


if __name__ == "__main__":
    main()
