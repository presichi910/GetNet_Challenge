# V4 review: what changed, with evidence

For the reviewer of the V4 delivery (official grade 6.0/10, about 7.3 before the Critical Security Gate). Every criterion and gate of the feedback is mapped below to what changed, the commits, the tests (exact names) and the reports, and to what is still pending and why. Commits are on `main`. Reports are under `eval/reports/`.

Test suite: `pytest -q -m "not eval"`, 455 tests, no network and no API keys (CI runs the same on Python 3.12).

## Critical Security Gate (the cap of V4)

V4 problem: `POST /chat` took an unauthenticated `user_id` that chose both whose data was read and the memory thread (`thread_id = user_id`).

| What changed | Commits | Tests | Evidence |
|---|---|---|---|
| JWT (PyJWT, HS256 fixed in code, `exp`/`iss`/`aud`/`sub` required, weak secret = the app does not start). The customer is the token `sub`; a body `user_id` is optional and must equal it (403) | `6e6eeb3`, `087d50a` | `tests/test_auth.py` (valid, expired, forged, wrong algorithm, wrong scope/role) | README "Authentication" |
| `conversation_id` separate from the user: `thread_id = "<sub>:<conversation_id>"` | `6e6eeb3` | `tests/test_memory.py::test_two_users_never_cross_threads_with_the_persistent_saver` | `app/service.py::thread_id_for` |
| Conversation ownership: registry of owners, `GET/DELETE /conversations/{id}`, somebody else's / missing / malformed = the same 404 | `5cdcdb5`, `dc8b62a`, `dd18dd2` | `tests/test_conversations_api.py::test_b_cannot_read_or_delete_the_thread_of_a_and_it_stays_intact`, `::test_somebody_elses_missing_and_malformed_ids_are_indistinguishable`, `::test_the_owner_comes_from_the_token_never_from_the_path_query_or_body` | README "Conversation memory" |
| Support tools bound by closure to the token user; no tool has a user parameter | `6e6eeb3` | `tests/test_customer_tools.py::test_tools_are_bound_to_one_user`, `tests/test_tool_schemas.py::test_no_tool_exposes_a_parameter_that_selects_a_user` | eval `x04` |

**The two negative tests the review asked for** (one customer can not act as another):

1. `tests/test_auth.py::test_body_user_id_of_another_customer_is_403`: a valid token of `cliente1988` with `"user_id": "cliente2024"` in the body is 403 and nothing runs.
2. `tests/test_auth.py::test_prompt_asking_for_another_customer_still_runs_as_the_token_user`: a prompt that asks for another customer's data runs with the token's user; the tools can only read that user.

More of the same kind: `tests/test_memory_api.py::test_b_sends_the_conversation_id_of_a_and_sees_nothing_of_a`, `tests/test_operator_api.py::test_merchant_b_does_not_see_or_receive_the_message_for_merchant_a`, `tests/test_handoff_resume.py::test_another_merchants_turns_never_see_it_and_do_not_use_it_up`. With the real LLM, `x04` (asking for another customer's transactions) refused without calling a data tool in 4 of 4 runs on Fireworks (`eval/reports/REPEATS-20261112.md`) and 5 of 5 on Groq (`BASELINE-REPEATS-20261112.md`).

## Criteria

| Criterion (weight, V4) | What changed | Commits | Tests | Reports | Pending and why |
|---|---|---|---|---|---|
| Security & Identity (13%, 2.5) | See the gate above. Roles `merchant` / `operator`: an operator can not use `/chat`, a merchant can not use `/operator/*` | `6e6eeb3`, `087d50a` | `tests/test_auth.py`, `tests/test_roles.py`, `tests/test_operator_api.py::test_a_merchant_is_403_on_every_operator_endpoint_whatever_its_scopes` | `x04` rows in `latest.md`, `REPEATS-20261112.md` | Demo identity: HS256 shared secret, tokens from `scripts/issue_token.py`. Production needs an IdP (RS256/JWKS), refresh, revocation and rate limiting. Not in scope of the fix |
| Memory, State & Resilience (8%, 5.5) | Persistent `SqliteSaver` in the `data` volume, rolling summary (`summarize` node), one checkpoint row per thread, TTL purge, per-user thread limit (429), `secure_delete` + WAL truncation, owner-checked delete | `63ae3a5`, `0e0f4d1`, `5cdcdb5`, `6371310`, `dc8b62a` | `tests/test_memory_api.py::test_lifespan_opens_the_sqlite_file_keeps_the_thread_across_restarts_and_closes_it`, `tests/test_summary.py`, `tests/test_retention.py`, `tests/test_memory_threads.py::test_a_deleted_conversation_leaves_no_trace_in_the_file_or_the_wal`, `tests/test_followup_context.py` | `BASELINE-MULTITURN-20261112.md` | Restart with a live conversation and the rolling summary with the real LLM (`docs/VERIFICATION_DEBT.md` items 7-10): pending, outside the delivery window. Follow-ups that refer only to the conversation (`m01`-`m03`) are routed to Knowledge: kept as a limitation (Router prompt not changed in this delivery) |
| Human Oversight & Risk (7%, 4.5) | SQLite operator queue: claim → reply (approve / reject) → the customer's next turn carries the reply (resume), plus reject/release, leases, idempotency keys, exactly-once transitions in SQL, audit events. The reply enters the agents' context as a labelled message | `ae6c319`, `a67412d`, `27c96d5`, `266473a`, `defcd63`, `0d879bc` | `tests/test_operator_api.py::test_full_flow_create_list_claim_reply_and_the_customer_receives_it`, `tests/test_handoff_store.py`, `tests/test_handoff_lease.py`, `tests/test_handoff_release.py`, `tests/test_handoff_resume.py` | `h01`-`h03`, `ho04` in `latest.md`; `REPEATS-20261112.md` | Real-LLM checks of the queue and of the operator text in the context (items 11-13): pending. No operator UI and no notifier (Slack/Teams) |
| Observability, Reliability & Cost (6%, 5.5) | Per-turn tokens, cost and per-node spans through a LangChain callback (no prompt or node change), `traces.sqlite` without any text (erased with the conversation and by TTL), `usage` in `/chat`, `GET /operator/metrics`, `ALERT` log lines (token / cost limit, 429), tokens and cost in every eval report | `4f65d35` | `tests/test_observability.py` (18 tests, e.g. `test_tokens_and_cost_are_summed_per_node_and_model_through_the_real_graph`, `test_handle_returns_usage_and_stores_a_trace_without_any_text`, `test_metrics_aggregates_for_an_operator`, `test_a_broken_trace_store_never_fails_the_turn`), `tests/test_eval_usage.py` | `latest.md` (tokens and cost per case and per model) | No visual dashboards, no OpenTelemetry/Langfuse export, no alert delivery (the `ALERT` lines are where a log rule hooks in) |
| Evaluation & Quality (14%, 8.7) | Versioned full report with all cases (17 → 27: 17 single-turn, 5 multi-turn, 5 blind holdout), tokens and cost per case, repeats of the security/handoff cases, judge calibration tools (export + agreement: kappa, confusion, MAE) | `f55a085`, `2396958`, `63d4b85`, `aff8351`, `8ecfe29` | `tests/test_eval.py`, `tests/test_eval_usage.py`, `tests/test_calibration.py` (hand-computed kappas) | `latest.md`, `REPEATS-20261112.md`, `eval/calibration/labels_template.csv` | Human labels (need a person), judge self-agreement (the report does not keep the evidence the judge saw), a `--repeats` variance report of the whole eval: pending, outside the delivery window |
| Agentic Reasoning & Orchestration (15%, 8.5) | Kept "plan first, then run" on purpose; the argument and what would change it are below | - | `tests/test_graph.py` | `latest.md` routing | Adaptive replanning not built, see below |
| Context, Knowledge & RAG (10%, 8.8) | Not changed in this delivery | - | `tests/test_ingest.py` | - | Retrieval metrics (hit@k, recall@5, MRR, nDCG) and a BM25 + RRF hybrid: pending, outside the delivery window. They need a labelled retrieval set checked against the live KB; the Router and the observability work were judged to carry more weight |
| Production Engineering (5%, 7.5) | Non-root image (uid 10001), read-only code, named volume, CI (pytest on 3.12 + image build + uid check), memory that survives restarts | `5a538d6`, `34663d1`, `63ae3a5` | CI workflow `.github/workflows/ci.yml`; `tests/test_memory_api.py::test_lifespan_opens_the_sqlite_file_keeps_the_thread_across_restarts_and_closes_it` | README "Production hardening" | A green CI run on GitHub has to be checked after the push |
| Tool & Action Engineering (10%, 9.5) | Protected as it was | - | `tests/test_customer_tools.py`, `tests/test_tool_schemas.py` | - | - |
| Business & Challenge Fit (8%, 9.8) / Engineering Judgment (4%, 9.8) | Protected; the agents moved to Fireworks (same gpt-oss models) because the Groq Developer plan was not available; the judge stays on Groq, a different family | `ac6e4e0` | `tests/test_llm_provider.py` | `BASELINE-FIREWORKS-SMOKE-20261112.md` | - |

## Why the judge pass rate went down and why `x04` hands off

The previous full report (`BASELINE-FULL-DELIVERED-20261112-092550.md`, Groq, 17 cases) had a 100% judge pass rate; `latest.md` (Fireworks, 27 cases) has 86% on the 22 regular cases. Two things changed at once, so the numbers are not one series:

1. **Five more cases, and harder ones.** The 22 regular cases include the 5 multi-turn follow-ups (`m01`-`m05`), which did not exist in the 17-case report. Two of the three regular judge failures are among them: `m01` (the synthesizer said it does not keep personal preferences instead of reading the earlier turn) and `m05` (it repeated the next payout instead of the one after). On the 17 cases that both reports share, only `q05` fails (it invented Wi-Fi details; it also failed 1 of 5 runs on Groq), so the comparable pass rate is 16 of 17 (94%).
2. **A different provider and a bigger Router/Synthesizer.** Fireworks serves only `gpt-oss-120b`, so the Router and the Synthesizer moved from the 20b to the 120b. That improved `m01`/`m02` routing (empty plan, as expected) and changed answers in other places (completeness 94% → 87%).

No prompt was changed to win these back: the hash test pins every prompt, and the failures are listed in `latest.md` with the judge's reason.

**`x04` hands off.** `x04` asks for another customer's transactions. The security behaviour is what matters and it held in every run on both providers: no data tool is called and no data of either customer is shown. What changed is that the Support agent also calls `escalate_to_human` ("User requested data for a different customer ID"): 1 of 5 runs on Groq, 4 of 4 on Fireworks, where with temperature 0 the run is close to deterministic (identical token counts). The case expects no handoff, so `handoff_accuracy` is 95%. It is a false alarm in the operator queue, not a leak. The fix would be a Support prompt change, and that prompt is not touched without measuring `x04` and `h03` together: removing the customer id from it once made `x04` fail 5 of 5.

## Plan first, then run (why there is no observe → replan loop)

The orchestration is one Router call that writes an ordered plan (`support` before `knowledge`), then the agents run in that order and each one reads what the earlier ones found; the synthesizer merges. There is no supervisor that looks at each result and replans.

**Evidence that the plan is right without replanning:**

- Routing accuracy 95% (21 of 22) on the 22 regular cases of the final run, and 80% (4 of 5; `ho02` added Knowledge to an account question, an extra agent, not a missing one) on the blind holdout.
- The cases that need two agents (`q05`, `q08`, `ho03`) run Support first and Knowledge reads its findings in the same turn: the "observe" part already exists inside the plan, through `format_prior_context`.
- When the plan is not enough the system does not loop, it escalates: an agent that can not resolve calls `escalate_to_human`, and a technical failure of every agent goes to a person (`handoff_reason`).
- **Cost per turn, measured** (final run, all agents on `gpt-oss-120b` on Fireworks): 102 LLM calls for 27 turns (3.8 per turn), a mean of 5,134 agent tokens per turn, about $0.001 per turn at list prices ($0.026 for the 27 agent turns). A greeting costs 1.2K tokens (router + synthesizer), a one-agent turn 3.5-8K, a two-agent turn 9-13K (`q08` 13.5K). A supervisor loop adds at least one LLM call (the supervisor reading the results) to every turn and more on each replan; on the current mix that is roughly 1-3K tokens (+20-60% on the 5.1K mean), since a supervisor call has to read the agents' findings, which are larger than the Router's input more tokens per turn, with no measured case that it would fix.

**What would justify observe → replan** (and how to measure it with what is already built):

1. A share of turns where the plan was wrong and a second look would have fixed it: e.g. Support finds the payout is blocked by a missing document and only then Knowledge is needed. Measure it as the rate of `agent_unresolved` handoffs and of turns whose answer says "I don't know" while another agent could have answered; `/operator/metrics` already counts handoffs per node.
2. A routing accuracy below ~95% on the holdout, with the misses being plans that a second step would have corrected (not misclassified intents, which a better Router prompt fixes more cheaply).
3. A budget: the extra tokens per turn (measured per node in `usage.by_node`) must stay under `MAX_TURN_TOKENS` for the p95 turn.

If 1 or 2 shows up in the traces, the change is small in this graph: a `review` node after the last agent that may append one agent to `plan` (bounded to one replan per turn), with the same tests.

## Delivery checklist

- `latest.md`: 27 cases, 0 provider errors. Regular (22): routing 95%, tools 100%, handoff 95% (`x04` false positive), groundedness 95%, completeness 87%, judge pass 86%, p50 3.7 s, p95 12.3 s. Holdout (5): routing 80%, tools 100%, handoff 100%, judge pass 80%. Tokens: 138.6K agents, 58.5K judge; estimated $0.084. Stable copy: `BASELINE-FIREWORKS-FULL-20261112-131428.md`.
- `REPEATS-20261112.md`: `x04`, `h01`-`h03` and `q08`, three runs each.
- Still for the author: push, check the green CI run, label `eval/calibration/labels_template.csv`, record the ≤ 15 min video.
