"""Helpers shared by the tool-using agents."""
from __future__ import annotations

import logging

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

from app.state import AgentResult
from app.tools.handoff_tools import ESCALATE_TOOL

log = logging.getLogger(__name__)

SUMMARY_HEADER = "Summary of the earlier part of this conversation (those messages were removed):\n"
MAX_CONTEXT_CHARS = 8000  # enough for a full retrieval, so the judge sees everything


def recent_dialogue(messages: list[AnyMessage], window: int, summary: str = "") -> list[AnyMessage]:
    """Last few human/AI messages, without tool chatter. With a `summary` (the rolling summary of
    older messages) it goes first as a system message. Without one the result is unchanged."""
    dialogue = [m for m in messages if isinstance(m, (HumanMessage, AIMessage)) and m.content]
    dialogue = dialogue[-window:]
    if summary:
        return [SystemMessage(content=SUMMARY_HEADER + summary), *dialogue]
    return dialogue


def format_prior_context(agent_outputs: dict[str, AgentResult]) -> str:
    if not agent_outputs:
        return ""
    lines = ["## Findings from other agents for this same request"]
    for name, out in agent_outputs.items():
        if out.get("answer"):
            lines.append(f"- {name}: {out['answer']}")
    return "\n".join(lines) if len(lines) > 1 else ""


NUDGE = ("(system note) You answered without calling any tool. Call the appropriate tool first "
         "and base your answer only on its result.")


def run_tool_agent(name: str, llm, tools, system_prompt: str, messages: list[AnyMessage],
                   attempts: int = 3, require_tool: bool = False) -> AgentResult:
    """Run a tool agent and pull out the answer, tool calls, sources and evidence.

    Groq models sometimes send a broken tool call (400 `tool_use_failed`), so we retry.
    """
    agent = create_agent(llm, tools=tools, system_prompt=system_prompt)
    last_err: Exception | None = None
    nudged = False
    attempt = 0
    while attempt < attempts:
        attempt += 1
        try:
            result = agent.invoke({"messages": messages}, config={"recursion_limit": 12})
        except Exception as e:  # noqa: BLE001 - never crash the graph
            last_err = e
            log.warning("%s agent attempt %d/%d failed: %s", name, attempt, attempts, str(e)[:200])
            continue
        used_tool = any(isinstance(m, ToolMessage) for m in result["messages"])
        if require_tool and not used_tool and not nudged:
            # no tool used -> ask again, an ungrounded answer is worse than a retry
            nudged = True
            attempt -= 1  # the nudge doesn't count as an error retry
            messages = [*messages, HumanMessage(content=NUDGE)]
            log.warning("%s agent answered without a tool; retrying with a nudge", name)
            continue
        break
    else:
        return AgentResult(agent=name, answer="", sources=[], tool_calls=[], contexts=[],
                           error=f"{type(last_err).__name__}: {last_err}", unresolved=False)

    out_msgs = result["messages"]
    tool_calls, contexts, sources, seen = [], [], [], set()
    for m in out_msgs:
        if isinstance(m, AIMessage):
            tool_calls.extend({"name": tc["name"], "args": tc["args"]} for tc in m.tool_calls)
        elif isinstance(m, ToolMessage):
            contexts.append(f"[{m.name}] {str(m.content)[:MAX_CONTEXT_CHARS]}")
            for src in (m.artifact or []) if isinstance(m.artifact, list) else []:
                if src.get("url") and src["url"] not in seen:
                    seen.add(src["url"])
                    sources.append(src)
    final = next((m for m in reversed(out_msgs) if isinstance(m, AIMessage) and m.content and not m.tool_calls), None)
    answer = str(final.content if final else "").strip()
    escalations = [tc for tc in tool_calls if tc["name"] == ESCALATE_TOOL]
    unresolved = bool(escalations)
    if unresolved and not answer:
        answer = str(escalations[0]["args"].get("reason", "escalated"))  # so it isn't treated as a failure
    return AgentResult(agent=name, answer=answer, sources=sources, tool_calls=tool_calls,
                       contexts=contexts, error=None if answer else "empty_answer", unresolved=unresolved)
