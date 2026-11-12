import re
from typing import Literal

from pydantic import BaseModel, Field, StrictBool, field_validator

# Allowed characters for ids (ASCII only, no ":" so "<user_id>:<conversation_id>" is unambiguous).
# Used for the JWT `sub`, the optional body `user_id` and `conversation_id`.
USER_ID_CHARS = r"[A-Za-z0-9_.@-]+"
USER_ID_MAX = 100
CONVERSATION_ID_CHARS = r"[A-Za-z0-9_.-]+"
CONVERSATION_ID_MAX = 64
DEFAULT_CONVERSATION = "default"

# human oversight
HANDOFF_ID_PATTERN = r"HND-[A-Za-z0-9]{1,32}"  # what the handoff node generates: HND-3F9A1C
OPERATOR_MESSAGE_MAX = 1000  # characters an operator may send to a customer
REJECT_REASON_MAX = 500  # characters of the reason an operator gives for giving a case up
IDEMPOTENCY_KEY_CHARS = r"[A-Za-z0-9_.-]+"
IDEMPOTENCY_KEY_MIN, IDEMPOTENCY_KEY_MAX = 8, 64  # a uuid4 fits
HandoffStatusName = Literal["pending", "claimed", "resolved"]


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
    status: str = "pending"  # pending | claimed | resolved. Nothing about who is handling it


def clean_text(v: str, what: str) -> str:
    """Trim, refuse empty text and control characters (newline and tab are fine)."""
    v = v.strip()
    if not v:
        raise ValueError(f"{what} is empty")
    if any(ord(c) < 32 and c not in "\n\t" for c in v):
        raise ValueError(f"{what} contains control characters")
    return v


def is_valid_handoff_id(value: str) -> bool:
    return re.fullmatch(HANDOFF_ID_PATTERN, value) is not None


class OperatorMessage(BaseModel):
    """What a human operator answered about the customer's case. Never says who the operator is."""

    handoff_id: str
    decision: Literal["approve", "reject"]
    message: str
    created_at: str  # ISO 8601, UTC
    sender: Literal["human_operator"] = "human_operator"  # clients label it, so it is never mistaken for the assistant


class OpenHandoff(BaseModel):
    """A case of this conversation that is still with the humans."""

    id: str
    status: HandoffStatusName


class ChatResponse(BaseModel):
    response: str
    user_id: str
    conversation_id: str = DEFAULT_CONVERSATION
    agents_used: list[str]
    routing: Routing
    sources: list[Source] = []
    tool_calls: list[ToolCall] = []
    degraded: bool = False  # True if an agent failed
    handoff: HandoffInfo | None = None  # set when a human takes over (this turn), with its status
    open_handoffs: list[OpenHandoff] = []  # earlier cases of this conversation still pending or claimed
    operator_messages: list[OperatorMessage] = []  # human replies not delivered before, delivered once here
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


class StoredOperatorMessage(OperatorMessage):
    delivered: bool  # already handed to the customer in a /chat response


class OperatorMessageList(BaseModel):
    conversation_id: str
    messages: list[StoredOperatorMessage]


# ---- /operator/* ----

class ReplyRequest(BaseModel):
    decision: Literal["approve", "reject"]
    message: str = Field(min_length=1, max_length=OPERATOR_MESSAGE_MAX, description="Shown to the customer.")
    idempotency_key: str = Field(min_length=IDEMPOTENCY_KEY_MIN, max_length=IDEMPOTENCY_KEY_MAX,
                                 pattern=f"^{IDEMPOTENCY_KEY_CHARS}$",
                                 description="Chosen by the client, e.g. a uuid. Repeating it repeats the result, not the message.")

    @field_validator("message")
    @classmethod
    def _clean(cls, v: str) -> str:
        return clean_text(v, "message")


class RejectRequest(BaseModel):
    """An operator giving a case up. `reason` is for the other operators and the audit trail, not for the customer."""

    reason: str = Field(min_length=1, max_length=REJECT_REASON_MAX)
    close: StrictBool = Field(default=False, description="False: back to the queue. True: closed, nothing sent to the customer.")

    @field_validator("reason")
    @classmethod
    def _clean(cls, v: str) -> str:
        return clean_text(v, "reason")


class HandoffSummary(BaseModel):
    """One row of the operator queue."""

    id: str
    conversation_id: str
    merchant_id: str
    reason: str
    priority: str
    summary: str
    status: HandoffStatusName
    claimed_by: str | None = None
    claimed_at: str | None = None
    lease_expires_at: str | None = None  # a claim only holds until then; claim again to renew
    resolution: Literal["approve", "reject"] | None = None
    resolved_at: str | None = None
    created_at: str


class HandoffDetail(HandoffSummary):
    operator_reply: str | None = None
    record: dict  # the full case: recent conversation, what the agents found, the tool calls


class ReplyResult(HandoffDetail):
    replayed: bool = False  # True: this idempotency_key was already applied, nothing was stored again


class HandoffList(BaseModel):
    handoffs: list[HandoffSummary]
    count: int
