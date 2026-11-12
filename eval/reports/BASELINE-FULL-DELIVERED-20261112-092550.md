# Evaluation report

_Generated 20261112-092550_

_Models: knowledge = `openai/gpt-oss-120b`, router / synthesizer = `openai/gpt-oss-20b`, support = `openai/gpt-oss-120b`, judge = `qwen/qwen3.8-27b`_

## Summary

_Judge scores are shown as a percentage of the maximum (5)._

- **provider_errors**: 0
- **routing_accuracy**: 100%
- **tool_use_accuracy**: 100%
- **degraded_rate**: 0%
- **latency_ms_p50**: 4234
- **latency_ms_p95**: 8579
- **handoff_accuracy**: 100%
- **handoff_rate**: 24%
- **avg_relevance**: 100%
- **avg_groundedness**: 93%
- **avg_completeness**: 94%
- **avg_tone**: 100%
- **judge_pass_rate**: 100%

## Cases

| id | route | tools | handoff | rel | grounded | compl | tone | verdict | ms |
|---|---|---|---|---|---|---|---|---|---|
| q01_compare_plans | OK ['knowledge'] | OK | OK  | 100% | 80% | 100% | 100% | pass | 8579 |
| q02_weather | OK ['knowledge'] | OK | OK  | 100% | 80% | 80% | 100% | pass | 8445 |
| q03_payout_timing | OK ['support'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 4011 |
| q04_pix_bank_account | OK ['knowledge'] | OK | OK  | 100% | 80% | 80% | 100% | pass | 4234 |
| q05_terminal_offline | OK ['support', 'knowledge'] | OK | OK  | 100% | 80% | 80% | 100% | pass | 36765 |
| q06_receivables_advance | OK ['knowledge'] | OK | OK  | 100% | 80% | 100% | 100% | pass | 5002 |
| q07_fx_euro | OK ['knowledge'] | OK | OK  | 100% | 80% | 100% | 100% | pass | 6483 |
| q08_decline_error | OK ['support', 'knowledge'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 8487 |
| q09_installments | OK ['knowledge'] | OK | OK  | 100% | 100% | 80% | 100% | pass | 3105 |
| q10_payment_link_whatsapp | OK ['knowledge'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 6048 |
| x01_portuguese | OK ['support'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 2778 |
| x02_greeting | OK [] | OK | OK  | 100% | 100% | 100% | 100% | pass | 3270 |
| x03_unknown_customer | OK ['support'] | OK | - agent_unresolved | 100% | 100% | 80% | 100% | pass | 2307 |
| x04_cross_customer_attempt | OK ['support'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 4440 |
| h01_human_request | OK [] | OK | OK user_requested | 100% | 100% | 100% | 100% | pass | 1377 |
| h02_human_request_pt | OK [] | OK | OK user_requested | 100% | 100% | 100% | 100% | pass | 1333 |
| h03_unanswerable | OK ['support'] | OK | OK agent_unresolved | 100% | 100% | 100% | 100% | pass | 2084 |