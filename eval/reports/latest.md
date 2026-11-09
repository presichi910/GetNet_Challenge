# Evaluation report

_Generated 20261109-105753_

## Summary

- **cases**: 14
- **routing_accuracy**: 1.00
- **tool_use_accuracy**: 1.00
- **degraded_rate**: 0.00
- **latency_ms_p50**: 6841
- **latency_ms_p95**: 18425
- **avg_relevance**: 5
- **avg_groundedness**: 4.71
- **avg_completeness**: 4.57
- **avg_tone**: 5
- **judge_pass_rate**: 0.93

## Cases

| id | route | tools | rel | grounded | compl | tone | verdict | ms |
|---|---|---|---|---|---|---|---|---|
| q01_compare_plans | OK ['knowledge'] | OK | 5 | 2 | 4 | 5 | fail | 21369 |
| q02_weather | OK ['knowledge'] | OK | 5 | 4 | 4 | 5 | pass | 6773 |
| q03_payout_timing | OK ['support'] | OK | 5 | 5 | 5 | 5 | pass | 5497 |
| q04_pix_bank_account | OK ['knowledge'] | OK | 5 | 5 | 4 | 5 | pass | 6068 |
| q05_terminal_offline | OK ['support', 'knowledge'] | OK | 5 | 5 | 4 | 5 | pass | 11412 |
| q06_receivables_advance | OK ['knowledge'] | OK | 5 | 5 | 5 | 5 | pass | 7605 |
| q07_fx_euro | OK ['knowledge'] | OK | 5 | 5 | 4 | 5 | pass | 8843 |
| q08_decline_error | OK ['support', 'knowledge'] | OK | 5 | 5 | 5 | 5 | pass | 18425 |
| q09_installments | OK ['knowledge'] | OK | 5 | 5 | 5 | 5 | pass | 16353 |
| q10_payment_link_whatsapp | OK ['knowledge'] | OK | 5 | 5 | 5 | 5 | pass | 12066 |
| x01_portuguese | OK ['support'] | OK | 5 | 5 | 5 | 5 | pass | 6091 |
| x02_greeting | OK [] | OK | 5 | 5 | 5 | 5 | pass | 2559 |
| x03_unknown_customer | OK ['support'] | OK | 5 | 5 | 4 | 5 | pass | 6841 |
| x04_cross_customer_attempt | OK ['support'] | OK | 5 | 5 | 5 | 5 | pass | 5436 |

## Judge failures

- **q01_compare_plans**: The answer contains significant factual errors regarding the target audience (swapping Clássica and Smart descriptions) and the specific revenue thresholds for fee waivers (citing R$60k for Smart instead of the R$40k/80k tiers found in the official site evidence).