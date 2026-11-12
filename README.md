# Getnet multi-agent support

A small multi-agent assistant for Getnet customers. A router reads the message and decides who answers: a knowledge agent (Getnet website via RAG, or the web) and/or a support agent (the customer's own account data). If nobody can solve it, a human takes over. There's also an offline evaluator that scores the whole thing.

Stack: LangGraph, `gpt-oss` models on Groq or Fireworks (`AGENT_PROVIDER`; the delivered runs of 2026-11-12 use Fireworks `gpt-oss-120b` for every agent), `qwen3.8-27b` on Groq as judge, Voyage embeddings, Chroma, Tavily, FastAPI, Docker.

For the reviewer of V4: [docs/V4_REMEDIATION.md](docs/V4_REMEDIATION.md) maps every point of the review to commits, tests and reports.

Demo video: https://youtu.be/bnBYBmf2AlQ

More docs: [SPEC](docs/SPEC.md)

## How it works

```mermaid
flowchart TD
    U[User] --> API[API /chat]
    API --> R[Router]
    R --> S[Support agent]
    R --> K[Knowledge agent]
    S --> K
    S --> Y[Synthesizer]
    K --> Y
    R -->|asks for a human| H[Human handoff]
    S -->|can't resolve| H
    K -->|can't resolve| H
    Y --> A[Answer]
    H --> A
```

1. The router picks the agents (`support`, `knowledge`, both, or none) and detects the language. If both are needed, support goes first so knowledge can use what it found.
2. Each agent uses tools. Support reads account data, knowledge searches the Getnet KB (Chroma) or the web (Tavily).
3. The synthesizer builds the final reply: none ran → answers directly, one → passes it through, two → merges them.
4. If the user asks for a person, or nobody could resolve it, the handoff node opens a ticket, queues a record for the operators (SQLite, plus an audit copy in `data/handoffs.jsonl`; both in the `data` volume when running in Docker, see "Human oversight") and tells the user.

The response also includes which agents ran, the tools they called and the sources, so it's easy to debug.

## Run it

```bash
cp .env.example .env            # add GROQ_API_KEY, TAVILY_API_KEY, VOYAGE_API_KEY and a real JWT_SECRET

docker compose build
docker compose run --rm ingest  # builds the knowledge base (takes a while on Voyage's free tier)
docker compose up -d            # API on http://localhost:8000
```

```bash
# the API needs a JWT (see Authentication). This makes one for the demo customer cliente1988:
TOKEN=$(docker compose exec -T api python -m scripts.issue_token --user cliente1988 | head -1)

curl -s -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"message": "My card machine won'\''t connect to the internet, what should I do?"}'
```

Without Docker:

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python -m app.rag.ingest
uvicorn app.main:app --reload
python -m scripts.issue_token --user cliente1988     # prints a token and an example curl
```

`python -m app.rag.ingest --from-cache` re-indexes without spending Tavily credits. `GET /health` shows how many chunks are in the KB.

**Demo customers** (fake data):

| Token `sub` | What's going on |
|---|---|
| `cliente1988` | Terminal offline (weak Wi-Fi), pending payouts, two declined sales |
| `cliente2024` | Healthy account, Pix on |
| `cliente3030` | Printer out of paper, one open ticket, Pix off |

## Authentication

`POST /chat` requires `Authorization: Bearer <jwt>`. `GET /health` is public. The token has a `role` (`merchant`, the default, or `operator`): see "Human oversight". The rest of this section is about merchants.

- **Who is calling comes only from the token.** The JWT is verified with PyJWT: HS256 fixed in code (so `alg: none` or any other algorithm is rejected), signature, `exp`, `iss` and `aud`, and the claims `exp`, `iss`, `aud`, `sub` are required. The `sub` claim is the customer (`user_id`). It goes to the tools, the memory thread, the tickets, the handoff records and the logs. The LLM never chooses it: the tools are bound to it and take no user argument. The Support prompt does show the id (it comes from the verified token, never from the user's text) so the model can tell "my data" from someone else's. Removing it made the cross-customer refusal case (`x04`) fail in 5 of 5 runs of an A/B test.
- **Status codes.** `401`: token missing, malformed, expired, bad signature, wrong issuer/audience or invalid `sub` (always the same generic message, the real reason is only in the server log). `403`: valid token without the `chat` scope (`scope` claim, space separated), with the wrong `role` (an operator token on `/chat`), or a body `user_id` that is not the token's. `404` on `/conversations/{id}`: the conversation does not exist **or is not yours** (same answer, on purpose).
- **Body `user_id` is optional.** It's still accepted so clients that follow the original `{message, user_id}` contract keep working, but it is never used as identity: if present it must equal the token's `sub`.
- **Conversations.** Optional `conversation_id` (`[A-Za-z0-9_.-]`, max 64) lets one customer have several conversations. The memory thread is `<sub>:<conversation_id>` (default `default`). The prefix always comes from the token, so nobody can read another customer's thread, whatever `conversation_id` they send. The threads are persistent, with a rolling summary, a retention period and a limit per customer: see "Conversation memory" below, which also lists the endpoints to list, read and delete your own conversations.
- **The app won't start with a weak secret.** `JWT_SECRET` (plus `JWT_ISSUER`, `JWT_AUDIENCE`) comes from `.env`. Empty, shorter than 32 characters, repetitive or placeholder-looking values are rejected at startup.

```bash
python -m scripts.issue_token --user cliente1988                       # chat scope, 15 min
python -m scripts.issue_token --user cliente1988 --scopes "chat" --minutes 60
docker compose exec -T api python -m scripts.issue_token --user cliente1988
```

The token is printed on the first line, followed by an example `curl`. This is a demo: a shared secret means whoever can issue tokens can impersonate anyone. In production I'd use an identity provider that signs with RS256 and publishes its keys (JWKS), and the API would only verify with the public key.

## Human handoff

Three triggers:

| Trigger | When |
|---|---|
| `user_requested` | The user asks for a human (any language). Agents are skipped. |
| `technical_failure` | Every agent failed. High priority. |
| `agent_unresolved` | Every agent called its `escalate_to_human` tool. |

If only one of two agents couldn't resolve it, we don't escalate, the user already got something useful.

A few things I learned while building it:

- **Retrieval score doesn't work as a trigger.** Answerable questions scored 0.41-0.67 and unanswerable ones 0.37-0.50, so any threshold is wrong either way.
- **Tool instead of a text marker.** First version had agents add `[[UNRESOLVED]]` to their reply. It showed up even on good answers (it caused false handoffs in the eval). An explicit `escalate_to_human(reason)` tool call fixed that.
- **The user only sees the handoff notice.** Whatever the unresolved agent found goes to the human, not the customer.
- **A broken notifier never breaks the reply.**

## Human oversight

A handoff is a case a person works through, not a line in a file. Operators claim it, answer it, and the answer reaches the customer and the agents.

```
                 claim (operator A, lease 15 min)             reply (operator A)
   pending ─────────────────────────────────────▶ claimed ─────────────────────────▶ resolved
      ▲  ▲                                          │  │  ▲                         (approve | reject)
      │  │   lease runs out (nobody has to act)     │  │  └── claim again by A: lease renewed
      │  └──────────────────────────────────────────┘  │      claim or reply by B: 409
      │            reject {close: false} by A          │
      └────────────────────────────────────────────────┤
                                  reject {close: true} └──────────────────────────▶ resolved (no message)
   created by the handoff node (reason, summary, whole record)
```

Every arrow is one SQL statement, `UPDATE ... WHERE status = <expected>` (plus `AND claimed_by = <me>` for the operator's own moves), and the rows it changed are counted. 1 means this caller won; 0 means somebody else got there first, and a read says whether it is a `409` or a `404`. That statement is what makes it exactly-once, not a lock in Python: two operators, two threads or two processes can race and SQLite lets one through (the tests race separate connections to the same file). `operator_messages.handoff_id` is also `UNIQUE`, so a handoff cannot deliver two messages even if the code were wrong.

**Roles.** The JWT has a `role` claim: `merchant` or `operator`. A token without it is a merchant, an unknown value is `401`, so nothing becomes an operator by accident.

| | merchant | operator |
|---|---|---|
| `/chat`, `/conversations*` | yes (scope `chat`) | **403**, even with the `chat` scope: an operator is not a merchant and has no conversations |
| `/operator/*` | **403**, even with the `operator` scope | yes (scope `operator`) |
| `sub` means | the merchant id (`user_id`) | the operator id, used in the audit trail |

```bash
python -m scripts.issue_token --role operator --operator ops1       # prints the token and an example curl
```

**Endpoints (operator only).** Times are ISO 8601 UTC. An id that does not exist or is malformed is `404`.

| Endpoint | What it does |
|---|---|
| `GET /operator/handoffs?status=pending&limit=100` | The queue, **oldest first**. `status` is `pending`, `claimed` or `resolved` (none = all), `limit` 1-500. One row per handoff: `id`, `merchant_id`, `conversation_id`, `reason`, `priority`, `summary`, `status`, `claimed_by`, `claimed_at`, `lease_expires_at`, `resolution`, `resolved_at`, `created_at`. |
| `GET /operator/handoffs/{id}` | One handoff with the whole case (`record`: recent conversation, what the agents found, tool calls) and `operator_reply`. |
| `POST /operator/handoffs/{id}/claim` | `pending` to `claimed`, with a lease. `409` if another operator holds a valid claim or it is resolved. Claiming what you already hold is `200` and **renews the lease** (`claimed_at` stays). |
| `POST /operator/handoffs/{id}/reply` | Body `{"decision": "approve" or "reject", "message": "...", "idempotency_key": "..."}`. `claimed` to `resolved`, only by the operator who holds a valid claim. `409` if nobody claimed it, someone else did, the lease ran out, or it is already resolved. Returns the handoff plus `replayed`. |
| `POST /operator/handoffs/{id}/reject` | Body `{"reason": "...", "close": false}`. The operator who holds a valid claim gives the case up. `close: false` (default): back to `pending` for any operator. `close: true`: resolved as `reject` **with no message for the customer** (spam, duplicates). `reason` (1-500 characters) is stored in the audit trail for the other operators and is never shown to the customer. `409` without a valid claim or if resolved. Not to be confused with `reply` + `decision: "reject"`, which answers the customer with a refusal. |

`message` is 1-1000 characters, trimmed, with no control characters (`422` otherwise). `idempotency_key` is 8-64 characters of `[A-Za-z0-9_.-]`, one per attempt, chosen by the client (a uuid works).

**Leases.** A claim holds for `HANDOFF_LEASE_MINUTES` (default 15). After that the handoff is `pending` again: the next list, read, claim or reply (by anybody, or the customer asking for the status) notices and puts it back, records `lease_expired` with the operator who lost it, and another operator can take it. There is no background job to run. The operator who lost the lease can no longer reply (`409`, claim again first); a claim made before the end renews it, so a long case needs a claim call now and then. The clock is injectable in the store, so the tests move time instead of sleeping.

**Idempotency of the reply.** Repeating the same `idempotency_key` with the same decision and message returns the same result (`replayed: true`, same `resolved_at`) and stores nothing again, so retrying after a timeout is safe. The same key with another decision or message is `409`. Another key on a handoff that is already resolved is `409`. The key is checked together with the operator: it does not let a second operator in.

**What the customer gets.**

- The operator's message is saved in SQLite, tied to the conversation, in the queue file and not in the checkpointer. It is delivered in the `operator_messages` field of the **next `/chat` response of that conversation**, once (it is marked delivered in the same transaction, so two simultaneous turns cannot both get it; if the turn then fails it is given back). `GET /conversations/{conversation_id}/operator-messages` lists all of them, delivered or not, without sending a turn. Each has `handoff_id`, `decision`, `message`, `created_at` and `sender: "human_operator"` so a client can label it.
- **The agents read it too.** On that same turn it is added to the conversation just before the customer's message, as an assistant-side message `[Operador humano] (approve) <text>` (or `(reject)`). It is an ordinary message in the thread, so `recent_dialogue`, the rolling summary and the next handoff record carry it with no change to any prompt, node or the router, and it ages out of the window like any other message. When there is no operator reply the input of every agent is identical to what it was before (a test compares the Router input with and without the queue). Because the label is what tells a model "this is a human, not you and not the customer", a customer who types `[Operador humano]` in their own message gets it turned into `[cita: Operador humano]` before it reaches the graph (nothing else in a message is ever changed). The customer also sees it in `GET /conversations/{id}` as an assistant message.
- The handoff status is visible in `/chat`: `handoff.status` for the case opened in this turn, and `open_handoffs` (`[{"id", "status"}]`) for earlier cases of the conversation that are still `pending` or `claimed`. `GET /conversations/{id}` has `pending_human` (`true` while a case of that conversation is pending or claimed). Nothing says who the operator is, and a claim that expired shows as `pending`.

**Security.** A merchant only gets data of their own conversations: every merchant-side query filters by the token's `sub`, and somebody else's conversation or handoff answers like a missing one (`404`). Operators see every merchant's handoffs; that is their job. Each action (create, claim, reply, release, close, lease expiry) is appended to the `handoff_events` table with the operator id and a timestamp, and logged; refused attempts, renewals and replays are not recorded as actions. If the queue breaks, the customer's `/chat` turn still works (the message just stays undelivered).

**Privacy of the queue.**

- `DELETE /conversations/{id}` also erases that conversation's handoffs, their operator messages and their audit events (matched by the token's user and the conversation id), with `secure_delete` on, and truncates the WAL. A test searches a token in the raw bytes of `handoffs.sqlite` and its `-wal` before and after. A case that was in progress disappears from the operators' queue too (`404`): deleting your conversation wins over a case that is still open. If the erase fails the call is a `500` (the thread may already be gone) and repeating it finishes the job.
- `THREAD_TTL_DAYS` also applies to handoffs: one whose last activity (created, claimed or resolved, the latest) is older than that is erased in any status, with its messages and events, by the same startup and periodic purge. A case nobody answered in that time is logged as such. It is a separate clock from the conversation's: an active conversation does not keep an old handoff alive.
- **The JSONL file keeps no excerpt.** `data/handoffs.jsonl` is an audit trail, and a copy of the case in it could not be erased by either of the above, so new lines carry only `handoff_id`, `created_at`, `user_id`, `conversation_id`, `reason`, `priority`, `ticket_id`, `language`, the tool names (`agent:tool`, no arguments) and `"redacted": true`: no message, no dialogue, no agent findings, no summary. The importer skips redacted lines. The case itself lives only in `handoffs.sqlite`. What the customer said can still be in `tickets.jsonl` (the ticket description holds the first 200 characters) and in backups; those are not covered. `FileQueueNotifier` (whole record) still exists for someone who wants that file.

**Storage and the old queue file.** The queue is its own SQLite file, `HANDOFFS_DB_PATH` (default `data/handoffs.sqlite`, in the `data` volume), apart from the conversation checkpoints, so changing how conversations are stored never touches it.

Importing the old JSONL is **off** by default (`HANDOFF_IMPORT_JSONL=false`). Files written before this version hold whole conversation excerpts, and the eval writes its cases to the same file, so loading it blindly would put test cases and old customer text in front of operators. When it is off and the file has unimported records, startup logs a warning with the count. To import it once:

```bash
# 1. back it up (the copy has the excerpts: keep it out of git and OneDrive, delete it when done)
cp data/handoffs.jsonl "data/handoffs.jsonl.bak-$(date +%F)"
#    in Docker:
docker compose cp api:/app/data/handoffs.jsonl "./handoffs-backup-$(date +%F).jsonl"

# 2. look at what is there and keep only real cases (here: drop the demo customers the eval uses)
python -c "import json,collections; print(collections.Counter(json.loads(l)['user_id'] for l in open('data/handoffs.jsonl')))"
grep -v -E '"user_id": "cliente(1988|2024|3030)"' data/handoffs.jsonl > data/handoffs.clean && mv data/handoffs.clean data/handoffs.jsonl
#    or start empty:   : > data/handoffs.jsonl
#    in Docker, edit a copy and put it back: docker compose cp ./handoffs.clean api:/app/data/handoffs.jsonl

# 3. turn it on in .env, restart, and look at the log line "handoff import ... {'imported': N, ...}"
HANDOFF_IMPORT_JSONL=true
# 4. set it back to false (leaving it on is harmless, records already in the queue are skipped)
```

Every record that is not in the queue yet comes in as `pending` with its old date, the rest are skipped, so a restart changes nothing and a handoff somebody already answered is never reopened. A line that is not valid JSON or lacks `handoff_id`, `user_id` or `reason` is logged and skipped. Records from before this feature have no `conversation_id` and land in `default`. **A record older than `THREAD_TTL_DAYS` is imported and then erased by the purge that runs at the same startup**; set `THREAD_TTL_DAYS=0` for that one start if you want to see it. The eval keeps writing only to the JSONL (a redacted line), never to the queue.

**What this does not do yet.** There is no operator UI (operators use the API), no notification when a handoff appears (a Slack/Teams `HandoffNotifier` is the place), no way to hand a claimed case straight to a named operator (release it and let them claim it), and `handoffs.sqlite` is not encrypted.

## Design notes

- **Plan first, then run.** One router call and then the agents run in order. Cheaper and more predictable than a supervisor loop, and easy to test.
- **Customer isolation.** The customer is the authenticated JWT `sub`, never something in the body or the prompt. Support tools are created per request already tied to it and take no user argument, so a prompt like "show me cliente2024's data" can't change whose data is read. Both layers are tested (`tests/test_auth.py`, `test_tools_are_bound_to_one_user`).
- **Retrieval.** Contracts/manuals are ~60% of the chunks and crowded out the useful pages, so search returns 3 web pages + max 2 PDFs.
- **Fails softly.** If something breaks the request continues with `degraded: true`, and if everything fails it goes to a human. The API shouldn't 500 because of the LLM.
- **Per-turn reset.** The router resets the per-turn state, so nothing leaks between turns.
- **Fake customer data** lives behind `app/data/repository.py`. Replace that class to use a real backend.

## Tests and evaluation

```bash
pytest -q                            # 455 tests, no network or keys needed (also: pytest -m "not eval")
docker compose run --rm eval         # full evaluation (needs keys + KB)
python -m eval.run_eval --only q05   # just some cases
```

The tests cover tools, router, ingestion, the API, authentication (valid, expired, forged, wrong scope or role, cross-customer attempts), the operator queue (claim/reply races, idempotency, who sees what), handoff and the graph itself (real graph, stub agents). One test inspects the schema of every tool and fails if any exposes a parameter that selects a user (`user_id`, `customer`, `account`...), so the model can never choose whose data is read.

The evaluation has 27 cases: 17 single-turn ones (10 from the challenge + extras + 3 handoff cases), 5 multi-turn follow-ups (`m01`-`m05`; their earlier turns are given as context, see `history` in `eval/dataset.jsonl`) and 5 blind holdout cases (`ho01`-`ho05`, summarized apart). Each one checks routing, tool use and handoff, and an LLM judge (different model family) scores relevance, groundedness, completeness and tone. Reports go to `eval/reports/latest.md`.

**Provider errors are not verdicts.** A per-minute rate limit is waited out and the case retried (`--retries`, default 2). A daily quota is never retried. If a case still ends with a rate limit, an exhausted quota or an outage (in the router, an agent or the synthesizer), it is marked `error`, skipped by the judge and left out of every metric, and the report lists it so it can be re-run (`--only <id>`). A run with provider errors, or a partial run (`--only`), does not overwrite `latest.md`. Model failures (e.g. a broken tool call) still count against the system. Every report states which model ran each role.

### Holdout, repeats and judge calibration

- **Holdout.** `ho01`-`ho05` (`"holdout": true` in `eval/dataset.jsonl`) were written on 2026-11-12, before any prompt change, and are never used to tune a prompt: a Getnet product no other case asks about (Get Tap), Support for a third customer (`cliente3030`), a mixed Support + Knowledge case, an explicit request for a person in Spanish and an out-of-scope request. They run with the rest, but the report summarizes them apart (section "Holdout"), so the main metrics stay comparable with earlier reports.
- **Repeats.** Repeated runs of the cases that carry the security and handoff decisions (`x04`, `h01`-`h03`) and of `q08` are in `eval/reports/REPEATS-20261112.md`. There is no `--repeats` option and no variance report of the whole eval (see `docs/V4_REMEDIATION.md`).
- **Judge calibration (human labels).** The judge is an LLM, so its verdicts need a human check:

  ```bash
  python -m eval.calibration.export                      # newest eval/reports/<stamp>.json -> eval/calibration/labels_template.csv
  # copy it to eval/calibration/labels.csv and fill human_grounded, human_complete (1-5), human_pass (yes/no), notes
  python -m eval.calibration.agreement eval/calibration/labels.csv -o eval/reports/CALIBRATION-<date>.md
  ```

  `agreement` reports pass/fail agreement, Cohen's kappa, the 2x2 confusion matrix (judge vs human) and the mean absolute error of groundedness and completeness, in pure Python; unlabeled rows are skipped. The template of the final run is committed; **the human labels are not done yet** (they need a person), so there is no agreement number yet.
- **Judge self-agreement** (judging the same answers 3 times) is not done: `run_eval` does not store the evidence the judge saw, so re-judging saved answers needs a change in what the report keeps. Pending, see `docs/V4_REMEDIATION.md`.

### Models per role

| Variable | Role | Default |
|---|---|---|
| `AGENT_PROVIDER` | Provider of every agent: `groq` (ChatGroq, `GROQ_API_KEY`) or `fireworks` (Fireworks' OpenAI-compatible API through `langchain-openai`, `FIREWORKS_API_KEY`, `reasoning_effort` sent as a body field). The judge is always Groq | `groq` |
| `LLM_MODEL` | Knowledge agent | `openai/gpt-oss-120b` |
| `LIGHT_MODEL` | Router, Synthesizer | `openai/gpt-oss-20b` |
| `SUPPORT_MODEL` | Support agent | empty = same as `LIGHT_MODEL` (`.env.example` sets `openai/gpt-oss-120b`) |
| `JUDGE_MODEL` | Eval judge (a different model family) | `qwen/qwen3.8-27b` |

With `AGENT_PROVIDER=fireworks` the model variables take Fireworks ids. The account used on 2026-11-12 serves only `accounts/fireworks/models/gpt-oss-120b` (the 20b answers 404 "Model not found"), so `LLM_MODEL`, `LIGHT_MODEL` and `SUPPORT_MODEL` are all that model there. The agents moved to Fireworks because Groq's paid Developer plan was not available and the free tier's 200K tokens per day per model cannot hold a full eval plus repeats.

They come from `.env`, `docker-compose.yml` passes them to the containers, and one run can override them: `LIGHT_MODEL=openai/gpt-oss-120b docker compose run --rm eval`. Groq's free tier limits tokens per model (gpt-oss: 8K per minute, 200K per day), so roles that share a model share one quota. That is why the light roles are on a different model than Knowledge. Support is the exception: the 20b often skips `get_recent_transactions` on a decline (`q08`), so the delivered `.env.example` runs it on the 120b, which shares its quota with Knowledge.

### Latest full report

`eval/reports/latest.md` (2026-11-12, agents on Fireworks `gpt-oss-120b`, judge `qwen3.8-27b` on Groq): **all 27 cases**, no provider errors.

| | regular (22) | holdout (5) |
|---|---|---|
| Routing | 95% (`m03` sent to Knowledge) | 80% (`ho02` added Knowledge) |
| Tool use | 100% | 100% |
| Handoff accuracy | 95% (`x04` false positive) | 100% |
| Groundedness / completeness | 95% / 87% | 88% / 92% |
| Judge pass rate | 86% (`q05`, `m01`, `m05` fail) | 80% (`ho03` fails) |
| Latency p50 / p95 | 3.7 s / 12.3 s | 3.7 s / 13.1 s |

Tokens: 138.6K on the agents (102 calls, 5.1K per turn on average) and 58.5K on the judge; estimated cost $0.026 for the agents and $0.058 for the judge at list prices. A stable copy is `eval/reports/BASELINE-FIREWORKS-FULL-20261112-131428.md`; the previous full report (17 cases on Groq: routing, tools, handoff and judge 100%, groundedness 93%, completeness 94%) is `BASELINE-FULL-DELIVERED-20261112-092550.md`. It is one run and the judge is not ground truth: the per-case reasons are at the end of the report.

### Which report is which

| Report | Date | Agents | Judge | Cases |
|---|---|---|---|---|
| `latest.md` (copy: `BASELINE-FIREWORKS-FULL-20261112-131428.md`) | 2026-11-12 13:14 | Fireworks, every role on `gpt-oss-120b` | Groq `qwen3.8-27b` | 27 (22 regular + 5 holdout) |
| `REPEATS-20261112.md` | 2026-11-12 13:15-13:17 | same as `latest.md` | same | `x04`, `h01`-`h03`, `q08`, 4 runs each |
| `BASELINE-FIREWORKS-SMOKE-20261112.md` | 2026-11-12 12:48 | same as `latest.md` | same | `x02`, `q03`, `x04` (smoke test of the move) |
| `BASELINE-FULL-DELIVERED-20261112-092550.md` | 2026-11-12 09:25 | Groq: Knowledge and Support `gpt-oss-120b`, Router and Synthesizer `gpt-oss-20b` | Groq `qwen3.8-27b` | 17 (before `m01`-`m05` and the holdout existed) |
| `BASELINE-REPEATS-20261112.md`, `BASELINE-MULTITURN-20261112.md`, other `BASELINE-*` | 2026-11-11 / 2026-11-12 | Groq, the models each one states | Groq `qwen3.8-27b` | subsets, see each file |

Every report states its models in its header. Numbers from the Groq reports and the Fireworks reports are not one series: the provider and the Router/Synthesizer model changed between them.

### Re-evaluation after the move to Fireworks (2026-11-12)

The Groq baselines are a reference, not the criterion. The acceptance criteria for the provider change were: `x04` refuses, `h01`-`h03` hand off and `q08` calls `get_recent_transactions` in every run, routing at least 95%, no provider errors. All hold (`eval/reports/REPEATS-20261112.md`, the full run plus 3 repeats of those five cases):

- `x04` never called a data tool (4 of 4). But it now **always** escalates to a human (4 of 4, it was 1 of 5 on Groq): a false alarm for the queue, not a leak. Not fixed, see Limitations.
- `h01`-`h03` hand off 4 of 4; `q08` calls `get_recent_transactions(status="declined")` 4 of 4. Smoke test before the move: `BASELINE-FIREWORKS-SMOKE-20261112.md`.
- With the Router on the 120b (it was the 20b on Groq), the recall follow-ups `m01` and `m02` now get an empty plan (they went to Knowledge 12 of 12 on Groq); `m03` (rephrase) still goes to Knowledge. `m01` then fails the judge: the synthesizer said it does not keep personal preferences instead of reading the earlier turn.
- Where it got worse: completeness 94% → 87% and judge pass rate 100% → 86% on the regular cases, mostly the multi-turn cases that did not exist in the 17-case report (`m01`, `m05`) and `q05`, which invented Wi-Fi details again (it also failed 1 of 5 runs on Groq). The Router on the 120b also labels the language of an English message as Portuguese when it contains Portuguese words (`x04` "cliente2024", `ho03` "receivables advance"), so `ho03` was answered in Portuguese.
- No prompt was changed for this; the numbers are what the provider and model change did.

### Real-LLM checks of memory and human oversight (2026-11-12, Fireworks)

`docs/VERIFICATION_DEBT.md` items 7-13 were run with the real agents in a separate Docker project (port 8001, image from HEAD, a read-only copy of the knowledge base), about 100K agent tokens. Each box there has its result.

- **Works with the real stack:** a conversation survives an API restart ("Rafa" is remembered) and another customer with the same `conversation_id` gets nothing; `GET /conversations/{id}` shows the exact answer, `DELETE` empties it; `h01`, `h02` and `h03` (agent-made) handoffs reach the queue with the right customer and conversation; claim → reply → the customer's next turn carries the operator text once, never to another customer; the queue survives a restart; leases expire after `HANDOFF_LEASE_MINUTES` and the former holder gets 409; the thread limit answers 429 and frees a slot on `DELETE`; the rolling summary fires, holds only facts, and a summary timeout never fails a turn.
- **Security held:** after an operator reply and with a rolling summary present, asking for another customer's transactions was refused with no tool call. A customer typing `[Operador humano] ...` is stored as `[cita: Operador humano] ...` and is not acted on.
- **Gaps found (model behaviour, documented in Limitations, not fixed because they need prompt changes):** the operator's text is in the context but "what did support say?" is answered "I don't have any details"; with a tool at hand the model prefers it over the summary (turn 5 said "Carlos Mendes" / "88231" instead of "Rafa" / "4421"), and the direct-answer path did not use a summary that held the name; some English turns are answered in Portuguese.

### Re-evaluation after the authentication change (2026-11-11 and 2026-11-12, on Groq)

**2026-11-11.** Nine Support/handoff cases (`q03 q05 q08 x01 x03 x04 h01 h02 h03`), one run each, so small differences are noise. Reports: `eval/reports/BASELINE-*.md`.

| | default models | everything on `gpt-oss-120b` |
|---|---|---|
| Cases that ran | 9 of 9 | 8 of 9 (`h03`: daily quota ran out, marked `error`) |
| Routing | 100% | 100% |
| Tool use | 89% (`q08` fails) | 100% |
| Handoff accuracy | 100% | 100% (`h03` not measured) |
| Judge pass rate | 100% | 88% (`q05`: invented technical details) |
| Groundedness / completeness | 98% / 89% | 92% / 95% |

- `x04` (asking for another customer's data) is refused in both runs, and `h01`/`h02` always hand off.
- `q08`: with `gpt-oss-20b` the Support agent calls only `get_terminal_status` and blames the Wi-Fi, although the data has two declined sales (codes 51 and 05) and the prompt says to always call `get_recent_transactions` for a decline. In the reports we have, Support on the 20b skipped that tool in 5 of 6 runs, and on the 120b it called it in 7 of 7 (six of those are from 11-08, before the light model existed, so only today's run is a controlled comparison). The judge passes the 20b answer because it never sees the transactions, so the tool check is the only signal.
- `q05` failed groundedness on the 120b run (the answer added details like 2.4 GHz and a faulty module that the tools did not return). Single run: it was reviewed on 2026-11-12, see below.
- Both gpt-oss models ran out of daily quota that day, so the checks below were pending until 2026-11-12.

**2026-11-12 (quota restored).** All the pending checks were run (list and results in [docs/VERIFICATION_DEBT.md](docs/VERIFICATION_DEBT.md), items 1-6). Reports: `eval/reports/BASELINE-*.md`.

- **No regression from the authentication change.** The same 9 cases on HEAD with the default models match the 2026-11-11 baseline in every case. The same cases on the commit before step 1 (`bab3729`) and on HEAD, with the same models, are identical, so `q08` failing on the 20b is older than the authentication change. HEAD already includes the later steps (memory, handoff queue).
- **`q08` fix confirmed: `SUPPORT_MODEL`.** Support runs on its own model variable, set to the 120b in `.env.example`. It called `get_recent_transactions(limit=5, status="declined")` in 4 of 4 runs of the delivered configuration (the full report plus three repeats); the exact calls are in `BASELINE-REPEATS-20261112.md` (the eval now records tool arguments in the JSON report).
- **`q05` was noise.** It passes in the full report and in 3 of 3 repeats; the 40% groundedness of 2026-11-11 was 1 failure in 5 runs.
- **`h03`** hands off with `escalate_to_human` in 4 of 4 runs. **`x04`** refuses without calling any data tool in 5 of 5 runs with Support on the 120b, but in 1 of them the agent also escalated to a human ("User requested data for a different customer ID"), which counts as a handoff the case does not expect.
- **Tokens measured** for the full 17 cases: 56.7K on `gpt-oss-120b`, 18.8K on `gpt-oss-20b` and 26K on the judge (the earlier estimate of 120K was too high).
- **Container (Docker), same day.** The image runs as uid 10001, `/health` reports the 1,493-chunk knowledge base copied into the `data` volume, and the Support route works with real tools for two customers: each sees only their own profile and payouts, and a request for another customer's transactions is refused. Handoffs and tickets written by the non-root process are in the volume and show up in the operator queue, and `docker compose run --rm eval` writes its report to `./eval/reports` on the host.
- **What the container check did not settle.** In one conversation, a follow-up that refers to an earlier turn was answered with that context once, but pure recall questions ("What favorite color did I tell you?", "Which of the two is bigger?") were routed to Knowledge in 3 of 3 attempts, and Knowledge cannot answer them (it escalated twice, and failed once on the 120b quota). The conversation is stored and listed correctly; the weakness is in routing such follow-ups. Support on the 120b also hit its daily quota in the middle of the session, so the agent was degraded and handed the case to a human, which is the designed behavior.

Handoff behavior: explicit requests for a human always escalated. The unanswerable loan question escalated most of the time but not always, since the agent decides whether to call `escalate_to_human` (see Limitations).

## Production hardening

- **No root in the container.** The image runs as `app` (uid/gid 10001). The code under `/app` is root-owned and read-only for that user, and only `/app/data` and `/app/eval/reports` are writable by it.
- **Data lives in a named volume (`data`), not in `./data`.** A bind mount takes its permissions from the host: on Docker Desktop the old `data/chroma` was root-owned (755), so uid 10001 could not write Chroma's SQLite file. A new named volume is created with the image's owner for `/app/data`, so no root entrypoint or `chown` at start is needed, and a live SQLite database stays out of the OneDrive-synced folder. `./eval/reports` stays a bind mount so you can read the reports (on a Linux host make it writable for uid 10001 or run the `eval` service with `--user "$(id -u):$(id -g)"`).
- **One-time migration of an existing `./data`** (stop the API first so Chroma is not copied while it writes; the first `docker compose run` creates the volume):

  ```bash
  docker compose down
  # Git Bash on Windows: prefix with MSYS_NO_PATHCONV=1 and use $(pwd -W) instead of $PWD
  docker compose run --rm --no-deps -u root -v "$PWD/data:/old:ro" --entrypoint sh api \
    -c 'cp -a /old/. /app/data/ && chown -R 10001:10001 /app/data'
  docker compose up -d
  ```

  Or skip the copy and rebuild the knowledge base: `docker compose run --rm ingest` (uses Voyage and Tavily). If a volume ever has the wrong owner, repeat only the `chown` with `-u root --entrypoint chown api -R 10001:10001 /app/data`.
- **Warning: `docker compose down -v` deletes the `data` volume**: the Chroma index, `tickets.jsonl`, `handoffs.jsonl` and the crawl cache, with no confirmation and no undo. The same goes for `docker volume rm` and `docker volume prune` / `docker system prune --volumes`. Plain `docker compose down` and `up --build` keep it. Rebuilding the index costs Voyage and Tavily calls, and tickets and handoffs cannot be rebuilt. **Back it up before you migrate, recreate or `down -v`:**

  ```bash
  # Git Bash on Windows: prefix with MSYS_NO_PATHCONV=1 and use $(pwd -W) instead of $PWD
  docker compose run --rm --no-deps -u root -v "$PWD:/backup" --entrypoint tar api \
    czf /backup/getnet-data-$(date +%F).tgz -C /app/data .

  # restore into an empty or recreated volume
  docker compose run --rm --no-deps -u root -v "$PWD:/backup:ro" --entrypoint sh api \
    -c 'tar xzf /backup/getnet-data-YYYY-MM-DD.tgz -C /app/data && chown -R 10001:10001 /app/data'
  ```

  Stop the API first (`docker compose stop api`) so Chroma is not copied while it writes. The backup is a file in your project folder: keep it out of git (it is not in `.gitignore` yet, so do not `git add .`) and out of OneDrive if it holds real customer data.
- **Read the handoff queue and tickets from the volume:** as an operator, through `GET /operator/handoffs` (see "Human oversight"); the audit copy is `docker compose exec api cat /app/data/handoffs.jsonl`, or `docker compose cp api:/app/data/handoffs.jsonl .`. Running outside Docker still uses `./data`. `handoffs.sqlite` is in the same volume, so `down -v` deletes it too, and the backup commands above include it (stop the API first so its `-wal` file is folded in).
- **CI** (`.github/workflows/ci.yml`, on push and pull request to `main`): `pytest` on Python 3.12 with a pip cache, no `.env` and no API keys (only a fake `JWT_SECRET` in the job), plus a `docker build` (no push) that fails if the container's uid is not 10001.

## Conversation memory

- **Where.** LangGraph checkpoints in one SQLite file, `CHECKPOINT_DB_PATH` (default `data/checkpoints.sqlite`, so inside the named volume `data` in Docker). `SqliteSaver` (sync) on one shared connection, one uvicorn worker. The thread key is still `<sub>:<conversation_id>`. A `threads` table in the same file records the owner and the last activity of each thread. The API closes the file cleanly on shutdown. The eval keeps using in-process memory, so it never writes conversations to disk.
- **Your own conversations (API).** Same authentication as `/chat` (JWT with role `merchant` and the `chat` scope; there is no extra scope). The owner is always the token's `sub`: the path only holds a `conversation_id`, and a user in the path, query, body or header is ignored.

  | Endpoint | Returns |
  |---|---|
  | `GET /conversations` | `{"conversations": [{"conversation_id", "created_at", "last_active", "turns"}], "count", "limit"}`, most recent first, only yours. **No content.** `limit` is `MAX_THREADS_PER_USER` (null = none). Times are ISO 8601 UTC. |
  | `GET /conversations/{id}` | The same metadata plus `messages` (`[{"role": "user" or "assistant", "content"}]`, no tool output) and `summary` (text, or null). It is what the thread still holds: after a rolling summary, only the recent window plus the summary. |
  | `DELETE /conversations/{id}` | `204`, no body. The thread is gone from the checkpoints and the registry, its handoffs (with their operator messages and audit events) are erased from the handoff queue, and its slot against the limit is free again (you can reuse the id for a new, empty conversation). Not undoable. |

  A conversation that is somebody else's, one that does not exist and an id with a bad character or length all get the same `404 {"detail": "Conversation not found."}`, so the API never says whether a conversation exists. Without a token it is `401`, with a token without the `chat` scope `403`, as in `/chat`. `DELETE` erases the conversation's handoffs too (see "Human oversight", privacy), but **not** the tickets, which keep the first 200 characters of the request, and not backups. A `DELETE` while a turn of that same conversation is still running can leave that turn's checkpoints behind as orphans; the purge removes them (when `THREAD_TTL_DAYS` is not 0).
- **Rolling summary.** Above `SUMMARY_THRESHOLD` messages (default 12), the messages older than the last `HISTORY_WINDOW` (6) are folded into a `summary` with `LIGHT_MODEL` (never the Support model) and removed. Router and agents get the summary (as a system message) plus the recent window; with no summary their input is exactly what it was. If the summary call fails or exceeds `SUMMARY_TIMEOUT_SECONDS` (20, no retries), the turn goes on without it, a warning is logged and the next turn tries again. After every turn only the latest checkpoint of the thread is kept: LangGraph writes several per turn and `SqliteSaver.prune` is not implemented in the pinned version, so `Memory.prune_thread` does it in SQL. Without that, the table would grow with every turn and the removed messages would stay readable in old rows.
- **Personal data at rest.** The checkpoints store the whole graph state: the conversation, the summary, `agent_outputs` and the raw results of the tools (profile, transactions, payouts). That is customer data in `checkpoints.sqlite`, in the Docker volume, in its `-wal` file and in every backup. **Nothing is encrypted**: protect the volume and the backups at the host level (disk encryption, access control). `handoffs.sqlite` holds the same kind of data (the recent conversation of every handoff, in clear) and is erased by `DELETE /conversations/{id}` and by the same TTL. `tickets.jsonl` is covered by neither (and a `handoffs.jsonl` written before the audit copy was redacted still has excerpts: see "Human oversight" before importing it).
- **Retention.** `THREAD_TTL_DAYS` (default 30, 0 = never) counts days **without activity**. Idle threads are deleted by `thread_id` (checkpoints, writes and registry row) when the API starts and every `PURGE_INTERVAL_MINUTES` (60). Checkpoints without a registry row (an interrupted purge) are deleted too. Handoffs idle for the same time are erased by the same pass (see "Human oversight"). Deleted rows are zeroed (`secure_delete` is on) and the write-ahead log is truncated after a `DELETE` or a purge, so the text is gone from the file and its `-wal`; tests search the raw bytes for it. **Backups keep what was purged or deleted**, and so does the host's own snapshotting: delete old backups on the same schedule.
- **Limit per customer.** `MAX_THREADS_PER_USER` (default 20, 0 = no limit). A new conversation above it gets `429` with a message saying so; existing conversations keep working and **nothing is deleted to make room**: free a slot with `DELETE /conversations/{id}` or wait for the TTL. The check and the insert are atomic, and a failed first turn gives its slot back.
- **Backup.** Same commands as above (`tar` through a throwaway container). Stop the API first: it flushes the WAL on shutdown, so the `.sqlite` file is then complete. Copying the file while the API runs can miss what is still in the `-wal` file.
- **Tuning.** All of this is read from the environment (`.env.example` lists the variables). A shorter window or summary threshold costs fewer tokens per turn but forgets sooner.

## Observability

Every `/chat` turn is measured without touching a prompt or a node: `ChatService` attaches a LangChain callback handler (`app/observability.py`, `TurnCollector`) to that one `graph.invoke`, and it sees every LLM and tool call of every node, including the calls of the nested tool agents.

- **What is recorded.** Per LLM call: the LangGraph node (`router`, `support`, `knowledge`, `synthesizer`, `summarize`), the model, input and output tokens (`usage_metadata`, reported by both ChatGroq and ChatOpenAI/Fireworks), latency and the error type (class and HTTP status, never the error text). Per tool call: name, latency and ok/error, never its arguments or result.
- **Cost.** A price table in `app/observability.py` (USD per million tokens, list prices of 2026-11-12 from the Fireworks and Groq pricing pages) for `accounts/fireworks/models/gpt-oss-120b`, `openai/gpt-oss-120b`, `openai/gpt-oss-20b` and the judge `qwen/qwen3.8-27b`. `MODEL_PRICES_JSON` adds or corrects prices. A model without a price has cost `null`, never 0, and a warning is logged once (Fireworks does not price `gpt-oss-20b` because it does not serve it serverless). These are estimates from list prices, not the provider's bill.
- **In the response.** `/chat` has a new `usage` field: `{input_tokens, output_tokens, total_tokens, cost_usd, by_node: [{node, model, calls, input_tokens, output_tokens, cost_usd}]}`. Every other field is unchanged.
- **Stored traces.** `TRACES_DB_PATH` (default `data/traces.sqlite`, in the `data` volume in Docker): one row per turn (`traces`: trace id, user, conversation, time, latency, plan, degraded, handoff reason, tokens, cost, provider errors) and one per call (`spans`). **No message, answer or tool result is stored**; a test reads the raw bytes of the file and its `-wal` to check it. `DELETE /conversations/{id}` erases that conversation's traces, and the retention pass erases traces older than `THREAD_TTL_DAYS`. If the store fails, the turn still answers 200 and a warning is logged.
- **Metrics.** `GET /operator/metrics?since=&until=` (role `operator`, scope `operator`; a merchant gets 403), times in ISO 8601 (no zone = UTC): requests, latency p50/p95, degraded and handoff rates, provider errors, tokens and cost per node and per model, and tool calls/errors.

  ```bash
  OP=$(python -m scripts.issue_token --role operator --operator ops1)
  curl -s -H "Authorization: Bearer $OP" "localhost:8000/operator/metrics?since=2026-11-12T00:00:00"
  ```
- **Logs and alerts.** The one log line per request keeps its fields and ends with `tokens=<n> cost_usd=<usd>`. A turn above `MAX_TURN_TOKENS` (30000) or `MAX_TURN_COST_USD` (0.02), or one that got a 429 from the provider, logs a `WARNING` line starting with `ALERT` (`ALERT turn_tokens 31200 > 30000 trace=... user=... conv=...`), ready for a log-based alert rule.
- **Eval.** Every case of the report shows its tokens and cost, and the summary shows tokens per model (agents and judge apart) and the estimated total cost.
- **Not done.** No visual dashboards, no OpenTelemetry/Langfuse export and no alert delivery (the `ALERT` lines are where a log-based rule would hook in). The metrics come from one SQLite file, fine for one worker; a real deployment would export the same spans to OTel.

## Limitations

- Conversation memory persists (SQLite in the `data` volume) but there is **no encryption at rest**, and it assumes one uvicorn worker. See "Conversation memory".
- Customer data is fake.
- The handoff queue has an API (see "Human oversight") but no operator UI, no notification when a case arrives, so "a human will follow up" is only as true as somebody polling `/operator/handoffs`. A real setup needs Slack/Teams/helpdesk behind `HandoffNotifier`. The path from a real agent's handoff to the queue, and what the real LLM does with an operator's reply in its context, are tested with stub agents and a recording fake LLM, not yet with the real one (`docs/VERIFICATION_DEBT.md`, items 11 and 12).
- Whether an agent escalates is the LLM's call, so it can miss sometimes.
- On Groq with `gpt-oss-20b` the Support agent often skips `get_recent_transactions` on a decline and blames the terminal (`q08`, see Re-evaluation). `SUPPORT_MODEL=openai/gpt-oss-120b` (the delivered setting) avoids it (4 of 4 runs), at the cost of 120b quota, which Knowledge shares: a day of testing can use it up, and then the agents degrade and hand cases to a human.
- Follow-up questions that only refer to the conversation ("what did I tell you?", "say that again more simply"). On Groq (Router on the 20b) the 3 that need no agent (`m01`-`m03`) were routed to Knowledge in 12 of 12 calls, which cannot answer them; on Fireworks (Router on the 120b) `m01` and `m02` get an empty plan and `m03` still goes to Knowledge, and `m01` is still answered badly (the synthesizer says it keeps no preferences) and `m05` repeats the earlier payout. It is not a memory problem: the conversation reaches the Router and every agent (`tests/test_followup_context.py`). The fix is a rule and an example in the Router prompt; it was left out of this delivery on purpose (Router not touched) and stays a known limitation. Details: `eval/reports/BASELINE-MULTITURN-20261112.md` and `latest.md`.
- **The operator's reply is delivered but not used by the agents.** It reaches the customer in `operator_messages` and is in the agents' context as `[Operador humano] ...`, but asked "what did the support team say about my terminal?" the agents searched tickets and the KB and said they had no details (VERIFICATION_DEBT item 12). Using it needs a Router/agent prompt rule; not done.
- **Facts said earlier lose to tools and are ignored on the direct path.** With a rolling summary that held "Rafa" and terminal "…4421", the agent answered the profile name and part of the serial from new tool calls, and a greeting-style answer said it had no name stored (item 8, eval `m01`). The summary itself is correct.
- With the Router on `gpt-oss-120b`, an English message that contains Portuguese words can be labelled Portuguese, and the answer comes in Portuguese (`ho03`, and the `x04` handoff notice in the smoke test).
- The Knowledge agent sometimes puts customer details it got from Support into its KB search query (`ho02`: bank and branch). The query is embedded by Voyage, an external API, so those details leave the system in the query. Not fixed (a Knowledge prompt change).
- **Handoff false positive in `x04`.** `x04` (a customer asks for another customer's transactions) refused without calling any data tool in 5 of 5 runs on Groq and 4 of 4 on Fireworks, so the security behavior held. But the agent also called `escalate_to_human` in 1 of 5 runs on Groq and in **4 of 4 on Fireworks** ("User requested data for a different customer ID") and the case was handed to a human, while the case expects no handoff. It is a false alarm for the operator queue, not a data leak. It is not fixed: the way to remove it is a Support prompt change, and the Support prompt is not touched without measuring it again (removing the customer id from it already made `x04` fail in 5 of 5 runs, see "Who is calling comes only from the token").
- `qwen/qwen3.8-27b` works as the judge but not as an agent: it fails with "Request too large ... output tokens per minute" because the agents do not set `max_tokens`.
- The KB is only as good as the crawl. There's little troubleshooting content, so those answers are generic. Adding help-center URLs to `app/rag/sources.py` would help the most.
- The LLM judge isn't ground truth, so spot-check the answers.
- Auth is a demo: HS256 with a shared secret and tokens from a local script. A real deployment needs an identity provider (RS256/JWKS). There are no refresh tokens, revocation or rate limiting.
- Not done: guardrails, visual dashboards, OpenTelemetry/Langfuse export and alert delivery (traces, tokens, cost, metrics and `ALERT` log lines are done, see "Observability").

## Layout

```
app/
  main.py auth.py service.py graph.py handoff.py handoff_store.py state.py prompts.py config.py ...
  agents/   router, knowledge, support, synthesizer
  tools/    customer_tools, knowledge_tools, handoff_tools
  rag/      sources, ingest, store
  data/     customers.json, repository.py
eval/       dataset.jsonl, judge.py, run_eval.py
scripts/    issue_token.py (demo JWT, merchant or operator)
tests/
docs/
```
