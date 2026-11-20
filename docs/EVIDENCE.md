# Where to verify each claim

A map from a claim to the code that does it and to the test or report that backs it. Line numbers are those of this checkout and move when a file is edited; the file and the test name are the stable part.
A test named here is in the file next to it. Reports are in `eval/reports/` and are versioned. **No judge rate is typed here**: for those, read the generated block in the [README](../README.md#results). The answer to each point of the external review is in [REVIEW_RESPONSE.md](REVIEW_RESPONSE.md).

To run the tests without keys or network: `pytest -m "not eval"`.

## What the CI checks and what is manual

Three kinds of evidence, and a claim below is one of them. **Automatic**: a test or a generated report that anybody can re-run from a checkout. **Captured**: somebody ran it against real infrastructure and the output is saved in `docs/evidence/` with its date and command (a reader can read it, not re-run it without the infrastructure). **Manual**: somebody did it by hand, once; it may have left no artifact, and a reader cannot re-run it from this checkout without the infrastructure it names. **Reported by the owner**: a result that lives outside the repository (the last section of this page).

| Claim | Kind | What there is in this checkout |
|---|---|---|
| The unit and integration tests (no keys, no network) | Automatic | `pytest -m "not eval"`; the workflow's job `test` runs them (what the runs on GitHub said is in the last section) |
| The quality gates (ruff, mypy, bandit, pip-audit, promtool, trivy) are blocking in the workflow | Automatic (that the gates exist) | `tests/test_ci_gates.py`; whether they passed on GitHub is in the last section |
| The alert rules fire and stay quiet when they should | Automatic | `infra/observability/alerts_test.yml`, run by `promtool test rules` in the job `alerts` and in `tests/test_observability_infra.py` |
| The auth code against a REAL Keycloak | **Captured** 2026-11-18 (`RUN_KEYCLOAK_TESTS=1`; the CI has no Keycloak, so the 4 tests are skipped there) | `tests/test_keycloak_live.py`; the output of the run, 4 passed: `docs/evidence/keycloak_live_2026-11-18.txt` |
| A turn arrives in a REAL Jaeger as one trace with child spans and no customer data | **Captured** 2026-11-18 (two traces: a synthetic greeting, 3 spans; and a read-only turn with a lookup tool and the knowledge base, 11 spans) | `docs/evidence/jaeger_trace_2026-11-18.json` and `docs/evidence/jaeger_trace_tools_2026-11-18.json` (redacted: see below); the code that exports it is `app/telemetry.py` and `tests/test_telemetry.py` (an in-memory exporter, automatic). The earlier by-hand check (4 spans) is not recorded and is not what this file shows |
| Prometheus scrapes the API's token-protected `/metrics` (`up` = 1) | **Captured** 2026-11-18 | `docs/evidence/prometheus_up_2026-11-18.txt` (the `up` query, `getnet_turns_total` after the synthetic turn, and the target's health) |
| The alert rules are valid and every one has its unit tests | **Captured** 2026-11-18 (and automatic in the CI job `alerts`) | `docs/evidence/promtool_2026-11-18.txt` (`promtool check rules`: 9 rules, exit 0; `promtool test rules`: SUCCESS, exit 0) |
| The Grafana dashboard loads with its provisioned data source | **Manual** (nothing was exported: it stays manual) | `infra/observability/grafana/dashboards/getnet.json` and `tests/test_observability_infra.py` (the files are well formed, automatic); no artifact of Grafana itself, and it was not looked at under live traffic |
| A ticket approval with the REAL model (a turn that asks for a ticket becomes `pending_approval`, approving it opens the ticket, the next turn says so) | **Manual** (one run by hand) | `tests/test_approvals.py` covers the mechanism with scripted models (automatic); no artifact of the real-model run |
| Provider failover | **Test doubles only** (automatic, but nobody caused a real outage) | `tests/test_failover.py` simulates a 503 and a 429; the repository holds no artifact of a real provider outage |
| Every judge-based rate | Generated | the blocks of the [README](../README.md#results) and of [FINAL_REPORT.md](FINAL_REPORT.md); none is typed on this page |

## Security

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| Tokens are RS256 only, checked against the identity provider's JWKS; no shared secret in the app | `app/auth.py:51` (the algorithm list, fixed in code), `app/auth.py:185` (header check), `app/auth.py:207` (decode) | `tests/test_auth_rs256.py:61` (`test_a_valid_rs256_token_is_accepted`) | `infra/keycloak/realm-merchants.json`, `infra/keycloak/realm-operators.json`; `tests/test_keycloak_live.py` runs the same checks against a real Keycloak when `RUN_KEYCLOAK_TESTS=1` (skipped otherwise) |
| `kid` is required; a token cannot bring its own key (`jku`, `jwk`, `x5u`, `x5c` are refused) | `app/auth.py:53` | `tests/test_auth_rs256.py:66` and `:75` | |
| Merchants and operators have separate issuers, audiences and keys; the role comes from the issuer | `app/auth.py:207` (issuer and audience per realm), `app/config.py:22-27` (the six settings) | `tests/test_auth_rs256.py:86` and `:96` | `tests/test_roles.py`, `tests/test_operator_api.py` |
| The key cache refreshes on an unknown `kid` at most once per interval and keeps serving if the provider is down | `app/jwks.py:39` (`JwksCache`), `app/jwks.py:72` | `tests/test_auth_rs256.py:117`, `:136`, `:149`, `:160` | |
| A token cannot live longer than the configured maximum | `app/auth.py:216` | `tests/test_auth_rs256.py:190` | |
| Token revocation by `jti`, kept in SQLite so it survives a restart | `app/revocation.py:20` (`Denylist`), `app/auth.py:152` and `:223`, `app/main.py:458` (the operator endpoint) | `tests/test_auth_rs256.py:199`, `:300`, `:324` | |
| Rate limit per authenticated caller (`sub`), with its own limit for operators | `app/ratelimit.py:21` (`RateLimiter`), `app/auth.py:143` | `tests/test_auth_rs256.py:226`, `:236`, `:251` | The limiter lives in the process: see [SCALING.md](SCALING.md) |
| Refusals are logged with a fixed reason and never with the token, the claims or the `sub` | `app/auth.py:49` (logger `auth.audit`) | `tests/test_auth_rs256.py:263`, `:275`, `:287` | |
| The app does not start with an incomplete auth setup | `app/auth.py:92-108` | `tests/test_auth.py:233`, `:254` | |

## The customer's identity is injected, never chosen by the model

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| The customer is the token's `sub`; a body `user_id` that differs is refused | `app/main.py:250-261` | `tests/test_api.py`, `tests/test_auth.py` | |
| Support tools are created per request, tied to one customer, and take no user argument | `app/tools/customer_tools.py:43` (`make_support_tools`), `app/agents/support.py:23` | `tests/test_tool_schemas.py:67` (no tool exposes a parameter that selects a user, found automatically), `tests/test_customer_tools.py` | |
| A hostile call that names another customer is refused; injected text does not change whose data is read | `app/tools/customer_tools.py:24` (arguments not declared are an error) | `tests/test_injection.py:37`, `:51`, `:63` | Eval cases `x04_cross_customer_attempt` (`eval/dataset.jsonl:14`), `inj01_override_and_prompt_leak`, `inj02_claims_to_be_an_operator` |

## Human in the loop

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| A ticket the agent asks for waits for a person (LangGraph `interrupt()`, one thread per approval, apart from the conversation's graph) | `app/approvals.py:36` (the import), `app/approvals.py:354` (the `interrupt()` call), `app/graph.py:63-71` | `tests/test_approvals.py:126` (two decisions, one winner), `:160` (expiry), `:208` | |
| Silence never approves: an expired request opens no ticket and goes to the human queue | `app/approvals.py` (reason `approval_expired`) | `tests/test_approvals.py:160` | |
| An escalation in the same turn beats a ticket proposal | `app/handoff.py:67-82` | `tests/test_escalate_vs_ticket.py:79`, `:90` | |
| Handoff queue with claim, reply, lease and an audit trail | `app/handoff_store.py`, `app/main.py` (the `/operator/*` endpoints) | `tests/test_handoff_store.py`, `tests/test_handoff_lease.py`, `tests/test_operator_api.py`, `tests/test_handoff_privacy.py` | |
| Signed webhook to the operators, with no customer text, that never slows a turn | `app/webhook.py:34` (`sign`), `app/webhook.py:38` (`verify`), `app/webhook.py:59` (`WebhookNotifier`) | `tests/test_webhook.py:66`, `:87`, `:107`, `:118` | |
| A handoff nobody claims within the SLA is escalated once | `app/handoff_store.py:457` (`escalate_overdue`), `app/main.py:72` (`sweep_once`) | `tests/test_handoff_sla.py:30`, `:41`, `:53` | |

## Observe, replan and `add_support`

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| A node after the synthesizer audits the answer and can replan; with `REPLAN_MAX=0` (the default) it does nothing | `app/agents/observe.py:121` (`make_observe_node`), `app/graph.py:53-61` (the wiring), `app/config.py:142` | `tests/test_observe.py:83`, `:89`, `:323`, `:647` | `.env.example` (`REPLAN_MAX=0`) |
| Replan 1: one knowledge-base search with the unsupported claims grouped, then only what is still unsupported is deleted | `app/agents/observe.py:144-159` | `tests/test_observe.py:143`, `:162`, `:169` | `eval/reports/REPLAN_AB-20261115-151730.md` (the mechanical measurement) |
| Replan 2: `add_support` (the support agent runs with the authenticated user id, the answers are merged and audited again), or a ticket offer, or an escalation | `app/agents/observe.py:91` (`can_add_support`), `app/agents/observe.py:164` and `:199` (`_add_support`) | `tests/test_observe.py:441`, `:456`, `:475`, `:490`, `:523` | [REPLAN_DEMO.md](REPLAN_DEMO.md) |
| The three judged repetitions of the step on, compared with the five base repetitions by a script, without any interpretation | `eval/compare_runs.py` | `tests/test_compare_runs.py` (synthetic files, the headline line, the same bytes twice, and the regeneration of the committed report) | `eval/reports/REPLAN-AB-20261116-234030.md` (inputs: `eval/reports/20261116-234030.judged.json`, `eval/reports/20261116-234325.judged.json`, `eval/reports/20261116-234617.judged.json` and the five judged files of the final run, `eval/reports/20261115-202155.judged.json` and four more) |
| With the step off, the new code changes nothing | `app/agents/observe.py:129` (the early return) | `tests/test_observe.py:647`, `:661` | `eval/reports/SMOKE-add-support.md` (identity: no replans in any row, agent sets already seen) |
| The step on: how often it fired, and what it cost | | `tests/test_replan_stats.py` | `eval/reports/20261116-234030.json`, `eval/reports/20261116-234325.json`, `eval/reports/20261116-234617.json`, `eval/reports/ADDSUPPORT-CAPTURE-20261116-233738.jsonl`, `eval/reports/REPEATS-20261116-234617.md`, `eval/reports/REPLAN_ON-20261115.md`. The three repetitions of the `add_support` branch were scored by the judge (see the comparison report below); the earlier branch was not |
| The step on against the step off, on 12 cases written to make it act, preregistered, paired and scored by the unchanged judge | `eval/compare_runs.py` (`--paired`), `eval/probe_preflight.py`, `eval/run_eval.py` (`--dataset`) | `tests/test_compare_runs_paired.py`, `tests/test_probe_preflight.py`, `tests/test_eval_dataset_env.py`, `tests/test_replan_probe_cases.py`, `tests/test_probe_docs_lines.py` | `docs/REPLAN_PROBE_PREREG.md` (the rule, two amendments and the record of the firing run), `eval/probe/replan_probe.jsonl`, `eval/reports/REPLAN-PROBE-20261118-133249.md` (generated), `eval/reports/20261118-133249.judged.json`, `eval/reports/20261118-133453.judged.json`, `eval/reports/20261118-133646.judged.json`, `eval/reports/20261118-133846.judged.json`, [REPLAN_DEMO.md](REPLAN_DEMO.md#the-replan-probe-2026-11-18-the-step-on-against-the-step-off-on-cases-written-to-make-it-act). The firing run that came first (no judge, diagnostic): `eval/reports/20261118-131849.json`, `eval/reports/20261118-132136.json` |
| A human check, after the fact, of the one control row where the retry changed the verdict: the claim in dispute is not in the retrieved passages | | | `docs/evidence/c03_post_hoc_check_2026-11-18.md` (built from the raw run files, with no judge data; the result is at its end). One row, made after the preregistered result was fixed; it does not change it |

**The probe's headline.** Copied by a script from the generated report `eval/reports/REPLAN-PROBE-20261118-133249.md`; the rule is the one of the preregistration, and the replan stays off by default whatever it says:

<!-- probe:ev_outcome:start -->
> **Outcome of the preregistered rule: null.** Net +0, inside +/-2.
<!-- probe:ev_outcome:end -->

**Findings recorded with it:** the two replan-2 safety nets did not act in the ON rows of the probe (apart from one ticket offer), the cases were not revised to make them act (amendment 1 of the preregistration), and phase 5 of the plan of the day was skipped by decision of the owner.

## The verifier

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| The model lists each claim with a quote and the **code** checks the quote against the evidence; one rewrite deletes what has no support | `app/agents/verifier.py:94` (`quote_in`), `app/agents/verifier.py:107`, `app/agents/verifier.py:226` (`make_verify_node`) | `tests/test_verify.py:55`, `:69`, `:82`, `:88` | |
| An answer that repeats 10 or more consecutive words of a system prompt is replaced by a refusal | `app/guard.py:53` (`leaked_run`) | `tests/test_guard.py:21`, `:27`, `:34` | |
| A segment-by-segment verifier was built, measured and **not adopted** | `app/segments.py` | `tests/test_verify_segments.py`, `tests/test_verifier_ab.py` | `eval/verifier_ab.py`; the comparison table is in the README ("Verifier alternatives") |

## Memory

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| Conversations are checkpointed in SQLite, per customer, with a rolling summary, retention and a limit per customer | `app/memory.py:48` (`Memory`), `app/memory.py:63` (`SqliteSaver`), `app/memory.py:119` (`prune_thread`), `app/main.py:190` | `tests/test_memory.py:51` (survives rebuilding the graph on the same file), `tests/test_memory_threads.py`, `tests/test_summary.py`, `tests/test_retention.py` | |
| The server generates the conversation id; one that is not the caller's is the same 404 as a missing one | `app/main.py:250-261` | `tests/test_conversation_ids.py`, `tests/test_conversations_api.py` | |
| The eval's multi-turn cases survive a restart of the checkpointer | `eval/persistent.py` | `tests/test_eval_persistent.py:38`, `:47` | The memory row of the generated block in the [README](../README.md#results) |

## Failover

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| With `FALLBACK_PROVIDER` set, an outage (5xx, 429, no connection, timeout) is answered by the same model at the other provider; a 400 is not | `app/llm.py:45` (`outage_errors`), `app/llm.py:58` (`fallback_model_name`), `app/llm.py:68` (`get_llm`), `app/llm.py:87` | `tests/test_failover.py:69`, `:94`, `:103` | `app/config.py:44` (the default is empty), `docker-compose.yml:59` (the API) and `:84` (the eval, which has it off) |
| The eval turns failover off, so a report is measured on one provider | `eval/run_eval.py:550` | `tests/test_run_config.py` | |

## Observability

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| `/metrics` exists only with a token, and carries no text or customer id | `app/telemetry.py:30` (`Metrics`), `app/main.py:466-468` | `tests/test_telemetry.py:82`, `:201`, `:209`, `:214`, `:222` | |
| One OTLP trace per turn, a child span per model or tool call, with no text | `app/telemetry.py:86` (`Tracing`), `app/config.py:146` | `tests/test_telemetry.py:102`, `:114`, `:128`, `:142` | `docker-compose.yml:93` (Jaeger), the `observability` profile |
| Prometheus, Grafana and the dashboard are provisioned and bound to localhost | `docker-compose.yml:102` (Prometheus) and `:116` (Grafana), `infra/observability/prometheus.yml`, `infra/observability/grafana/dashboards/getnet.json` | `tests/test_observability_infra.py:43`, `:54`, `:70`, `:78` | The Jaeger check was **captured** on 2026-11-18 (`docs/evidence/jaeger_trace_2026-11-18.json`) and so was Prometheus' scrape (`docs/evidence/prometheus_up_2026-11-18.txt`); the Grafana check is still **manual** (see [the section above](#what-the-ci-checks-and-what-is-manual)) and is not in the repository |
| 9 alert rules, each with a unit test that fires it and one that keeps it quiet | `infra/observability/alerts.yml` | `tests/test_observability_infra.py:36`, `:59`, `infra/observability/alerts_test.yml` (run by `promtool`) | `.github/workflows/ci.yml:80` (job `alerts`) |

## Retrieval

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| The knowledge base can be searched in three modes: `dense` (the original), `hybrid` (dense plus BM25, fused with RRF) and `hybrid_rerank` (hybrid, then a Voyage reranker reorders the best candidates); if BM25 or the reranker fails the search falls back to the stage before | `app/rag/hybrid.py:103` (`rrf`), `app/rag/hybrid.py:185` (`hybrid_search`), `app/tools/knowledge_tools.py:44` (the mode is chosen here), `app/config.py:61-64` | `tests/test_hybrid.py:43`, `:54`, `:58` | |
| The three modes were measured on the same 21 questions, with the parameters chosen on one half of the regular questions and the other half and the holdout only measured | `eval/run_retrieval.py:86` (`average`) | `tests/test_retrieval_metrics.py` | `eval/reports/RETRIEVAL-20261115-182559.md` (the tables per set and per mode), `eval/reports/RETRIEVAL-20261115-182559.json` |
| **The default is `dense`, and it stays so.** The README's reasons: fusion and the reranker put the right page higher, but they lose coverage in the top 5; on the holdout the plain hybrid is worse than dense; the improvement on the half where the parameters were chosen does not carry over to the questions it was not chosen on; and the knowledge agent reads all 5 chunks, so coverage matters more to it than the order inside the 5 | `app/config.py:61` (`rag_mode` defaults to `dense`) | | The README, "Results", the paragraph "What it says, honestly" |
| The retrieval table typed in the README says what the saved report says (the four sets and the three modes) | | `tests/test_retrieval_table.py` (it fails if a value of the README differs from the report, and it is skipped if the report is not in the checkout) | The README, "Results", the table of the paragraph "Retrieval" |

## CI

| Claim | Where in the code | Test | Other evidence |
|---|---|---|---|
| Four jobs: `quality` (ruff, mypy, bandit, pip-audit), `test`, `alerts` (promtool), `docker` (build, non-root check, compose, trivy) | `.github/workflows/ci.yml:19`, `:56`, `:80`, `:91` | `tests/test_ci_gates.py:74`, `:80`, `:84` | The result of the runs on GitHub is the project's own account (final report, section 7, item 13) and cannot be checked from a checkout |
| The vulnerabilities pip-audit ignores are only the ones documented | `.github/workflows/ci.yml:48` | `tests/test_ci_gates.py` (the ignored ids equal the documented ones) | The README, "Accepted vulnerabilities" |
| Pinned dependencies for the image | `constraints.txt`, `scripts/lock_deps.py`, `Dockerfile` | `tests/test_constraints.py` | |

## Calibration of the judge (V1, V2, V3)

| Claim | Where | Test | Other evidence |
|---|---|---|---|
| Three human checks of the judge, each with its own sample and its own version of the judge | `eval/calibration/CALIBRATION_V1.md`, `eval/calibration/CALIBRATION_V2.md`, `eval/calibration/CALIBRATION_V3.md` | `tests/test_calibration.py` | `eval/calibration/agreement.py` computes agreement, Cohen's kappa and the mean absolute error |
| V3 is the only independent one: 15 rows fixed before any judge note existed | `eval/calibration/sample.py`, `eval/calibration/sample_v3.json`, `eval/calibration/labels_v3_blind.csv` | `tests/test_calibration_sample.py` | [FINAL_REPORT.md](FINAL_REPORT.md#3-does-the-judge-agree-with-a-human-v3) |
| V2 re-scored with the current judge on 19 rows drawn with a seed fixed beforehand; it is not independent | `eval/calibration/v2_rescore.py`, `eval/calibration/V2_RESCORE_REPORT.md` | `tests/test_v2_rescore.py` | `eval/reports/V2RESCORE19-20261114-192208.judged.json` |

## Repetitions

| Claim | Where | Test | Other evidence |
|---|---|---|---|
| The final run is five full repetitions of the 40 cases on one commit with a clean tree, scored by the judge afterwards | `eval/run_eval.py`, `eval/rejudge.py`, `eval/judge_budget.py` | `tests/test_rejudge.py`, `tests/test_repeats.py`, `tests/test_provenance.py` | `eval/reports/REPEATS-20261115-204526.md` (the spread over the five), `eval/reports/20261115-202155.judged.json` (and four more), [FINAL_REPORT.md](FINAL_REPORT.md) |
| The results in the README and the final report are generated from the saved runs, with the number of rows each rate is over | `eval/results_block.py` | `tests/test_results_block.py`, `tests/test_final_report.py` | |
| The later code behaves like the final run on the checks that need no judge | `eval/same_behavior.py` | `tests/test_same_behavior.py` | `eval/reports/SMOKE-0835e91.md`, `eval/reports/SMOKE-add-support.md` |

## Captured on 2026-11-18

Run on the experimental branch `exp/b-plus`; `app/` is identical to `98ea315` (the work of that day touched documents, tests and `eval/` only). The stack is the one of `docker-compose.yml` (Keycloak, the API, Jaeger and Prometheus of the `observability` profile) plus a scratch override that turns on what `.env` does not have (`OTLP_ENDPOINT`, `METRICS_TOKEN`) and tags the image apart (`docs/evidence/compose_override_2026-11-18.yml`); `.env` was not edited. The passwords and the metrics token were generated for the run and are in no file of the repository. The API image was built for this run (`getnet-agents:exp-b-plus-98ea315`, label `org.opencontainers.image.revision=98ea315`).

| File | Date | Exact command | What it shows | What it does not show |
|---|---|---|---|---|
| `docs/evidence/keycloak_live_2026-11-18.txt` | 2026-11-18 12:11 | `RUN_KEYCLOAK_TESTS=1 DEMO_USER_PASSWORD=<generated> python -m pytest tests/test_keycloak_live.py -rs -v` (after `docker compose -f docker-compose.yml -f docs/evidence/compose_override_2026-11-18.yml --profile observability up -d keycloak api jaeger prometheus`) | the 4 tests against a real Keycloak 26.2: merchant token accepted, operator token only an operator, tampered token refused, keys fetched once | that the CI runs them (it does not: it has no Keycloak) |
| `docs/evidence/jaeger_trace_2026-11-18.json` | 2026-11-18 | `curl -s -X POST http://127.0.0.1:8000/chat -H "Authorization: Bearer <token>" -H "Content-Type: application/json" -d '{"message": "Hello, how are you today?"}'`, then `curl -s "http://127.0.0.1:16686/api/traces?service=getnet-agents&limit=5"` and `python docs/evidence/redact_jaeger_trace.py raw.json out.json` | one trace as Jaeger returned it: `chat.turn` (3,764 ms) with two children, `llm router` and `llm synthesizer`, parented, with tokens and cost per span and no text | a turn with tools, the knowledge base or `verify` (a greeting has none of them: the next row has them), or a handoff; the raw export was not kept |
| `docs/evidence/jaeger_trace_tools_2026-11-18.json` | 2026-11-18 | `curl -s -X POST http://127.0.0.1:8000/chat -H "Authorization: Bearer <token>" -H "Content-Type: application/json" -d '{"message": "When will my next payout arrive, and how does contactless payment work on the Getnet Smart terminal?"}'` as the demo customer `cliente1988`, then `curl -s "http://127.0.0.1:16686/api/traces?service=getnet-agents&limit=5" > raw.json` and `python docs/evidence/redact_jaeger_trace.py raw.json out.json "<the request>"` (run on that raw export, which was then deleted) | a read-only turn that used the support and the knowledge agents: 11 spans, listed below | a write, an approval, a handoff or a replan (none happened; `REPLAN_MAX=0`) |
| `docs/evidence/prometheus_up_2026-11-18.txt` | 2026-11-18 | `curl -s 'http://127.0.0.1:9090/api/v1/query?query=up'`, the same with `getnet_turns_total`, and `curl -s http://127.0.0.1:9090/api/v1/targets` | Prometheus scraped the API with the bearer file and the target is `up` | the dashboard, or any alert firing |
| `docs/evidence/promtool_2026-11-18.txt` | 2026-11-18 | `docker run --rm --entrypoint promtool -v "$PWD/infra/observability:/w" prom/prometheus:v3.0.1 check rules /w/alerts.yml` and the same with `test rules /w/alerts_test.yml` | the two commands the CI job `alerts` runs, with their output | nothing beyond what the CI already runs |

**Why a greeting, and the attempt before it.** A trace exists only for a turn, and every turn calls a model (the router first), so no request that skips the model makes one: `GET /health`, `GET /conversations` with a valid token and `GET /metrics` were tried first and Jaeger listed no service of the API afterwards. Then ONE `POST /chat` with a synthetic greeting and no customer data was made: 1,420 tokens, about 0.0003 USD, in the usage field of its response.

**The second trace (a turn with a tool and the knowledge base).** The question was a payout date plus a product question, read-only (no ticket, no approval, no handoff: the response had no approval, no open handoff and no replan). The turn took 14,604 ms, 13,828 tokens (11,912 in, 1,916 out) and cost 0.0029364 USD (the root span's own tags, which equal the usage field of the response). The plan was `support,knowledge`. The spans that appear, in start order: the root `chat.turn`; `llm router`; `llm support` (a first call, 486 ms); **`tool support` = `get_payout_schedule`** (the tool span exists; its recorded duration is 0 µs, a local lookup in the demo data); `llm support` (a second call, after the tool); three `llm knowledge` calls with **`tool knowledge` = `search_getnet_kb`** (1,460 ms) between the second and the third; `llm synthesizer`; and `llm verify` (the verifier's model call, 2,755 ms; it is the last span, so `verify` does appear). In total 1 root, 8 `llm` spans and 2 `tool` spans, all children of the root. **What the trace does not show:** the retrieval is one tool span, with no embedding or rerank span inside it; there is no span for the code that checks the quotes (only for the model call); there is no `observe` span (the step is off) and none for `summarize` or `request_approval`. The support agent ran before the knowledge agent (their spans do not overlap). One oddity, as the file has it: the last child ends 29,611 µs after the end of its root (14,633,611 µs against 14,604,000 µs); the file does not say why, and nothing here explains it.

**The redaction of the traces**, and the check on each file (`grep -c`, case-insensitive, on `docs/evidence/jaeger_trace_2026-11-18.json` and on `docs/evidence/jaeger_trace_tools_2026-11-18.json`): absolute timestamps were replaced by offsets from the start of the turn and the process tag `service.instance.id` was replaced by `<redacted>`. Matches: `Bearer` 0, `eyJ` 0, `Authorization` 0, `api_key`/`apikey`/`api-key` 0, e-mail addresses 0, numbers of 10 digits or more 0, a customer id (`cliente<N>`) 0, the text of the message 0, the conversation id 0, the generated passwords and token 0, key prefixes (`gsk_`, `fw_`, `sk-`, `pa-`, `tvly-`) 0, in both files; the second file also: the text of the question 0, the text of the answer (the amount and `R$`) 0. The first grep of the second file found 3 numbers of 10 digits or more: they were floating-point noise in `getnet.cost_usd` (for example 0.0029363999999999996), not data. The script was then changed to round float tags to 8 decimals (declared in the file's own `_capture` note), the redaction was run again on the same raw export and every count was 0 before the file was saved.

## Captured on 2026-11-19

| File | Date | Exact command | What it shows | What it does not show |
|---|---|---|---|---|
| `docs/evidence/openrouter_models_2026-11-19.json` | 2026-11-19 (02:59 UTC of the 20th, as the file says) | `python -m scripts.openrouter_models` (public endpoints, no key) | what OpenRouter listed for the seven models of plan 9.6: price, parameters, reasoning options, and for `openai/gpt-oss-120b` and `qwen/qwen3.8-27b` every host with its quantization, price and uptime. The two hosts the code pinned when it was captured (`deepinfra/bf16` and `alibaba`; the agents' host was changed later by the measurement below) and the prices in `app/observability.py` (`DEFAULT_PRICES`, the `<model>@<host>` entries) come from it | any call to any model; it is a snapshot, and prices and uptime move (a model's listed price is the cheapest host's, not the pinned host's: for the judge model 0.06/2.2 listed against 0.425/2.55 at `alibaba`, USD per million tokens) |
| `docs/evidence/openrouter_smoke_2026-11-19.json` | 2026-11-19 (03:22 UTC of the 20th, as the file says), at commit `3f85f00`, tree clean | `python -m scripts.openrouter_smoke` (3 real calls, US$0.0035 in all; needs `OPENROUTER_API_KEY`) | which host answered each call (`DeepInfra` for the agent, `Alibaba` for the judge, as pinned), that a tool call works on the pinned host, that the judge with reasoning off answers JSON the eval's parser reads, and that its real cost (`usage.cost`) equals the pinned host's price (0.425/2.55) and not the listed 0.06/2.2 or the old Groq table; and what the default reasoning effort does (880 reasoning tokens, 997 completion tokens, 4.7 times the cost, same JSON, no truncation) | one call per case, so no variation; the agreement of this judge with the Groq judge (block C2); latency. The file keeps no key, header or message text. A first run of the same calls from an uncommitted copy gave 954 reasoning tokens, 1070 completion tokens and US$0.003024 for the default effort: it is quoted in the docstring of `get_judge_llm`, and not kept as a file |
| `eval/reports/HOST-LATENCY-20261119-214824.md` (with its JSON next to it and the 16 `q01` runs it names) | 2026-11-19 (03:58 UTC of the 20th, as the file says), at commit `98f869f`, tree clean | `python -m eval.host_latency --prior-spend 0.000974` (real calls; needs `OPENROUTER_API_KEY`) | which host of `openai/gpt-oss-120b` the preregistered rule picks (`docs/PREREG_PLAN96.md`, amendment 1): `groq`; each candidate's 10 calls (router and knowledge, real prompts, one attempt, no client retries), its `q01` twice, which tools failed and with what error, and that the previously pinned `deepinfra/bf16` has a p50 13 times the winner's with no retry involved | the hosts' latency tomorrow (it is a snapshot of services whose load moves); quality of the answers per host (only routing, tools and cost are compared); the p50 of a host whose calls error (the errors come back fast, so their p50 is not a latency). The measurement before it crashed before saving anything: its US$0.000974 is counted in the cost and stated in the report |

Note on the smoke row above (`docs/evidence/openrouter_smoke_2026-11-19.json`): it was captured while `deepinfra/bf16` was the pinned host, so the host it shows answering the agent call is DeepInfra. After the measurement the pin is `groq`, and running `python -m scripts.openrouter_smoke` today reaches Groq for that call.

## Reported by the owner (not verifiable from the checkout)

These are results of runs on GitHub. The repository cannot show them: a checkout has no access to the run, and the tests only check that the workflow keeps its gates (`tests/test_ci_gates.py`). The link was pasted by the owner; it is the owner's account of that run, and **this page claims nothing about it beyond the link**.

| What | Commit | Link |
|---|---|---|
| CI run of the previous delivery (run #4) | `98ea315` | https://github.com/presichi910/Challenge_GetNet/actions/runs/44189949082 |

The CI run of the delivered commit is the latest run on `main` in the Actions tab.

The earlier account of the first run on GitHub (it failed in two jobs, was fixed, and the four jobs passed in the pull request of the branch `ci-fix`) is in [FINAL_REPORT.md](FINAL_REPORT.md), section 7, item 13, and says nothing about `main`.
