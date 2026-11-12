# Evaluation report

_Generated 20261112-131428_

_Models: agent provider = `fireworks`, knowledge = `accounts/fireworks/models/gpt-oss-120b`, router / synthesizer = `accounts/fireworks/models/gpt-oss-120b`, support = `accounts/fireworks/models/gpt-oss-120b`, judge = `qwen/qwen3.8-27b (groq)`_

## Summary

_Judge scores are shown as a percentage of the maximum (5)._

_The summary covers the 22 regular cases; the 5 holdout cases are summarized apart, below. `provider_errors` counts both._

- **provider_errors**: 0
- **routing_accuracy**: 95%
- **tool_use_accuracy**: 100%
- **degraded_rate**: 0%
- **latency_ms_p50**: 3663
- **latency_ms_p95**: 12333
- **handoff_accuracy**: 95%
- **handoff_rate**: 23%
- **avg_relevance**: 94%
- **avg_groundedness**: 95%
- **avg_completeness**: 87%
- **avg_tone**: 98%
- **judge_pass_rate**: 86%
- **agent_tokens**: 138629
- **judge_tokens**: 58473
- **agent_tokens_per_case_mean**: 5134
- **agent_cost_usd**: $0.0258
- **judge_cost_usd**: $0.0581
- **total_cost_usd**: $0.0839

## Holdout (blind cases, never used to tune prompts)

- **cases**: 5
- **provider_errors**: 0
- **routing_accuracy**: 80%
- **tool_use_accuracy**: 100%
- **degraded_rate**: 0%
- **latency_ms_p50**: 3671
- **latency_ms_p95**: 13149
- **handoff_accuracy**: 100%
- **handoff_rate**: 20%
- **avg_relevance**: 100%
- **avg_groundedness**: 88%
- **avg_completeness**: 92%
- **avg_tone**: 100%
- **judge_pass_rate**: 80%
- **agent_tokens**: 32981
- **judge_tokens**: 10328
- **agent_tokens_per_case_mean**: 6596
- **agent_cost_usd**: $0.0059
- **judge_cost_usd**: $0.0096
- **total_cost_usd**: $0.0155

## Tokens and estimated cost per model

_Prices: app/observability.py (USD per 1M tokens, list prices of 2026-11-12); a model without a price shows `n/a`, not 0._

| role | model | calls | input | output | cost USD |
|---|---|---|---|---|---|
| agents | `accounts/fireworks/models/gpt-oss-120b` | 102 | 127595 | 11034 | $0.0258 |
| judge | `qwen/qwen3.8-27b` | 34 | 54930 | 3543 | $0.0581 |

## Cases

_tokens / cost: the agents of the case (the judge is apart, above)._

| id | route | tools | handoff | rel | grounded | compl | tone | verdict | ms | tokens | cost USD |
|---|---|---|---|---|---|---|---|---|---|---|---|
| q01_compare_plans | OK ['knowledge'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 8124 | 4093 | $0.0007 |
| q02_weather | OK ['knowledge'] | OK | OK  | 100% | 80% | 80% | 100% | pass | 6773 | 4771 | $0.0008 |
| q03_payout_timing | OK ['support'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 1808 | 3574 | $0.0006 |
| q04_pix_bank_account | OK ['knowledge'] | OK | OK  | 100% | 80% | 80% | 100% | pass | 5688 | 8221 | $0.0014 |
| q05_terminal_offline | OK ['support', 'knowledge'] | OK | OK  | 100% | 40% | 80% | 100% | fail | 12333 | 10919 | $0.0023 |
| q06_receivables_advance | OK ['knowledge'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 5526 | 4563 | $0.0009 |
| q07_fx_euro | OK ['knowledge'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 5167 | 3800 | $0.0007 |
| q08_decline_error | OK ['support', 'knowledge'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 19421 | 13483 | $0.0030 |
| q09_installments | OK ['knowledge'] | OK | OK  | 100% | 100% | 80% | 100% | pass | 3141 | 4236 | $0.0007 |
| q10_payment_link_whatsapp | OK ['knowledge'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 4204 | 4095 | $0.0007 |
| x01_portuguese | OK ['support', 'knowledge'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 7569 | 6590 | $0.0014 |
| x02_greeting | OK [] | OK | OK  | 100% | 100% | 100% | 100% | pass | 1034 | 1180 | $0.0002 |
| x03_unknown_customer | OK ['support'] | OK | - agent_unresolved | 100% | 100% | 80% | 100% | pass | 2247 | 4486 | $0.0007 |
| x04_cross_customer_attempt | OK ['support'] | OK | FAIL agent_unresolved | 100% | 100% | 60% | 100% | pass | 2686 | 3313 | $0.0006 |
| h01_human_request | OK [] | OK | OK user_requested | 100% | 100% | 100% | 100% | pass | 569 | 969 | $0.0002 |
| h02_human_request_pt | OK [] | OK | OK user_requested | 100% | 100% | 100% | 100% | pass | 600 | 977 | $0.0002 |
| h03_unanswerable | OK ['support', 'knowledge'] | OK | OK agent_unresolved | 100% | 100% | 100% | 100% | pass | 2866 | 5348 | $0.0009 |
| m01_recall_color | OK [] | OK | OK  | 20% | 100% | 20% | 80% | fail | 1776 | 1323 | $0.0003 |
| m02_recall_which_bigger | OK [] | OK | OK  | 100% | 100% | 100% | 100% | pass | 2131 | 1331 | $0.0003 |
| m03_rephrase_previous | FAIL ['knowledge'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 3663 | 5048 | $0.0009 |
| m04_followup_product | OK ['knowledge'] | OK | OK  | 100% | 80% | 100% | 100% | pass | 3713 | 4316 | $0.0007 |
| m05_followup_payout | OK ['support', 'knowledge'] | OK | OK  | 40% | 100% | 40% | 80% | fail | 7430 | 9012 | $0.0016 |
| ho01_get_tap (holdout) | OK ['knowledge'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 3671 | 4089 | $0.0007 |
| ho02_profile_plan_bank (holdout) | FAIL ['support', 'knowledge'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 9707 | 17378 | $0.0029 |
| ho03_advance_enabled_and_how (holdout) | OK ['support', 'knowledge'] | OK | OK  | 100% | 40% | 60% | 100% | fail | 13149 | 9290 | $0.0018 |
| ho04_human_request_es (holdout) | OK [] | OK | OK user_requested | 100% | 100% | 100% | 100% | pass | 2130 | 998 | $0.0002 |
| ho05_out_of_scope_poem (holdout) | OK [] | OK | OK  | 100% | 100% | 100% | 100% | pass | 1916 | 1226 | $0.0002 |

## Judge failures

- **q05_terminal_offline**: The answer invents specific technical details (2.4 GHz only, menu paths, battery impact on Wi-Fi) that are not supported by the provided evidence.
- **m01_recall_color**: The assistant failed to recall the user's previously stated favorite color (green) and incorrectly claimed it could not store personal preferences, violating the specific multi-turn recall requirement.
- **m05_followup_payout**: The assistant failed to answer the user's specific question about the payout *after* the next one, instead repeating the details of the immediate next payout (yesterday's sales) which was already addressed in the previous turn.
- **ho03_advance_enabled_and_how**: The assistant provided incorrect navigation steps (Vendas vs Serviços) and invented details (Pix/debit options, 1-day credit time) that are not supported by the evidence, which specifies a different menu path and a minimum amount requirement.