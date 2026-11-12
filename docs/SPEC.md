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
| F26 | The JWT carries a `role` (`merchant` default, `operator`). An operator is 403 on `/chat` and `/conversations*` even with the `chat` scope; a merchant is 403 on `/operator/*` even with the `operator` scope; an unknown role is 401 | `tests/test_roles.py`, `tests/test_operator_api.py` (`test_a_merchant_is_403_on_every_operator_endpoint_...`, `test_an_operator_is_403_on_chat_...`) |
| F27 | Handoffs are stored in SQLite (`HANDOFFS_DB_PATH`) with status `pending`, `claimed`, `resolved`; the JSONL stays as an audit copy; the old JSONL is imported at startup, idempotently | `tests/test_handoff_store.py` (`test_import_...`), `test_startup_imports_the_old_queue_once_...` |
| F28 | `GET /operator/handoffs?status=` lists oldest first; `claim` is pending to claimed, idempotent for the same operator, 409 for another one or a resolved handoff; unknown id is 404 | `tests/test_operator_api.py`, `tests/test_handoff_store.py` |
| F29 | Transitions are atomic (`UPDATE ... WHERE status = ...` and rows counted): simultaneous claims have exactly one winner, through threads, separate connections and the API | `test_two_simultaneous_claims_...`, `test_many_operators_claiming_at_once_exactly_one_wins` |
| F30 | `reply` (approve or reject, message up to 1000 characters, `idempotency_key`) is allowed only to the operator who claimed it; the same key repeats the result without a second message; another key on a resolved handoff, an unclaimed one or one held by another operator is 409 | `test_reply_...` in `tests/test_operator_api.py`, `tests/test_handoff_store.py` |
| F31 | The operator message is stored in SQLite and delivered once in `operator_messages` of the next `/chat` of that conversation (and readable at `GET /conversations/{id}/operator-messages`); it is given back if that turn fails | `test_full_flow_...`, `test_a_failed_turn_gives_the_message_back_...` |
| F32 | `/chat` shows `handoff.status` and the conversation's `open_handoffs`, never the operator's identity; a merchant only sees and receives their own conversations' data | `test_merchant_b_does_not_see_or_receive_...`, `test_a_merchant_response_never_carries_operator_identity_...` |
| F33 | Every operator action (create, claim, reply, release, close, lease expiry) is stored in `handoff_events` with operator id and timestamp; refused attempts, renewals and replays are not recorded as actions | `test_every_operator_action_is_recorded_...`, `test_release_and_close_are_audited_...`, `test_expiry_is_recorded_...` |
| F34 | `DELETE /conversations/{id}` also erases that conversation's handoffs, operator messages and events (matched by the token's user), with `secure_delete` and a truncated WAL; an erase failure is a 500, never a 204 with a copy left | `tests/test_handoff_privacy.py` (`test_deleting_a_conversation_erases_...` searches a token in `handoffs.sqlite` and its `-wal`) |
| F35 | The JSONL audit file carries no conversation excerpt (no message, dialogue, findings, summary or tool arguments); the importer skips those lines | `test_the_audit_file_has_no_conversation_text_...`, `test_tool_arguments_do_not_reach_the_audit_file` |
| F36 | Handoffs expire with `THREAD_TTL_DAYS` (last activity, any status), with their messages and events, at startup and every purge interval | `test_idle_handoffs_expire_...`, `test_the_lifespan_purges_expired_handoffs_at_startup` |
| F37 | A claim holds for `HANDOFF_LEASE_MINUTES` (default 15, renewable by claiming again); an expired claim is pending again when anyone lists, reads, claims or replies; the former holder can not reply | `tests/test_handoff_lease.py` (injected clock) |
| F38 | `POST /operator/handoffs/{id}/reject {reason, close}` needs a valid claim: `close: false` releases to pending, `close: true` resolves as reject with no customer message; reason is audited and never shown to the customer; invalid transitions are 409 | `tests/test_handoff_release.py` |
| F39 | The operator reply enters the agents' context as an assistant-side `[Operador humano] (decision) text` message, once, through `recent_dialogue`; with no reply the agents' input is identical to before; a customer typing the label gets it turned into a quote | `tests/test_handoff_resume.py` |
| F40 | `GET /conversations/{id}` reports `pending_human` (a handoff of the conversation is pending or claimed) | `test_pending_human_...` in `tests/test_operator_api.py` |
| F41 | The old JSONL queue is imported at startup only with `HANDOFF_IMPORT_JSONL=true` (default false); when off and the file has unimported records, the log says how many | `test_the_old_file_is_not_imported_by_default_...`, `test_with_the_flag_on_...` |

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
Guardrail classifiers, tracing backend, dashboards/alerts, an identity provider (RS256/JWKS, refresh, revocation), rate limiting, real customer backend, an operator UI, a real notifier (Slack/Teams/helpdesk) for the handoff queue, reassigning a claimed case to a named operator, and encryption of `handoffs.sqlite`.

## 6. Open decisions (resolved with the product owner)
- Routing style: multi-step plan (chosen) vs single-agent vs supervisor loop.
- Customer data: mock JSON in-process (chosen).
- Memory: LangGraph checkpoints in SQLite (`SqliteSaver`, `CHECKPOINT_DB_PATH`), `thread_id = "<user_id>:<conversation_id>"` with `user_id` taken from the JWT (chosen). Rolling summary above `SUMMARY_THRESHOLD` messages, only the latest checkpoint per thread kept, TTL of idle threads (`THREAD_TTL_DAYS`) and a limit of conversations per customer (`MAX_THREADS_PER_USER`, HTTP 429). The eval and tests that pass no checkpointer still use `MemorySaver`. See README, "Conversation memory".
- Models: one variable per role (`LLM_MODEL` knowledge, `LIGHT_MODEL` router/support/synthesizer, `JUDGE_MODEL`), passed to the containers by docker-compose (chosen). Support has its own `SUPPORT_MODEL` (default `LIGHT_MODEL`, set to `openai/gpt-oss-120b` in `.env.example`) because the 20b skips `get_recent_transactions` on declines (`q08`); a model change was chosen over a prompt change because the 20b is very sensitive to prompt wording. Not yet confirmed with a full eval (`docs/VERIFICATION_DEBT.md`).
- Identity: JWT HS256 with a shared secret for the demo (chosen); the id comes only from `sub`. Body `user_id` kept optional for compatibility with the original contract.
- Evaluation: offline dataset + LLM judge (chosen).
- Handoff action: ticket + SQLite operator queue behind a pluggable notifier (chosen); operators work through `/operator/*` (claim, reply approve or reject), and the answer is delivered in the customer's next `/chat` turn. No webhook yet. See README, "Human oversight".
- Operator reply delivery: stored in SQLite, returned as `operator_messages` on the customer's next turn, and added to the agents' context on that turn as an assistant-side message labelled `[Operador humano] (approve|reject) <text>`, through the ordinary message history (chosen, replaces the earlier decision to keep it out of the graph). No prompt, node or router changed; with no reply the input is identical to before. The label is the trust marker, so a customer's own `[Operador humano]` is turned into a quote. The behavior of the real LLM with that message (and `x04`) is not yet confirmed (`docs/VERIFICATION_DEBT.md`, item 12).
- Claims: a lease (`HANDOFF_LEASE_MINUTES`, 15) instead of a permanent claim, swept lazily on access, no background job (chosen). `reject` releases (default) or closes without a customer message.
- Privacy of handoffs: erased with their conversation and by the same TTL; the JSONL audit file is redacted (chosen over keeping excerpts there). Importing the old file is opt-in (`HANDOFF_IMPORT_JSONL`).
- Roles: a `role` claim (`merchant` default, `operator`) on top of the scopes; each endpoint family needs its own role (chosen).
- Handoff triggers: explicit user request, technical failure, agent-reported low confidence (chosen); user frustration detection left out as too subjective to evaluate.
