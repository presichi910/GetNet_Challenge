"""Rolling summary: keeps `messages` bounded without losing the thread of the conversation.

Runs first in every turn. Below the threshold it does nothing. Above it, the messages older than
the last `history_window` are folded into `summary` (together with the previous summary) and
removed from `messages` with `RemoveMessage`, so the checkpoint stops growing.

It never fails the turn: any error or timeout is logged, the state is left as it was and the
next turn tries again (the message count is still above the threshold).
"""
from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage

from app.config import get_settings
from app.llm import get_llm
from app.prompts import SUMMARIZER_PROMPT

log = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 2000   # the summary itself must stay small
MAX_MESSAGE_CHARS = 1500   # per message sent to the summarizer, bounds the call


def _transcript(messages) -> str:
    lines = []
    for m in messages:
        if isinstance(m, (HumanMessage, AIMessage)) and m.content:
            role = "Customer" if isinstance(m, HumanMessage) else "Assistant"
            lines.append(f"{role}: {str(m.content)[:MAX_MESSAGE_CHARS]}")
    return "\n".join(lines)


def make_summarizer_node(llm=None):
    def summarizer_node(state: dict) -> dict:
        s = get_settings()
        messages = state.get("messages", [])
        keep = s.history_window
        if len(messages) <= s.summary_threshold or len(messages) <= keep:
            return {}

        old = messages[:-keep] if keep > 0 else list(messages)
        removable = [m for m in old if m.id]
        previous = state.get("summary", "") or "(none)"
        try:
            # the cheapest model in the config (not the Support one), short timeout, no retries:
            # this call must never hold a customer's turn for long
            model = llm or get_llm(model=s.light_model, max_tokens=400,
                                   timeout=s.summary_timeout_seconds, max_retries=0)
            msg = model.invoke([
                SystemMessage(content=SUMMARIZER_PROMPT),
                HumanMessage(content=f"PREVIOUS SUMMARY:\n{previous}\n\nOLDER MESSAGES:\n{_transcript(old)}"),
            ])
            summary = str(msg.content).strip()
            if not summary:
                raise ValueError("empty summary")
        except Exception as e:  # noqa: BLE001 - never fail the turn
            log.warning("summary skipped (%d messages, will retry next turn): %s: %s",
                        len(messages), type(e).__name__, str(e)[:200])
            return {}

        log.info("summary updated: %d old messages folded, %d kept", len(removable), len(messages) - len(old))
        return {"summary": summary[:MAX_SUMMARY_CHARS], "messages": [RemoveMessage(id=m.id) for m in removable]}

    return summarizer_node
