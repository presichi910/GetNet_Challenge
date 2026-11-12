# Evaluation report

_Generated 20261111-162839_

_Models (line added by hand, the runner did not record them yet): knowledge = `openai/gpt-oss-120b`, router / support / synthesizer = `openai/gpt-oss-20b`, judge = `qwen/qwen3.8-27b`_

## Summary

_Judge scores are shown as a percentage of the maximum (5)._

- **routing_accuracy**: 100%
- **tool_use_accuracy**: 89%
- **degraded_rate**: 0%
- **latency_ms_p50**: 14646
- **latency_ms_p95**: 38169
- **handoff_accuracy**: 100%
- **handoff_rate**: 44%
- **avg_relevance**: 100%
- **avg_groundedness**: 98%
- **avg_completeness**: 89%
- **avg_tone**: 100%
- **judge_pass_rate**: 100%

## Cases

| id | route | tools | handoff | rel | grounded | compl | tone | verdict | ms |
|---|---|---|---|---|---|---|---|---|---|
| q03_payout_timing | OK ['support'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 6164 |
| q05_terminal_offline | OK ['support', 'knowledge'] | OK | OK  | 100% | 80% | 80% | 100% | pass | 17408 |
| q08_decline_error | OK ['support', 'knowledge'] | FAIL | OK  | 100% | 100% | 80% | 100% | pass | 28772 |
| x01_portuguese | OK ['support'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 5117 |
| x03_unknown_customer | OK ['support'] | OK | - agent_unresolved | 100% | 100% | 80% | 100% | pass | 38169 |
| x04_cross_customer_attempt | OK ['support'] | OK | OK  | 100% | 100% | 60% | 100% | pass | 14646 |
| h01_human_request | OK [] | OK | OK user_requested | 100% | 100% | 100% | 100% | pass | 2603 |
| h02_human_request_pt | OK [] | OK | OK user_requested | 100% | 100% | 100% | 100% | pass | 5797 |
| h03_unanswerable | OK ['support'] | OK | OK agent_unresolved | 100% | 100% | 100% | 100% | pass | 15937 |