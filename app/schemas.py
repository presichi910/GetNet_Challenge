from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000, description="The user's query or statement.")
    user_id: str = Field(min_length=1, max_length=100, pattern=r"^[\w.\-@]+$", description="Customer identifier.")


class Source(BaseModel):
    url: str
    title: str = ""
    type: str = ""  # "kb" | "web"
    relevance: float | None = None  # KB only: 1 - cosine distance of the best chunk


class ToolCall(BaseModel):
    agent: str
    name: str
    args: dict = {}


class Routing(BaseModel):
    plan: list[str]
    reasoning: str = ""
    language: str = ""


class HandoffInfo(BaseModel):
    id: str
    reason: str  # user_requested | technical_failure | agent_unresolved
    priority: str
    ticket_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    user_id: str
    agents_used: list[str]
    routing: Routing
    sources: list[Source] = []
    tool_calls: list[ToolCall] = []
    degraded: bool = False  # True when at least one specialist failed
    handoff: HandoffInfo | None = None  # set when the case was passed to a human
    latency_ms: int
    trace_id: str
