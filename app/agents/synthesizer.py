"""Last node: builds the one answer the user sees.

- no agents ran -> answer directly (greetings, unclear, out of scope)
- 1 agent       -> pass its answer through
- 2+ agents     -> merge them with the LLM
- all failed    -> fixed fallback message
"""
from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, SystemMessage

from app.agents.common import recent_dialogue
from app.config import get_settings
from app.llm import get_llm
from app.prompts import FALLBACK_MESSAGE, SYNTHESIZER_DIRECT_PROMPT, SYNTHESIZER_MERGE_PROMPT

log = logging.getLogger(__name__)


def make_synthesizer_node(llm=None):
    def synthesizer_node(state: dict) -> dict:
        model = llm or get_llm(temperature=0.2, model=get_settings().light_model)
        language = state.get("language", "English")
        outputs = state.get("agent_outputs", {})
        ok = {k: v for k, v in outputs.items() if v.get("answer") and not v.get("error")}

        try:
            if not state.get("plan"):
                history = recent_dialogue(state["messages"], get_settings().history_window)
                msg = model.invoke([SystemMessage(content=SYNTHESIZER_DIRECT_PROMPT.format(language=language)), *history])
                answer = str(msg.content).strip()
            elif len(ok) == 1:
                answer = next(iter(ok.values()))["answer"]
            elif len(ok) >= 2:
                blob = "\n\n".join(f"### {k}\n{v['answer']}" for k, v in ok.items())
                history = recent_dialogue(state["messages"], 2)
                msg = model.invoke([SystemMessage(content=SYNTHESIZER_MERGE_PROMPT.format(language=language, agent_outputs=blob)), *history])
                answer = str(msg.content).strip()
            else:
                answer = FALLBACK_MESSAGE
        except Exception:
            log.exception("Synthesizer failed")
            # use whatever an agent produced instead of failing
            answer = next((v["answer"] for v in ok.values()), FALLBACK_MESSAGE)

        return {"final_answer": answer, "messages": [AIMessage(content=answer)]}

    return synthesizer_node
