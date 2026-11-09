"""Agent 2: Knowledge. RAG over Getnet's website (Chroma) + Tavily web search for everything else."""
from __future__ import annotations

from app.agents.common import format_prior_context, recent_dialogue, run_tool_agent
from app.config import get_settings
from app.llm import get_llm
from app.prompts import KNOWLEDGE_PROMPT
from app.tools.handoff_tools import escalate_to_human
from app.tools.knowledge_tools import search_getnet_kb, web_search


def make_knowledge_node(llm=None):
    def knowledge_node(state: dict) -> dict:
        outputs = state.get("agent_outputs", {})
        prompt = KNOWLEDGE_PROMPT.format(language=state.get("language", "English"),
                                         prior_context=format_prior_context(outputs))
        history = recent_dialogue(state["messages"], get_settings().history_window)
        result = run_tool_agent("knowledge", llm or get_llm(), [search_getnet_kb, web_search, escalate_to_human], prompt, history,
                               require_tool=True)
        return {"agent_outputs": {**outputs, "knowledge": result}, "step": state.get("step", 0) + 1}

    return knowledge_node
