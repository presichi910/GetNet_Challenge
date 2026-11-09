"""Thin service layer between the HTTP API (or the eval harness) and the compiled graph."""
from __future__ import annotations

import logging
import time
import uuid

from langchain_core.messages import HumanMessage

from app.schemas import ChatResponse, HandoffInfo, Routing, Source, ToolCall

log = logging.getLogger("chat")


class ChatService:
    def __init__(self, graph):
        self.graph = graph

    def run_raw(self, message: str, user_id: str, thread_id: str | None = None) -> tuple[dict, int]:
        """Invoke the graph; returns (final state, latency_ms)."""
        t0 = time.perf_counter()
        state = self.graph.invoke(
            {"messages": [HumanMessage(content=message)], "user_id": user_id},
            config={"configurable": {"thread_id": thread_id or user_id}, "recursion_limit": 25},
        )
        return state, int((time.perf_counter() - t0) * 1000)

    def handle(self, message: str, user_id: str, thread_id: str | None = None) -> ChatResponse:
        trace_id = uuid.uuid4().hex[:12]
        state, latency_ms = self.run_raw(message, user_id, thread_id)
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
        # One structured line per request: easy to ship to any log stack / build dashboards on.
        log.info("trace=%s user=%s plan=%s used=%s tools=%s degraded=%s handoff=%s latency_ms=%d",
                 trace_id, user_id, resp.routing.plan, resp.agents_used,
                 [t.name for t in tool_calls], resp.degraded, resp.handoff.reason if resp.handoff else None, latency_ms)
        return resp
