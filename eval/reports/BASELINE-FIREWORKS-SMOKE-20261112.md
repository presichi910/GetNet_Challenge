# Evaluation report

_Generated 20261112-124843_

_Models: agent provider = `fireworks`, knowledge = `accounts/fireworks/models/gpt-oss-120b`, router / synthesizer = `accounts/fireworks/models/gpt-oss-120b`, support = `accounts/fireworks/models/gpt-oss-120b`, judge = `qwen/qwen3.8-27b (groq)`_

## Summary

_Judge scores are shown as a percentage of the maximum (5)._

- **provider_errors**: 0
- **routing_accuracy**: 100%
- **tool_use_accuracy**: 100%
- **degraded_rate**: 0%
- **latency_ms_p50**: 2106
- **latency_ms_p95**: 4169
- **handoff_accuracy**: 67%
- **handoff_rate**: 33%
- **avg_relevance**: 100%
- **avg_groundedness**: 100%
- **avg_completeness**: 93%
- **avg_tone**: 100%
- **judge_pass_rate**: 100%

## Cases

| id | route | tools | handoff | rel | grounded | compl | tone | verdict | ms |
|---|---|---|---|---|---|---|---|---|---|
| q03_payout_timing | OK ['support'] | OK | OK  | 100% | 100% | 100% | 100% | pass | 4169 |
| x02_greeting | OK [] | OK | OK  | 100% | 100% | 100% | 100% | pass | 1054 |
| x04_cross_customer_attempt | OK ['support'] | OK | FAIL agent_unresolved | 100% | 100% | 80% | 100% | pass | 2106 |