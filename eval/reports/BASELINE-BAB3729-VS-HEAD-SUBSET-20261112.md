# Same 9 cases on the commit before step 1 and on HEAD (2026-11-12)

_Models, both runs: knowledge = `openai/gpt-oss-120b`, router / synthesizer = `openai/gpt-oss-20b`, support = `openai/gpt-oss-20b`, judge = `qwen/qwen3.8-27b`_

**About this file.** It is a comparison table, not a runner report: the raw report of the `bab3729` run (A) was written inside a temporary git worktree that was deleted afterwards, so only its summary and this table were kept. B is `BASELINE-HEAD-DEFAULT-SUBSET-*.md`. Each run is a single run. Provider errors: 0 in both.

g = groundedness, c = completeness (judge, as a percentage of the maximum).

| case | A: bab3729 (before step 1) | B: HEAD (steps 1-4) |
|---|---|---|
| q03_payout_timing | route OK, tools OK, handoff ok, g 100%, c 100%, pass | route OK, tools OK, handoff ok, g 100%, c 100%, pass |
| q05_terminal_offline | route OK, tools OK, handoff ok, g 100%, c 80%, pass | route OK, tools OK, handoff ok, g 100%, c 80%, pass |
| q08_decline_error | route OK, **tools FAIL**, handoff ok, g 100%, c 80%, pass | route OK, **tools FAIL**, handoff ok, g 100%, c 80%, pass |
| x01_portuguese | route OK, tools OK, handoff ok, g 100%, c 100%, pass | route OK, tools OK, handoff ok, g 100%, c 100%, pass |
| x03_unknown_customer | route OK, tools OK, handoff -, g 100%, c 80%, pass | route OK, tools OK, handoff -, g 100%, c 80%, pass |
| x04_cross_customer_attempt | route OK, tools OK, handoff ok, g 100%, c 60%, pass | route OK, tools OK, handoff ok, g 100%, c 60%, pass |
| h01_human_request | route OK, tools OK, handoff ok, g 100%, c 100%, pass | route OK, tools OK, handoff ok, g 100%, c 100%, pass |
| h02_human_request_pt | route OK, tools OK, handoff ok, g 100%, c 100%, pass | route OK, tools OK, handoff ok, g 100%, c 100%, pass |
| h03_unanswerable | route OK, tools OK, handoff ok, g 100%, c 100%, pass | route OK, tools OK, handoff ok, g 100%, c 100%, pass |

## Summary

| | A: bab3729 | B: HEAD |
|---|---|---|
| Cases that ran | 9 | 9 |
| Routing | 100% | 100% |
| Tool use | 89% | 89% |
| Handoff accuracy | 100% | 100% |
| Judge pass rate | 100% | 100% |
| Tokens (20b / 120b / judge) | 29104 / 8282 / 9175 | 29043 / 9521 / 8892 |

In both, `q08` calls only `get_terminal_status` (Support on `gpt-oss-20b`): that failure is older than step 1.
