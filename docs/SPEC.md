# Spec: Getnet Multi-Agent Support System

Written before implementation (Spec-Driven Development). Each requirement has an acceptance check that is either an automated test or an evaluation case.

## 1. Goal
HTTP service that answers Getnet customer messages by orchestrating specialised agents, grounded in Getnet's website and in the caller's account data, packaged with Docker.

## 2. Functional requirements

| ID | Requirement | Acceptance check |
|---|---|---|
| F1 | `POST /chat` accepts `{message}` (optional `user_id`, must match the token; optional `conversation_id`) with a Bearer JWT and returns JSON with the answer | `tests/test_api.py` |
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
| F17 | `/chat` requires a valid JWT: HS256 only, signature, `exp`, `iss`, `aud`, `sub` checked; anything else is 401 with a generic message. `/health` stays public | `tests/test_auth.py` |
| F18 | The `chat` scope is required (403 otherwise) | `test_missing_chat_scope_is_403` |
| F19 | The customer is the token `sub`. A body `user_id` of another customer is 403 and never reaches the orchestrator; the LLM never chooses the id (tools are bound to it) | `test_body_user_id_of_another_customer_is_403`, `test_end_to_end_tools_only_return_the_token_users_data`, `test_support_prompt_uses_the_authenticated_id_not_text_from_the_user` |
| F20 | Memory thread is `<sub>:<conversation_id>`: nobody can address another customer's conversation | `test_two_users_same_conversation_id_get_different_memory_threads`, `test_api_derives_the_memory_thread_from_the_token` |
| F21 | The app does not start with an empty, short or placeholder `JWT_SECRET` | `test_app_refuses_to_start_with_a_weak_secret` |
| F22 | No tool exposes a parameter that selects a user (checked on the schema of every tool, found automatically) | `tests/test_tool_schemas.py` |
| F23 | The eval marks provider errors (rate limit, quota, outage) as `error`: excluded from every metric, judge skipped, `latest.md` not overwritten | `tests/test_eval.py` (`test_provider_errors_do_not_count_in_any_metric`, ...) |
| F24 | The eval waits out per-minute rate limits and retries, never retries a daily quota (checked on the full error text), states the model of each role in the report, and a partial run does not replace `latest.md` | `tests/test_eval.py` (`test_a_real_daily_quota_message_is_not_retried_...`, `test_the_report_says_which_models_ran_...`) |
| F25 | The Support agent runs on `SUPPORT_MODEL` (empty = `LIGHT_MODEL`); Router and Synthesizer keep `LIGHT_MODEL`; compose and `.env.example` list every model variable | `tests/test_models.py` |

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
Guardrail classifiers, tracing backend, dashboards/alerts, an identity provider (RS256/JWKS, refresh, revocation), rate limiting, real customer backend, an operator UI or a real notifier (Slack/Teams/helpdesk) for the handoff queue.

## 6. Open decisions (resolved with the product owner)
- Routing style: multi-step plan (chosen) vs single-agent vs supervisor loop.
- Customer data: mock JSON in-process (chosen).
- Memory: LangGraph `MemorySaver`, `thread_id = "<user_id>:<conversation_id>"` with `user_id` taken from the JWT (chosen).
- Models: one variable per role (`LLM_MODEL` knowledge, `LIGHT_MODEL` router/support/synthesizer, `JUDGE_MODEL`), passed to the containers by docker-compose (chosen). Support has its own `SUPPORT_MODEL` (default `LIGHT_MODEL`, set to `openai/gpt-oss-120b` in `.env.example`) because the 20b skips `get_recent_transactions` on declines (`q08`); a model change was chosen over a prompt change because the 20b is very sensitive to prompt wording. Not yet confirmed with a full eval (`docs/VERIFICATION_DEBT.md`).
- Identity: JWT HS256 with a shared secret for the demo (chosen); the id comes only from `sub`. Body `user_id` kept optional for compatibility with the original contract.
- Evaluation: offline dataset + LLM judge (chosen).
- Handoff action: ticket + local operator queue behind a pluggable notifier (chosen); no webhook yet.
- Handoff triggers: explicit user request, technical failure, agent-reported low confidence (chosen); user frustration detection left out as too subjective to evaluate.
