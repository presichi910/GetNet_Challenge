"""State shared by the graph nodes."""
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

AgentName = Literal["knowledge", "support"]


class AgentResult(TypedDict, total=False):
    """What an agent hands back."""

    agent: str
    answer: str
    sources: list[dict]  # [{"url": ..., "title": ...}]
    tool_calls: list[dict]  # [{"name": ..., "args": {...}}]
    contexts: list[str]  # raw tool outputs, for the evaluator
    error: str | None
    unresolved: bool  # the agent said it couldn't resolve it


class GraphState(TypedDict, total=False):
    # conversation memory (one thread per "<user_id>:<conversation_id>")
    messages: Annotated[list[AnyMessage], add_messages]
    user_id: str  # the authenticated user (JWT sub), set by the API, never by the LLM

    # per-turn fields, the router resets them every turn
    plan: list[str]
    step: int
    language: str
    routing_reasoning: str
    agent_outputs: dict[str, AgentResult]
    final_answer: str
    router_error: str  # why the router fell back to the default plan ("" if it didn't), for the eval
    synth_error: str  # why the synthesizer used its fallback ("" if it didn't), for the eval
    wants_human: bool  # user asked for a person
    handoff: dict | None  # set by the handoff node
