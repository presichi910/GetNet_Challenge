"""Agent 3: Customer Support. Retrieves the caller's own data through tools bound to their user_id."""
from __future__ import annotations

from app.agents.common import format_prior_context, recent_dialogue, run_tool_agent
from app.config import get_settings
from app.data.repository import CustomerRepository
from app.llm import get_llm
from app.prompts import SUPPORT_PROMPT
from app.tools.customer_tools import make_support_tools
from app.tools.handoff_tools import escalate_to_human


def make_support_node(llm=None, repo: CustomerRepository | None = None):
    repository = repo or CustomerRepository(tickets_file=get_settings().tickets_file)

    def support_node(state: dict) -> dict:
        outputs = state.get("agent_outputs", {})
        user_id = state["user_id"]
        prompt = SUPPORT_PROMPT.format(user_id=user_id, language=state.get("language", "English"),
                                       prior_context=format_prior_context(outputs))
        tools = [*make_support_tools(user_id, repository), escalate_to_human]  # bound to this user only
        history = recent_dialogue(state["messages"], get_settings().history_window)
        result = run_tool_agent("support", llm or get_llm(model=get_settings().light_model), tools, prompt, history)
        return {"agent_outputs": {**outputs, "support": result}, "step": state.get("step", 0) + 1}

    return support_node
