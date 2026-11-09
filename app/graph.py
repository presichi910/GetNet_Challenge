"""The orchestration: a LangGraph StateGraph.

    START -> router --+--(user asked for a human)------------------------------------> handoff -> END
                      |
                      +--(plan[0])--> agent --(plan[1])--> agent --> ... --+--(nobody resolved)--> handoff -> END
                      |                                                    |
                      +--(plan is empty)-----------------------------------+--(resolved)---------> synthesizer -> END

Each agent node appends its result to `state["agent_outputs"]` and bumps `step`. A single
conditional-edge function (`next_node`) reads the router's plan and sends control to the next
specialist; when the plan is exhausted it asks `handoff_reason` whether a human must take over
(explicit request, technical failure, or no agent could resolve it) and routes to `handoff` or
`synthesizer`. Later agents see earlier outputs.
"""
from __future__ import annotations

from typing import Callable

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.handoff import handoff_reason
from app.state import GraphState

AGENT_NODES = ("knowledge", "support")


def next_node(state: GraphState) -> str:
    if state.get("wants_human"):
        return "handoff"  # explicit request: do not waste LLM calls on specialists
    plan, step = state.get("plan", []), state.get("step", 0)
    if step < len(plan):
        return plan[step]
    return "handoff" if handoff_reason(state) else "synthesizer"


def build_graph(router: Callable, knowledge: Callable, support: Callable, synthesizer: Callable,
                handoff: Callable, checkpointer=None):
    """Nodes are injected so tests can swap LLM-backed agents for deterministic stubs."""
    g = StateGraph(GraphState)
    g.add_node("router", router)
    g.add_node("knowledge", knowledge)
    g.add_node("support", support)
    g.add_node("synthesizer", synthesizer)
    g.add_node("handoff", handoff)

    g.add_edge(START, "router")
    targets = {**{n: n for n in AGENT_NODES}, "synthesizer": "synthesizer", "handoff": "handoff"}
    for src in ("router", *AGENT_NODES):
        g.add_conditional_edges(src, next_node, targets)
    g.add_edge("synthesizer", END)
    g.add_edge("handoff", END)

    # thread_id == user_id gives each customer their own multi-turn memory.
    return g.compile(checkpointer=checkpointer or MemorySaver())


def build_default_graph():
    from app.agents.knowledge import make_knowledge_node
    from app.agents.router import make_router_node
    from app.agents.support import make_support_node
    from app.agents.synthesizer import make_synthesizer_node
    from app.config import get_settings
    from app.data.repository import CustomerRepository
    from app.handoff import make_handoff_node

    repo = CustomerRepository(tickets_file=get_settings().tickets_file)  # shared: handoff tickets show up for Support
    return build_graph(make_router_node(), make_knowledge_node(), make_support_node(repo=repo),
                       make_synthesizer_node(), make_handoff_node(repo=repo))
