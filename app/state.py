"""Shared state that flows through the LangGraph StateGraph."""
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

AgentName = Literal["knowledge", "support"]


class AgentResult(TypedDict, total=False):
    """What a specialist agent hands back to the orchestrator."""

    agent: str
    answer: str
    sources: list[dict]  # [{"url": ..., "title": ...}]
    tool_calls: list[dict]  # [{"name": ..., "args": {...}}]
    contexts: list[str]  # raw tool outputs, used by the evaluator for groundedness
    error: str | None
    unresolved: bool  # the agent itself said it could not resolve the request (low confidence)


class GraphState(TypedDict, total=False):
    # Conversation memory (persisted per user_id by the checkpointer)
    messages: Annotated[list[AnyMessage], add_messages]
    user_id: str

    # Per-turn fields. The router resets them on every turn.
    plan: list[str]
    step: int
    language: str
    routing_reasoning: str
    agent_outputs: dict[str, AgentResult]
    final_answer: str
    wants_human: bool  # the user explicitly asked for a person
    handoff: dict | None  # set by the handoff node: {id, reason, priority, ticket_id}
