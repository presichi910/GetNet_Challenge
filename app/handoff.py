"""Human handoff: when a person takes over, and what happens then.

Three triggers:
  user_requested     the user asked for a human, agents are skipped
  technical_failure  every agent in the plan failed
  agent_unresolved   every agent that answered called `escalate_to_human`
                     (if only some did, we don't escalate, the user got something useful)

A retrieval score doesn't work as a trigger: answerable and unanswerable questions scored
about the same (0.41-0.67 vs 0.37-0.50).

On handoff we open a ticket (if the customer exists), queue a record and tell the user in their
language. `HandoffNotifier` is where Slack/Teams would plug in. The API queues into the SQLite
`HandoffStore` (what operators claim and answer, see app.handoff_store) AND appends a redacted
record (no conversation excerpt) to the JSONL file, which stays as an audit trail. The node itself does not know which: it gets one notifier.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from langchain_core.messages import AIMessage

from app.agents.common import recent_dialogue
from app.config import get_settings
from app.data.repository import CustomerRepository
from app.handoff_store import case_summary
from app.schemas import DEFAULT_CONVERSATION

log = logging.getLogger("handoff")

PRIORITY = {"technical_failure": "high", "agent_unresolved": "normal", "user_requested": "normal"}

# {ref} is the reference the customer can quote
NOTICES = {
    "English": {
        "user_requested": "Of course. I've passed your case to a human agent.",
        "technical_failure": "Sorry, I'm having technical trouble right now, so I've passed your case to a human agent.",
        "agent_unresolved": "I couldn't fully resolve this with the information I have, so I've passed your case to a human agent.",
        "suffix": "Your reference is {ref}. They will follow up with you, and you won't need to repeat the details we already have.",
    },
    "Portuguese": {
        "user_requested": "Claro. Encaminhei o seu caso para um atendente humano.",
        "technical_failure": "Desculpe, estou com um problema técnico agora, então encaminhei o seu caso para um atendente humano.",
        "agent_unresolved": "Não consegui resolver isso por completo com as informações que tenho, então encaminhei o seu caso para um atendente humano.",
        "suffix": "Sua referência é {ref}. Ele entrará em contato e você não precisará repetir os detalhes que já temos.",
    },
    "Spanish": {
        "user_requested": "Claro. He pasado tu caso a un agente humano.",
        "technical_failure": "Lo siento, tengo un problema técnico en este momento, así que he pasado tu caso a un agente humano.",
        "agent_unresolved": "No pude resolverlo por completo con la información que tengo, así que he pasado tu caso a un agente humano.",
        "suffix": "Tu referencia es {ref}. Te contactará y no tendrás que repetir los detalles que ya tenemos.",
    },
}


def handoff_reason(state: dict) -> str | None:
    """Why a human must take over, or None."""
    if state.get("wants_human"):
        return "user_requested"
    plan = state.get("plan") or []
    if not plan:
        return None  # greeting or unclear, nothing to escalate
    outs = state.get("agent_outputs", {})
    ok = [outs[a] for a in plan if a in outs and outs[a].get("answer") and not outs[a].get("error")]
    if not ok:
        return "technical_failure"
    if all(o.get("unresolved") for o in ok):
        return "agent_unresolved"
    return None


class HandoffNotifier(Protocol):
    def notify(self, record: dict) -> None: ...


class FileQueueNotifier:
    """Default notifier: appends to a JSONL file and logs a warning.

    For Slack/Teams/email, write another class with `notify(record)`.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def notify(self, record: dict) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.warning("HANDOFF id=%s reason=%s priority=%s user=%s ticket=%s",
                    record["handoff_id"], record["reason"], record["priority"], record["user_id"], record["ticket_id"])


OPERATOR_LABEL = "[Operador humano]"


def operator_context_message(m: dict) -> AIMessage:
    """What an operator's reply looks like in the conversation the agents read: an assistant-side
    message that starts with the label, so a model never takes it for its own words or the customer's.
    It enters the graph as an ordinary message, so `recent_dialogue` carries it with no change."""
    return AIMessage(content=f"{OPERATOR_LABEL} ({m['decision']}) {m['message']}",
                     additional_kwargs={"source": "human_operator", "handoff_id": m["handoff_id"]})


def defuse_label(text: str) -> str:
    """A customer can type the label too. Only messages that really come from an operator may carry
    it, so in the customer's own text it is turned into a visible quote. (Nothing else is touched.)"""
    return re.sub(re.escape(OPERATOR_LABEL), "[cita: Operador humano]", text, flags=re.IGNORECASE)


AUDIT_FIELDS = ("handoff_id", "created_at", "user_id", "conversation_id", "reason", "priority", "ticket_id", "language")


def audit_record(record: dict) -> dict:
    """The record without what the customer said: no user message, no conversation, no agent findings,
    no summary, no tool arguments. Only who, which conversation, why, when, and which tools ran.
    Deleting a conversation erases its handoffs from the queue; this keeps the audit copy from
    being a second place where its text survives."""
    out = {k: record.get(k) for k in AUDIT_FIELDS}
    out["tools"] = [f"{tc.get('agent')}:{tc.get('name')}" for tc in record.get("tool_calls", [])]
    out["redacted"] = True  # the importer skips these: there is nothing in them to work on
    return out


class AuditNotifier(FileQueueNotifier):
    """Appends the REDACTED record (see `audit_record`) to the JSONL file. Same file and logging as
    FileQueueNotifier, which keeps writing the whole record for anyone who wants that."""

    def notify(self, record: dict) -> None:
        super().notify(audit_record(record))


class FanOutNotifier:
    """Send each record to several notifiers. Every one is tried even if another fails, and the
    first failure is raised at the end (the node logs it, the customer still gets a reply)."""

    def __init__(self, *notifiers: HandoffNotifier):
        self.notifiers = notifiers

    def notify(self, record: dict) -> None:
        first_error: Exception | None = None
        for n in self.notifiers:
            try:
                n.notify(record)
            except Exception as e:  # noqa: BLE001
                log.exception("Notifier %s failed for %s", type(n).__name__, record.get("handoff_id"))
                first_error = first_error or e
        if first_error:
            raise first_error


def compose_notice(reason: str, language: str, ref: str) -> str:
    lang = next((k for k in NOTICES if language.lower().startswith(k.lower()[:4])), "English")
    t = NOTICES[lang]
    return f"{t[reason]} {t['suffix'].format(ref=ref)}"


def make_handoff_node(repo: CustomerRepository | None = None, notifier: HandoffNotifier | None = None):
    s = get_settings()
    repository = repo or CustomerRepository(tickets_file=s.tickets_file)
    queue = notifier or AuditNotifier(s.handoffs_file)

    def handoff_node(state: dict) -> dict:
        reason = handoff_reason(state) or "agent_unresolved"
        user_id = state["user_id"]
        outs = state.get("agent_outputs", {})
        dialogue = recent_dialogue(state["messages"], s.history_window)
        last_user_msg = next((m.content for m in reversed(dialogue) if m.type == "human"), "")
        findings = {a: o.get("answer", "")[:600] for a, o in outs.items() if o.get("answer")}

        handoff_id = f"HND-{uuid.uuid4().hex[:6].upper()}"
        ticket = None
        if repository.exists(user_id):
            ticket = repository.create_ticket(user_id, "human_handoff", f"[{reason}] {str(last_user_msg)[:200]}")
        ticket_id = ticket["ticket_id"] if ticket else None

        record = {
            "handoff_id": handoff_id, "created_at": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id, "conversation_id": state.get("conversation_id") or DEFAULT_CONVERSATION,
            "reason": reason, "priority": PRIORITY[reason], "ticket_id": ticket_id,
            "language": state.get("language", "English"), "user_message": last_user_msg,
            "conversation": [{"role": m.type, "content": m.content} for m in dialogue],
            "agent_findings": findings,
            "tool_calls": [{"agent": a, **tc} for a, o in outs.items() for tc in o.get("tool_calls", [])],
        }
        if state.get("summary"):  # the operator also needs what was said before the window
            record["earlier_summary"] = state["summary"]
        record["case_summary"] = case_summary(record)  # one line for the operator's queue
        try:
            queue.notify(record)
        except Exception:  # noqa: BLE001 - the customer still gets a reply
            log.exception("Handoff notifier failed for %s", handoff_id)

        ref = f"{handoff_id} / {ticket_id}" if ticket_id else handoff_id
        notice = compose_notice(reason, state.get("language", "English"), ref)
        # the customer only sees the notice; what the agents found goes to the human in the record
        # (an unresolved agent can pass off a generic contract clause as a specific answer)
        answer = notice

        info = {"id": handoff_id, "reason": reason, "priority": PRIORITY[reason], "ticket_id": ticket_id,
                "status": "pending"}
        return {"handoff": info, "final_answer": answer, "messages": [AIMessage(content=answer)]}

    return handoff_node
