"""LLM-as-judge: scores one (question, answer, evidence) triple.

The judge runs on a different model family than the generator (JUDGE_MODEL) to reduce the
well-known self-preference bias of LLM judges.
"""
from __future__ import annotations

import json
import re

from langchain_core.messages import HumanMessage, SystemMessage

from app.llm import get_llm
from app.config import get_settings
from app.prompts import JUDGE_PROMPT

SCORE_KEYS = ("relevance", "groundedness", "completeness", "tone")


def parse_judgement(text: str) -> dict:
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise ValueError(f"no JSON in judge output: {text[:200]!r}")
    data = json.loads(m.group(0))
    for k in SCORE_KEYS:
        data[k] = max(1, min(5, int(data[k])))
    data["verdict"] = "pass" if data.get("verdict") == "pass" else "fail"
    data.setdefault("reason", "")
    return data


class Judge:
    def __init__(self, llm=None):
        self.llm = llm or get_llm(temperature=0.0, model=get_settings().judge_model, max_tokens=500)

    def score(self, question: str, answer: str, evidence: list[str], criteria: str = "") -> dict:
        ev = "\n\n".join(evidence) if evidence else "(no evidence: the assistant used no tools)"
        note = f"## Case-specific expected behavior (takes precedence)\n{criteria}\n\n" if criteria else ""
        user = f"{note}## User question\n{question}\n\n## Assistant answer\n{answer}\n\n## Evidence available to the assistant\n{ev}"
        last_err = None
        for _ in range(2):
            try:
                msg = self.llm.invoke([SystemMessage(content=JUDGE_PROMPT), HumanMessage(content=user)])
                return parse_judgement(str(msg.content))
            except Exception as e:  # noqa: BLE001
                last_err = e
        return {"relevance": 0, "groundedness": 0, "completeness": 0, "tone": 0, "verdict": "error",
                "reason": f"judge_error: {last_err}"}
