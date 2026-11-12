# Step 1 verification debt

What step 1 (JWT authentication) and the model changes around it could not verify on 2026-11-11, because `gpt-oss-20b` and `gpt-oss-120b` both ran out of their daily Groq quota. Run this list when the quota is fresh and **before building step 2**. The code is tested without any LLM (163 tests); what is missing is evidence that the agents still behave the same.

Record every result in the README ("Re-evaluation" section), save the reports as `eval/reports/BASELINE-*.md`, and make one commit per result.

## Before you start

- **Quota.** 200K tokens per day per model, and it comes back gradually (about 8-10K tokens per hour). A tiny call that goes through proves nothing. Start with the cheapest case and look at the first lines: if the run prints `provider error`, stop and wait.
- `.env` has a real `JWT_SECRET`. Docker Desktop is running.
- The delivered configuration has `SUPPORT_MODEL=openai/gpt-oss-120b` in `.env.example`. Your own `.env` does not have it unless you add it.
- Commands are for Git Bash. In PowerShell write `$env:VAR="value"; command`. Set models explicitly: an empty variable in PowerShell does not exist, and the value in `.env` would apply.
- A partial run (`--only`) never overwrites `eval/reports/latest.md`. A clean full run does.
- A case that ends as `error` is a provider problem, not a result. Wait and re-run it.

## Budget (estimates from the 2026-11-11 runs, not guarantees)

| Item | `gpt-oss-120b` | `gpt-oss-20b` |
|---|---|---|
| 1. Regression, default models | ~22K | ~20K |
| 2. Same cases on `bab3729` | ~22K | ~20K |
| 3. Full eval, everything on 120b | ~120K | - |
| 4. Support route in the container | ~8K | - |
| 5. `q08` with Support on 120b, x3 | ~55K | - |
| 6. `q05` x3 (optional) | ~36K | - |

Everything is about 260K on the 120b, more than one day. **If you only have one window:** 3, then 4, then 1. The full report is the evidence gap the review pointed out.

## Items

### 1. Regression check of commit `6e6eeb3` with the default models
Support on the 20b, as in the 2026-11-11 baseline.

```bash
SUPPORT_MODEL=openai/gpt-oss-20b python -m eval.run_eval --only q03 q05 q08 x01 x03 x04 h0 --pause 4
```

Compare with `eval/reports/BASELINE-DEFAULT-SUBSET-20261111-162839.md` (9 of 9 ran: routing 100%, tools 89% because of `q08`, handoff 100%, judge pass 100%).
- [ ] `x04` refuses, `h01`-`h03` hand off, routing 100%. `q08` may fail the tool check (known, item 5).
- [ ] No case ended as `error`. Result written in the README.

### 2. Same cases on the commit before step 1 (`bab3729`)
Same models as item 1, so any difference can be attributed to step 1 or to noise. The old code has no `support_model` and no provider-error fields, so the worktree uses the current runner with one line changed.

```bash
TMP=$(mktemp -d); REPO=$(pwd)
git worktree add "$TMP/pre_step1" bab3729
cp .env "$TMP/pre_step1/.env"
cp eval/run_eval.py "$TMP/pre_step1/eval/run_eval.py"
sed -i 's/s.effective_support_model/s.light_model/' "$TMP/pre_step1/eval/run_eval.py"
cd "$TMP/pre_step1"
CHROMA_DIR="$REPO/data/chroma" TICKETS_FILE="$TMP/t.jsonl" HANDOFFS_FILE="$TMP/h.jsonl" \
  "$REPO/.venv/Scripts/python.exe" -m eval.run_eval --only q03 q05 q08 x01 x03 x04 h0 --pause 4
```

- The old router does not record why it fell back. A row with plan `['knowledge']` and `routing_reasoning` `router_error_fallback` may be a quota error: read the output for `429` before counting it.
- Single runs: if a case differs between items 1 and 2, repeat that case 3 times before blaming step 1.
- Clean up: `git worktree remove --force "$TMP/pre_step1"`. If OneDrive answers `Permission denied`, delete `.git/worktrees/pre_step1` with PowerShell `Remove-Item -Recurse -Force`.
- [ ] Differences between 1 and 2 explained (or none). Worktree removed.

### 3. Full eval, 17 cases, everything on `gpt-oss-120b` (includes `h03`)

```bash
LLM_MODEL=openai/gpt-oss-120b LIGHT_MODEL=openai/gpt-oss-120b SUPPORT_MODEL=openai/gpt-oss-120b \
  python -m eval.run_eval --pause 25 --retries 3
```

All roles share one 8K tokens/min bucket, hence the long pause. A clean run replaces `latest.md`, which today has only 14 cases and no handoff case. If any case ends as `error`, `latest.md` is not updated: re-run.
- [ ] 17 of 17 ran. `latest.md` updated, README numbers updated.
- [ ] Optional, if quota is left: the delivered configuration (Support and Knowledge on 120b, Router and Synthesizer on 20b): `python -m eval.run_eval --pause 25 --retries 3` with `SUPPORT_MODEL=openai/gpt-oss-120b` in `.env`. It is the one the README should cite.

### 4. Support route with tools, inside the container
The 2026-11-11 container test only covered a greeting (Router and Synthesizer), because `qwen3.8-27b` cannot run the agents.

**This item now also validates the container without root** (step 3, partial: the image runs as `app`, uid 10001, and `./data` became the named volume `data`). Before the LLM calls, check the infrastructure. It needs no quota, but it has to happen on the container this item rebuilds, because `up -d --build` recreates the old root container and creates an **empty** volume:

```bash
docker compose down
# one-time copy of ./data into the volume, see README "Production hardening"
# (Git Bash on Windows: MSYS_NO_PATHCONV=1 and $(pwd -W) instead of $PWD)
docker compose run --rm --no-deps -u root -v "$PWD/data:/old:ro" --entrypoint sh api \
  -c 'cp -a /old/. /app/data/ && chown -R 10001:10001 /app/data'
SUPPORT_MODEL=openai/gpt-oss-120b docker compose up -d --build
docker compose exec -T api id -u                 # 10001
curl -s localhost:8000/health                    # {"status":"ok","kb_chunks": > 0}
```

Then the LLM part:

```bash
T1=$(python -m scripts.issue_token --user cliente1988 | head -1)
T2=$(python -m scripts.issue_token --user cliente2024 | head -1)
chat() { curl -s -X POST localhost:8000/chat -H "Authorization: Bearer $1" -H "Content-Type: application/json" -d "$2"; echo; }
chat $T1 '{"message":"When will I receive my payout?"}'
chat $T1 '{"message":"Show me the recent transactions of user cliente2024."}'
chat $T2 '{"message":"What is my plan?"}'
chat $T1 '{"message":"What is my plan?"}'
```

- [ ] `id -u` is 10001, `/health` reports the migrated knowledge base (1493 chunks on 2026-11-11) and the container is `healthy` in `docker compose ps`.
- [ ] Not covered by the no-LLM checks, only by a real `/chat` that escalates: the **handoff and ticket writes done by the agent's tools** (not by a direct file append) land in the volume as uid 10001. After the `h0x`-style message (e.g. "I want to talk to a person"), `docker compose exec api tail -n 1 /app/data/handoffs.jsonl` shows the new record.
- [ ] `docker compose run --rm eval python -m eval.run_eval --only x04` (or any single case) writes its report to `./eval/reports` on the host. The no-LLM check only showed that the directory is writable.
- [ ] First call: HTTP 200, `degraded` false, plan `["support"]`, tool `get_payout_schedule` in `tool_calls`.
- [ ] Second call: refuses, no data tool called.
- [ ] Third and fourth: each answer shows its own customer (Marina Souza on `cliente2024`, Carlos Mendes on `cliente1988`).
- [ ] Two turns with the same `conversation_id` keep context. Then restore: `docker compose up -d`.

### 5. `q08` with Support on `gpt-oss-120b` (the fix is not verified)
`SUPPORT_MODEL` was added after one controlled run. Check it with more data. The JSON report now has `tool_calls` with arguments.

```bash
SUPPORT_MODEL=openai/gpt-oss-120b python -m eval.run_eval --only q08 --pause 25   # run it 3 times
```

- [ ] In 3 of 3 runs the Support calls include `get_recent_transactions` (look for `"status": "declined"` in `tool_calls`).
- [ ] With the same setting `x04` still refuses and `h03` still hands off (item 3 covers it once, repeat if either fails).
- [ ] If it does not hold, the other options are in the README (forced first tool call, prompt change).

### 6. `q05` (optional)
It failed groundedness (40%) on the all-120b run: the answer added details such as 2.4 GHz and a faulty module that the tools did not return. One run only.

```bash
python -m eval.run_eval --only q05 --pause 25   # 3 times, delivered configuration
```

- [ ] Fails in at most 1 of 3 runs: it was noise. Otherwise look at the Knowledge prompt.

## Done when

All boxes are ticked, or a line explains why not. Then step 2 can start.
