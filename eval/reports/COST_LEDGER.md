# Cost ledger (generated)

_Written by `eval/cost_guard.py` after every guarded run, and by `python -m eval.cost_guard note` for spending outside one. Nothing below is typed by hand except the `text` of a note._

## The rule, and how it differs from the plan

- Abort if `spent + 1.5 x estimate > 8.5` or `1.5 x estimate > 3.0` for one run. The plan said `spent + estimate > 9.0` or `estimate > 3.0`, with the estimate as it was.
- **Why**: on 2026-11-19 the host measurement reported US$0.0211 from its responses and the key's counter moved about twice that (the rows of a run that hits a provider error record no usage, but the graph kept running). So the estimate is padded by 1.5, and the **spent** side is read from the key's counter (`GET /api/v1/key`), never summed from `usage.cost`.
- **Why 8.5 and not 9.0**: the key is limited to US$9, and OpenRouter cutting a run in the middle would leave partial data. If the key has a limit of its own, the line is the lower of 8.5 and that limit minus 0.5.
- The counter lags behind the spending, so each run's real cost is read after the counter stopped moving for three reads in a row (at least 45 s) (`settled` below says whether it did; at least 45 s and three equal reads).

## Runs

| # | when (UTC) | block | command | estimate | real (counter) | real / estimate | reported by the responses | real / reported | settled | assumed roles |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 2026-11-20 04:43:59 | M1 (before the guard) | `-` | - | US$0.0420 | - | - | - | yes | - |
| 2 | 2026-11-20 04:45:35 | cost guard smoke | `python -m eval.run_eval --regular-only --no-judge --only m01 --label c` | US$0.0011 | US$0.0002 | 0.17 | US$0.0002 | 1.00 | yes | - |
| 3 | 2026-11-20 04:47:16 | ledger housekeeping | `-` | - | US$0.0000 | - | - | - | yes | - |

### Notes

- #1 (M1 (before the guard)): Everything the key had spent before the guard existed: the two V2 runs, the two V3 smokes, the host measurement and its crashed attempt. The responses' usage.cost summed to about US$0.0321 for these; the key's counter is the figure used.
- #3 (ledger housekeeping): Two entries (block rejudge, at 04:39 and 04:43 UTC on 2026-11-20) were written by a test that ran rejudge without --no-cost-guard, so it read the real key and wrote the real ledger: they spent nothing and were removed. tests/conftest.py now makes any test that reaches the key counter, the price listing or the real ledger fail loudly.

## Spent per block (real, from the counter)

| block | real |
|---|---|
| M1 (before the guard) | US$0.0420 |
| cost guard smoke | US$0.0002 |
| ledger housekeeping | US$0.0000 |
| **total of the entries** | **US$0.0422** |

## Calibration of the factor

- Runs with an estimate and a settled real cost: 1. real / estimate: median 0.17, largest 0.17, smallest 0.17.
- The factor in use is 1.5: every run stayed under it.
