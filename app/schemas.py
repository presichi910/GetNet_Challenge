import re
from typing import Literal

from pydantic import BaseModel, Field

# Allowed characters for ids (ASCII only, no ":" so "<user_id>:<conversation_id>" is unambiguous).
# Used for the JWT `sub`, the optional body `user_id` and `conversation_id`.
USER_ID_CHARS = r"[A-Za-z0-9_.@-]+"
USER_ID_MAX = 100
CONVERSATION_ID_CHARS = r"[A-Za-z0-9_.-]+"
CONVERSATION_ID_MAX = 64
DEFAULT_CONVERSATION = "default"


def is_valid_conversation_id(value: str) -> bool:
    return 0 < len(value) <= CONVERSATION_ID_MAX and re.fullmatch(CONVERSATION_ID_CHARS, value) is not None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000, description="The user's query or statement.")
    # The customer comes from the JWT. This field only exists so clients that follow the
    # {message, user_id} contract keep working: if sent, it must match the token, or 403.
    user_id: str | None = Field(default=None, min_length=1, max_length=USER_ID_MAX, pattern=f"^{USER_ID_CHARS}$",
                                description="Optional. Must equal the authenticated user.")
    conversation_id: str | None = Field(default=None, min_length=1, max_length=CONVERSATION_ID_MAX,
                                        pattern=f"^{CONVERSATION_ID_CHARS}$",
                                        description="Optional. Separate conversations of the same user.")


class Source(BaseModel):
    url: str
    title: str = ""
    type: str = ""  # "kb" | "web"
    relevance: float | None = None  # KB only


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
    conversation_id: str = DEFAULT_CONVERSATION
    agents_used: list[str]
    routing: Routing
    sources: list[Source] = []
    tool_calls: list[ToolCall] = []
    degraded: bool = False  # True if an agent failed
    handoff: HandoffInfo | None = None  # set when a human takes over
    latency_ms: int
    trace_id: str


class ConversationInfo(BaseModel):
    """Metadata of one of the caller's conversations. Never any content."""

    conversation_id: str
    created_at: str  # ISO 8601, UTC
    last_active: str  # ISO 8601, UTC
    turns: int  # answered turns


class ConversationList(BaseModel):
    conversations: list[ConversationInfo]
    count: int
    limit: int | None = None  # max conversations per customer, None = no limit


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ConversationDetail(ConversationInfo):
    # What the thread still holds: the recent window, plus a summary of what was folded away
    summary: str | None = None
    messages: list[HistoryMessage]
