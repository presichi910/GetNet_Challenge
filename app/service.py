"""Sits between the API (or the eval) and the graph."""
from __future__ import annotations

import logging
import time
import uuid

from datetime import datetime, timezone

from langchain_core.messages import AIMessage, HumanMessage

from app.handoff import defuse_label, operator_context_message
from app.schemas import (DEFAULT_CONVERSATION, ChatResponse, HandoffInfo, OpenHandoff, OperatorMessage, Routing,
                         Source, ToolCall)

log = logging.getLogger("chat")


def thread_id_for(user_id: str, conversation_id: str | None = None) -> str:
    """Memory key. It always starts with the authenticated user, so one user can never
    address another user's conversation, whatever conversation_id they send."""
    return f"{user_id}:{conversation_id or DEFAULT_CONVERSATION}"


class ChatService:
    def __init__(self, graph, memory=None, handoffs=None):
        self.graph = graph
        self.memory = memory  # app.memory.Memory: thread registry (limit, owner, last activity)
        # app.handoff_store.HandoffStore: the human queue. It is NOT part of the graph state or the
        # checkpointer, so conversations can change storage without touching human oversight.
        self.handoffs = handoffs

    def run_raw(self, message: str, user_id: str, thread_id: str | None = None,
                conversation_id: str | None = None, context: list | None = None) -> tuple[dict, int]:
        """Run the graph. Returns (state, latency_ms). `context`: messages that go into the conversation
        just before the customer's (an operator's reply). None or empty: the input is exactly what it
        always was."""
        t0 = time.perf_counter()
        state = self.graph.invoke(
            {"messages": [*(context or []), HumanMessage(content=message)], "user_id": user_id,
             "conversation_id": conversation_id or DEFAULT_CONVERSATION},
            config={"configurable": {"thread_id": thread_id or thread_id_for(user_id)}, "recursion_limit": 25},
        )
        return state, int((time.perf_counter() - t0) * 1000)

    # ---- the caller's own conversations. The owner is always the authenticated user_id; a thread that
    # is somebody else's or does not exist is the same answer (None / False), so nothing leaks. ----

    @staticmethod
    def _iso(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat()

    def _info(self, meta: dict) -> dict:
        return {"conversation_id": meta["conversation_id"], "created_at": self._iso(meta["created_at"]),
                "last_active": self._iso(meta["last_active"]), "turns": meta["turns"]}

    def conversation_limit(self) -> int | None:
        return (self.memory.max_threads or None) if self.memory else None

    def list_conversations(self, user_id: str) -> list[dict]:
        return [self._info(m) for m in self.memory.list_threads(user_id)] if self.memory else []

    def get_conversation(self, user_id: str, conversation_id: str) -> dict | None:
        if not self.memory:
            return None
        thread_id = thread_id_for(user_id, conversation_id)
        meta = self.memory.get_thread(user_id, thread_id)
        if meta is None:
            return None
        values = self.graph.get_state({"configurable": {"thread_id": thread_id}}).values
        messages = [{"role": "user" if isinstance(m, HumanMessage) else "assistant", "content": str(m.content)}
                    for m in values.get("messages", []) if isinstance(m, (HumanMessage, AIMessage)) and m.content]
        return {**self._info(meta), "summary": values.get("summary") or None, "messages": messages}

    def delete_conversation(self, user_id: str, conversation_id: str) -> bool:
        """Erase the conversation AND the handoffs of that conversation (cases, operator messages and
        their audit events), so deleting a conversation leaves no copy of it in the human queue.
        True if anything of the caller's went; the handoffs are matched by the token's user_id. An
        erase error propagates (500) rather than saying 204 with a copy left behind."""
        deleted = bool(self.memory) and self.memory.delete_thread_for(user_id, thread_id_for(user_id, conversation_id))
        # not caught: if the erase fails the client gets a 500 and retries, and the retry finds the
        # leftover handoffs (the conversation itself is already gone) and finishes the job
        erased = self.handoffs.delete_conversation(user_id, conversation_id) if self.handoffs else 0
        return deleted or erased > 0

    def operator_messages(self, user_id: str, conversation_id: str) -> list[dict] | None:
        """Every operator message of the caller's own conversation, or None if the conversation is not
        the caller's / does not exist (same answer for both). Never marks anything delivered."""
        if not self.handoffs:
            return None
        if self.memory:
            if self.memory.get_thread(user_id, thread_id_for(user_id, conversation_id)) is None:
                return None
        elif not self.handoffs.has_conversation(user_id, conversation_id):
            return None
        return [{**m, "created_at": self._iso(m["created_at"])}
                for m in self.handoffs.messages_for_conversation(user_id, conversation_id)]

    def _take_operator_messages(self, user_id: str, conversation_id: str) -> list[dict]:
        """The operator messages of this conversation not delivered yet, marked delivered in the same
        transaction (two simultaneous turns cannot both take one). They go into the conversation the
        agents read and into this turn's response. A problem here never fails the turn: nothing is
        taken and the message comes with the next one."""
        if not self.handoffs:
            return []
        try:
            return self.handoffs.pop_undelivered(user_id, conversation_id)
        except Exception:  # noqa: BLE001
            log.warning("could not read operator messages for user=%s conv=%s", user_id, conversation_id, exc_info=True)
            return []

    def _give_back(self, user_id: str, conversation_id: str, taken: list[dict]) -> None:
        """The turn failed: the messages it took were not delivered, so they are not marked delivered."""
        try:
            self.handoffs.restore_undelivered(user_id, conversation_id, [m["handoff_id"] for m in taken])
        except Exception:  # noqa: BLE001 - still readable at /conversations/{id}/operator-messages
            log.warning("could not give back operator messages for user=%s conv=%s", user_id, conversation_id, exc_info=True)

    def _human_side(self, user_id: str, conversation_id: str, handoff: HandoffInfo | None):
        """What the customer may learn about the humans: the status of this turn's handoff and the cases
        of this conversation still open. Every query is filtered by the token's user_id. A problem
        here never fails the turn."""
        if not self.handoffs:
            return handoff, []
        try:
            if handoff:
                handoff = handoff.model_copy(
                    update={"status": self.handoffs.status_for(user_id, handoff.id) or handoff.status})
            open_cases = [OpenHandoff(**h) for h in self.handoffs.open_for_conversation(user_id, conversation_id)
                          if not handoff or h["id"] != handoff.id]
            return handoff, open_cases
        except Exception:  # noqa: BLE001
            log.warning("could not read the handoff queue for user=%s conv=%s", user_id, conversation_id, exc_info=True)
            return handoff, []

    def handle(self, message: str, user_id: str, conversation_id: str | None = None) -> ChatResponse:
        """`user_id` must be the authenticated user. The memory thread is derived from it here."""
        trace_id = uuid.uuid4().hex[:12]
        conversation_id = conversation_id or DEFAULT_CONVERSATION
        thread_id = thread_id_for(user_id, conversation_id)
        # raises ThreadLimitExceeded for a new thread above the per-user limit, before anything runs
        created = self.memory.begin_turn(user_id, thread_id) if self.memory else False
        taken = self._take_operator_messages(user_id, conversation_id)
        try:
            state, latency_ms = self.run_raw(defuse_label(message), user_id, thread_id, conversation_id,
                                             context=[operator_context_message(m) for m in taken])
        except Exception:
            if taken:
                self._give_back(user_id, conversation_id, taken)
            if created:  # the first turn of a thread failed: do not keep its slot or its leftovers
                self.memory.abort_new_thread(thread_id)
            raise
        if self.memory:
            try:
                self.memory.complete_turn(thread_id)
                self.memory.prune_thread(thread_id)  # only the latest checkpoint is ever needed
            except Exception:  # noqa: BLE001 - the reply is ready, a prune problem must not lose it
                log.warning("checkpoint prune failed for %s", thread_id, exc_info=True)
        outputs = state.get("agent_outputs", {})

        sources, seen = [], set()
        tool_calls = []
        for agent, out in outputs.items():
            tool_calls += [ToolCall(agent=agent, **tc) for tc in out.get("tool_calls", [])]
            for s in out.get("sources", []):
                if s["url"] not in seen:
                    seen.add(s["url"])
                    sources.append(Source(**s))

        handoff, open_handoffs = self._human_side(
            user_id, conversation_id, HandoffInfo(**state["handoff"]) if state.get("handoff") else None)
        operator_messages = [OperatorMessage(**{**m, "created_at": self._iso(m["created_at"])}) for m in taken]

        resp = ChatResponse(
            response=state["final_answer"],
            user_id=user_id,
            conversation_id=conversation_id,
            agents_used=list(outputs.keys()),
            routing=Routing(plan=state.get("plan", []), reasoning=state.get("routing_reasoning", ""),
                            language=state.get("language", "")),
            sources=sources,
            tool_calls=tool_calls,
            degraded=any(o.get("error") for o in outputs.values()),
            handoff=handoff,
            open_handoffs=open_handoffs,
            operator_messages=operator_messages,
            latency_ms=latency_ms,
            trace_id=trace_id,
        )
        # one log line per request
        log.info("trace=%s user=%s conv=%s plan=%s used=%s tools=%s degraded=%s handoff=%s latency_ms=%d",
                 trace_id, user_id, conversation_id, resp.routing.plan, resp.agents_used,
                 [t.name for t in tool_calls], resp.degraded, resp.handoff.reason if resp.handoff else None, latency_ms)
        return resp
