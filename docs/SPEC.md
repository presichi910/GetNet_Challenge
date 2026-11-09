# Spec: Getnet Multi-Agent Support System

Written before implementation (Spec-Driven Development). Each requirement has an acceptance check that is either an automated test or an evaluation case.

## 1. Goal
HTTP service that answers Getnet customer messages by orchestrating specialised agents, grounded in Getnet's website and in the caller's account data, packaged with Docker.

## 2. Functional requirements

| ID | Requirement | Acceptance check |
|---|---|---|
| F1 | `POST /chat` accepts `{message, user_id}` and returns JSON with the answer | `tests/test_api.py` |
| F2 | Invalid payloads are rejected with 422; internal errors never leak details | `tests/test_api.py` |
| F3 | A Router agent outputs an ordered plan of specialists (possibly empty) and the user's language | `tests/test_agents.py`, eval routing accuracy |
| F4 | Plan execution is sequential; later agents can read earlier agents' findings | `tests/test_graph.py` |
| F5 | Knowledge agent answers Getnet questions via RAG and non-Getnet questions via web search | eval cases q01-q10 (tool-use accuracy) |
| F6 | Support agent exposes >= 2 tools over the caller's own data | `tests/test_customer_tools.py` |
| F7 | The model can never select which customer's data is read | `test_tools_are_bound_to_one_user`, eval `x04` |
| F8 | Answers are in the user's language | eval `x01` (Portuguese) |
| F9 | Responses expose agents used, routing, sources, tool calls, latency, trace id | `tests/test_api.py` |
| F10 | Per-user multi-turn memory | `test_memory_is_per_user_and_per_turn_state_resets` |
| F11 | An explicit request for a human skips the specialists and hands the case over | `tests/test_handoff.py`, eval `h01`, `h02` |
| F12 | If every specialist fails, the case is handed to a human (priority high) instead of a generic apology | `test_technical_failure_...`, `test_degraded_flag_when_specialist_fails` |
| F13 | If no specialist can resolve the request (each calls `escalate_to_human`), the case is handed to a human; mixed outcomes are not escalated | `test_handoff_reason` (parametrised), eval `h03` |
| F14 | A handoff opens a ticket (if the customer exists), appends a context-rich record to an operator queue, and tells the customer in their language with a reference id | `test_handoff_creates_ticket_queue_record_and_notice`, `test_notice_is_localised_...` |
| F15 | The customer never sees content from an agent that flagged itself unresolved; it goes to the operator | `test_unresolved_findings_go_to_the_human_not_to_the_customer` |
| F16 | Normal questions are NOT escalated (handoff false-positive rate is measured) | eval `expect_handoff` on q01-q10, x01, x02, x04 |

## 3. Non-functional requirements

| ID | Requirement | Acceptance check |
|---|---|---|
| N1 | Any single LLM/tool failure degrades the answer, never crashes the request | `test_agent_failure_is_contained`, `test_router_falls_back...`, container smoke test without API keys |
| N2 | Builds and runs with `docker compose` | Dockerfile + compose, HEALTHCHECK |
| N3 | Ingestion is idempotent and resumable | deterministic chunk ids; raw-page cache; batch retry/backoff |
| N4 | Ingestion respects embedding-provider rate limits | batch size + sleep settings |
| N5 | Answer quality is measurable | `eval/` (routing, tool use, LLM-judge, handoff decisions, latency) |
| N6 | A failing notifier or queue write never prevents the customer from getting a reply | `test_notifier_failure_does_not_break_the_reply` |
| N7 | Provider rate limits degrade latency, not correctness (query embedding is retried with back-off) | `test_embedding_backoff_*` |

## 4. Orchestration contract

State (`app/state.py`): `messages` (persistent, reducer `add_messages`), `user_id`, and per-turn fields `plan`, `step`, `language`, `agent_outputs`, `final_answer` (reset by the router every turn).

Graph: `START -> router -> {support|knowledge}* -> (synthesizer | handoff) -> END`. The next node is `plan[step]`; when the plan is exhausted it is `handoff` if `handoff_reason(state)` is set, otherwise `synthesizer`. If `wants_human` is set, the router goes straight to `handoff`.

Handoff triggers (`handoff_reason`, a pure function of the state):
- `user_requested`: the router set `wants_human` (explicit request, any language).
- `technical_failure`: every agent in the plan failed (no answer or error).
- `agent_unresolved`: every agent that answered called `escalate_to_human`.
- Otherwise none. A retrieval-relevance threshold was rejected: measured scores of answerable (0.41-0.67) and unanswerable (0.37-0.50) questions overlap.

Routing rules:
- Account-specific (deposits, terminal, declines, transactions) -> `support`.
- Product/how-to/general-purpose -> `knowledge` (which decides RAG vs web search).
- Both needed -> `support` first, then `knowledge`.
- Greeting/unclear/out-of-scope -> empty plan, synthesizer answers directly.
- Router failure -> `["knowledge"]`.

Synthesizer: 0 outputs -> direct reply; 1 -> passthrough; 2+ -> merge; all failed -> static fallback.

## 5. Out of scope (for now)
Guardrail classifiers, tracing backend, dashboards/alerts, authentication, real customer backend, an operator UI or a real notifier (Slack/Teams/helpdesk) for the handoff queue.

## 6. Open decisions (resolved with the product owner)
- Routing style: multi-step plan (chosen) vs single-agent vs supervisor loop.
- Customer data: mock JSON in-process (chosen).
- Memory: LangGraph `MemorySaver`, `thread_id = user_id` (chosen).
- Evaluation: offline dataset + LLM judge (chosen).
- Handoff action: ticket + local operator queue behind a pluggable notifier (chosen); no webhook yet.
- Handoff triggers: explicit user request, technical failure, agent-reported low confidence (chosen); user frustration detection left out as too subjective to evaluate.
