from pydantic import BaseModel, Field

# Allowed characters for ids (ASCII only, no ":" so "<user_id>:<conversation_id>" is unambiguous).
# Used for the JWT `sub`, the optional body `user_id` and `conversation_id`.
USER_ID_CHARS = r"[A-Za-z0-9_.@-]+"
USER_ID_MAX = 100
CONVERSATION_ID_CHARS = r"[A-Za-z0-9_.-]+"
CONVERSATION_ID_MAX = 64
DEFAULT_CONVERSATION = "default"


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
