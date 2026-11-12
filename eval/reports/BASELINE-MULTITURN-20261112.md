# Multi-turn cases `m01`-`m05`: how the Router routes follow-ups today (2026-11-12)

_Models: router / synthesizer = `openai/gpt-oss-20b` (the Router is the thing measured), knowledge and support = `openai/gpt-oss-120b`, judge = `qwen/qwen3.8-27b`. Router prompt unchanged._

**About this file.** Raw numbers from two kinds of run, both partial because the Groq daily quota ran out (120b during the end-to-end run, then the 20b during the Router-only run). No case was counted as a failure because of a quota error. The scripts were scratch scripts and are not in the repo: the end-to-end one is `python -m eval.run_eval --only m0`, the Router-only one called the real Router node with each case's history.

The earlier turns of a case are given to the graph as context (field `history` in `eval/dataset.jsonl`), so setting a case up costs no model calls.

## What each case should do

| case | message (after a short earlier turn) | correct route |
|---|---|---|
| m01_recall_color | "What favorite color did I tell you?" | none (answer from the conversation) |
| m02_recall_which_bigger | "Which of the two days was bigger?" | none |
| m03_rephrase_previous | "Can you say that again in simpler words?" | none |
| m04_followup_product | "Does the second one accept Pix?" | knowledge |
| m05_followup_payout | "And what about the one after that?" | support (knowledge optional) |

## Route accuracy today (Router only, current prompt, 3 repetitions, 15 answered calls)

| case | routing OK | plans returned |
|---|---|---|
| m01_recall_color | 0/3 | knowledge x3 |
| m02_recall_which_bigger | 0/3 | knowledge x3 |
| m03_rephrase_previous | 0/3 | knowledge x3 |
| m04_followup_product | 3/3 | knowledge x3 |
| m05_followup_payout | 3/3 | support+knowledge x2, support x1 |
| **all** | **6/15 (40%)** | the 3 follow-ups that need no agent never get an empty plan |

End-to-end run 1 (before the 120b quota ran out) agrees: m01, m02, m03 -> `knowledge`, m04 and m05 correct. In m01 the Knowledge agent answered "I'm unable to locate a record of the color you mentioned". Runs 2 and 3 of that end-to-end run hit the 120b daily limit and are excluded (provider errors).

Tokens per Router call: about 939 input + 63 output (short histories).

## Experiment, NOT in the repo: tell the Router which route the previous turn took

One extra system message before the last customer message ("The previous turn of this conversation was routed to: [...]"). The Router prompt text was not changed. 11 answered calls (the 20b quota ended in repetition 3).

| case | routing OK |
|---|---|
| m01 / m02 / m03 (recall, rephrase) | 0/3, 0/2, 0/2 (knowledge every time) |
| m04 | 2/2 |
| m05 | 2/2 |
| **all** | **4/11** |

Cost: 962 input tokens per call against 939 (+23, +2.5%), 67 output against 63.

## Reading

- The Router already sees the conversation (the last `history_window` = 6 messages plus the rolling summary). `tests/test_followup_context.py` shows it with a real graph and real SQLite: on turn 2 the Router, Support, Knowledge and the direct-answer path all receive turn 1.
- So the failure is not that the Router cannot see earlier turns. The Router prompt says to return an empty plan for greetings, thanks and unclear requests and to send every other question to an agent, and it has no example of a question about the conversation itself. The model follows that.
- Passing the previous route did not change a single recall case in the calls that ran.
