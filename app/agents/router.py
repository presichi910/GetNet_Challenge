"""Agent 1: Router. Decides which specialists run, and in which order."""
from __future__ import annotations

import logging
from typing import Literal

from langchain_core.messages import SystemMessage
from pydantic import BaseModel, Field

from app.agents.common import recent_dialogue
from app.config import get_settings
from app.llm import get_llm
from app.prompts import ROUTER_PROMPT

log = logging.getLogger(__name__)

VALID_AGENTS = ("knowledge", "support")
DEFAULT_PLAN = ["knowledge"]  # safest fallback: answers from public info, touches no customer data


class RoutingDecision(BaseModel):
    reasoning: str = Field(description="One short sentence explaining the choice.")
    agents: list[Literal["knowledge", "support"]] = Field(
        description="Ordered list of specialist agents to run. Empty for greetings/unclear/out-of-scope."
    )
    language: str = Field(default="English", description="Language of the user's latest message.")
    wants_human: bool = Field(default=False, description="True only if the user explicitly asks for a human.")


def normalize_plan(agents: list[str]) -> list[str]:
    """Deduplicate, drop unknown names, and enforce 'support' before 'knowledge'."""
    plan = [a for a in dict.fromkeys(agents) if a in VALID_AGENTS]
    return sorted(plan, key=lambda a: 0 if a == "support" else 1)  # stable: support first


def make_router_node(llm=None):
    structured = None

    def router_node(state: dict) -> dict:
        nonlocal structured
        if structured is None:
            structured = (llm or get_llm(model=get_settings().light_model)).with_structured_output(RoutingDecision, method="function_calling")

        history = recent_dialogue(state["messages"], get_settings().history_window)
        try:
            decision: RoutingDecision = structured.invoke([SystemMessage(content=ROUTER_PROMPT), *history])
            plan = normalize_plan(decision.agents)
            reasoning, language = decision.reasoning, decision.language
            wants_human = decision.wants_human
            if wants_human:
                plan = []  # no point running specialists: the case goes straight to a person
        except Exception:
            log.exception("Router failed; falling back to default plan")
            plan, reasoning, language, wants_human = list(DEFAULT_PLAN), "router_error_fallback", "English", False

        log.info("router plan=%s language=%s reason=%s", plan, language, reasoning)
        # Reset every per-turn field: the checkpointer keeps state between turns of one user.
        return {"plan": plan, "step": 0, "language": language, "routing_reasoning": reasoning,
                "agent_outputs": {}, "final_answer": "", "wants_human": wants_human, "handoff": None}

    return router_node
