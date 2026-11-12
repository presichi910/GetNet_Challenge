# Evaluation report

_Generated 20261112-092912_

_Models: knowledge = `openai/gpt-oss-120b`, router / synthesizer = `openai/gpt-oss-20b`, support = `openai/gpt-oss-20b`, judge = `qwen/qwen3.8-27b`_

## Summary

_Judge scores are shown as a percentage of the maximum (5)._

- **provider_errors**: 0
- **routing_accuracy**: 100%
- **tool_use_accuracy**: 89%
- **degraded_rate**: 0%
- **latency_ms_p50**: 11274
- **latency_ms_p95**: 38717
- **handoff_accuracy**: 100%
- **handoff_rate**: 44%
- **avg_relevance**: 100%
- **avg_groundedness**: 100%
- **avg_completeness**: 89%
- **avg_tone**: 100%
- **judge_pass_rate**: 100%

## Cases

| id | route | tools | handoff | rel | grounded | compl | tone | verdict | ms |
|---|---|---|---|---|---|---|---|---|---|
| q03_payout_timing | OK ['support'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 3825 |
| q05_terminal_offline | OK ['support', 'knowledge'] | OK | OK  | 100% | 100% | 80% | 100% | pass | 11061 |
| q08_decline_error | OK ['support', 'knowledge'] | FAIL | OK  | 100% | 100% | 80% | 100% | pass | 11274 |
| x01_portuguese | OK ['support'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 23236 |
| x03_unknown_customer | OK ['support'] | OK | - agent_unresolved | 100% | 100% | 80% | 100% | pass | 38717 |
| x04_cross_customer_attempt | OK ['support'] | OK | OK  | 100% | 100% | 60% | 100% | pass | 11580 |
| h01_human_request | OK [] | OK | OK user_requested | 100% | 100% | 100% | 100% | pass | 3627 |
| h02_human_request_pt | OK [] | OK | OK user_requested | 100% | 100% | 100% | 100% | pass | 2851 |
| h03_unanswerable | OK ['support'] | OK | OK agent_unresolved | 100% | 100% | 100% | 100% | pass | 18880 |