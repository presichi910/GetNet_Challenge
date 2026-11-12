"""Sits between the API (or the eval) and the graph."""
from __future__ import annotations

import logging
import time
import uuid

from langchain_core.messages import HumanMessage

from app.schemas import DEFAULT_CONVERSATION, ChatResponse, HandoffInfo, Routing, Source, ToolCall

log = logging.getLogger("chat")


def thread_id_for(user_id: str, conversation_id: str | None = None) -> str:
    """Memory key. It always starts with the authenticated user, so one user can never
    address another user's conversation, whatever conversation_id they send."""
    return f"{user_id}:{conversation_id or DEFAULT_CONVERSATION}"


class ChatService:
    def __init__(self, graph, memory=None):
        self.graph = graph
        self.memory = memory  # app.memory.Memory: thread registry (limit, owner, last activity)

    def run_raw(self, message: str, user_id: str, thread_id: str | None = None) -> tuple[dict, int]:
        """Run the graph. Returns (state, latency_ms)."""
        t0 = time.perf_counter()
        state = self.graph.invoke(
            {"messages": [HumanMessage(content=message)], "user_id": user_id},
            config={"configurable": {"thread_id": thread_id or thread_id_for(user_id)}, "recursion_limit": 25},
        )
        return state, int((time.perf_counter() - t0) * 1000)

    def handle(self, message: str, user_id: str, conversation_id: str | None = None) -> ChatResponse:
        """`user_id` must be the authenticated user. The memory thread is derived from it here."""
        trace_id = uuid.uuid4().hex[:12]
        conversation_id = conversation_id or DEFAULT_CONVERSATION
        thread_id = thread_id_for(user_id, conversation_id)
        # raises ThreadLimitExceeded for a new thread above the per-user limit, before anything runs
        created = self.memory.begin_turn(user_id, thread_id) if self.memory else False
        try:
            state, latency_ms = self.run_raw(message, user_id, thread_id)
        except Exception:
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
            handoff=HandoffInfo(**state["handoff"]) if state.get("handoff") else None,
            latency_ms=latency_ms,
            trace_id=trace_id,
        )
        # one log line per request
        log.info("trace=%s user=%s conv=%s plan=%s used=%s tools=%s degraded=%s handoff=%s latency_ms=%d",
                 trace_id, user_id, conversation_id, resp.routing.plan, resp.agents_used,
                 [t.name for t in tool_calls], resp.degraded, resp.handoff.reason if resp.handoff else None, latency_ms)
        return resp
