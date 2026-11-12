"""The LangGraph graph.

    router -> agents in the plan, one by one -> synthesizer
    router (user wants a human) or agents (nobody resolved it) -> handoff

`next_node` picks where to go next: the next agent in the plan, then handoff or synthesizer.
Each agent saves its result in `agent_outputs`, so later agents can read earlier ones.
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
        return "handoff"  # user asked for a human, skip the agents
    plan, step = state.get("plan", []), state.get("step", 0)
    if step < len(plan):
        return plan[step]
    return "handoff" if handoff_reason(state) else "synthesizer"


def build_graph(router: Callable, knowledge: Callable, support: Callable, synthesizer: Callable,
                handoff: Callable, checkpointer=None):
    """Nodes are passed in so tests can use stubs."""
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

    # thread_id = "<user_id>:<conversation_id>" (see service.thread_id_for), so each customer
    # has their own memory and nobody can reach another customer's thread
    return g.compile(checkpointer=checkpointer or MemorySaver())


def build_default_graph(checkpointer=None):
    """`checkpointer=None` keeps the memory in process (the eval does that, so it never writes
    conversations to disk). The API passes the persistent one, see `app.main.lifespan`."""
    from app.agents.knowledge import make_knowledge_node
    from app.agents.router import make_router_node
    from app.agents.support import make_support_node
    from app.agents.synthesizer import make_synthesizer_node
    from app.config import get_settings
    from app.data.repository import CustomerRepository
    from app.handoff import make_handoff_node

    repo = CustomerRepository(tickets_file=get_settings().tickets_file)  # shared, so handoff tickets show up for Support
    return build_graph(make_router_node(), make_knowledge_node(), make_support_node(repo=repo),
                       make_synthesizer_node(), make_handoff_node(repo=repo), checkpointer=checkpointer)
