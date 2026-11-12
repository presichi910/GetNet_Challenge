"""LLM judge: scores one answer. Uses a different model family than the agents, to avoid bias."""
from __future__ import annotations

import json
import re

from langchain_core.messages import HumanMessage, SystemMessage

from app.llm import get_judge_llm
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
        self.llm = llm or get_judge_llm(temperature=0.0, max_tokens=500)  # always Groq, whatever AGENT_PROVIDER

    def score(self, question: str, answer: str, evidence: list[str], criteria: str = "", callbacks=None) -> dict:
        ev = "\n\n".join(evidence) if evidence else "(no evidence: the assistant used no tools)"
        note = f"## Case-specific expected behavior (takes precedence)\n{criteria}\n\n" if criteria else ""
        user = f"{note}## User question\n{question}\n\n## Assistant answer\n{answer}\n\n## Evidence available to the assistant\n{ev}"
        last_err = None
        for _ in range(2):
            try:
                msg = self.llm.invoke([SystemMessage(content=JUDGE_PROMPT), HumanMessage(content=user)],
                                      config={"callbacks": callbacks} if callbacks else None)
                return parse_judgement(str(msg.content))
            except Exception as e:  # noqa: BLE001
                last_err = e
        return {"relevance": 0, "groundedness": 0, "completeness": 0, "tone": 0, "verdict": "error",
                "reason": f"judge_error: {last_err}"}
