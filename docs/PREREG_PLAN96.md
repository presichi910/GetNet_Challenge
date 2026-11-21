# Pre-registration, plan 9.6

Written and committed together with `eval/holdout_v3.jsonl`, BEFORE any behavior change of plan 9.6 (blocks M1, B, A and C). Every decision below is
taken by the rule written here, applied by code to the numbers the generated blocks produce. The rules are copied literally from `docs/PLAN_96.md`
(local, not versioned); the quoted text is not edited.

## Holdout v3

- File: `eval/holdout_v3.jsonl`, 20 cases (`hv3_01` to `hv3_20`), all marked `"holdout": true`: they only measure, never tune a model, prompt or threshold.
- SHA-256 (of the file with line endings normalized to LF): `254f79c07d7c1240e93e195aa6280ae17e380493b2910fc6b3492146da0d27ac`
- `tests/test_prereg.py` fails if the file no longer has this hash.

## Fixed hosts (OpenRouter, `allow_fallbacks=false`)

| Role | Model | Pinned upstream | Fallbacks |
|---|---|---|---|
| Agents (router, support, observe, summarizer; baseline of knowledge and synthesizer) | `openai/gpt-oss-120b` | `deepinfra`, quantization `bf16` | `allow_fallbacks=false` |
| Judge | `qwen/qwen3.8-27b` | `alibaba` | `allow_fallbacks=false` |

A call the pinned host cannot serve is an error that is counted, never silently served by another host.

## B1 (RAG adoption rule)

> Regla de adopción (prerregistrada): se adopta el modo con mayor recall@5 en la mitad de ajuste **sin** bajar hit@1 en la mitad de holdout; empate → `dense` (más simple).

## C1 (choice of the generation model)

> Diseño: mismo commit (con B y A1 hechos), 27 regulares, **1 repetición** cada uno; los dos mejores, una **segunda** repetición. Juez igual para todos.

> Regla prerregistrada (en `PREREG_PLAN96.md`):
> 1. Mayor número de pass del juez sobre las filas juzgadas (suma de repeticiones de los dos finalistas).
> 2. Empate (±1): menos afirmaciones sin respaldo (conteo del juez/verifier).
> 3. Empate: menor costo por case-run.
> 4. Un candidato que baja routing, tools o handoff respecto a la base queda descartado.
> 5. Sonnet 5.5 solo se adopta si gana por ≥ 3 pass **y** la proyección del cost guard para C3 deja el total ≤ US$9.

## C1b (verifier mini A/B)

> **Disparador prerregistrado** ("resultados muy buenos" del ganador de C1, medidos contra la base gpt-oss en los 27 regulares): pass del ganador ≥ base + 3 **y** afirmaciones sin respaldo del ganador ≤ 70 % de las de la base. Si no se cumple, el verifier se queda en gpt-oss y C1b no se corre.

> Si se cumple: 27 regulares × 1 rep, mismo commit y mismo modelo de generación, verifier = gpt-oss vs verifier = ganador (`VERIFIER_MODEL` nuevo setting, mismo patrón que M1). **Se adopta** si reduce las afirmaciones sin respaldo ≥ 25 %, no baja el pass, no baja completeness media más de 0.1 y el cost guard sigue proyectando ≤ US$9. Tiene que decidirse **antes del congelado del sábado 09:00**; si no da tiempo, no se cambia.

## C3 (default of the replan)

> Regla prerregistrada del default:
> - **ON por defecto** si, sobre los pares (caso, repetición): diferencia de pass ON−OFF con IC 95 % bootstrap (por caso, 10 000 remuestreos) con límite inferior > 0, **o** McNemar unilateral p < 0.10; **y** ningún grupo (regular, holdout original, holdout v3) empeora en más de 1 caso-corrida neto; **y** costo medio +≤ 35 % y p95 de latencia +≤ 6 s.
> - Si no se cumple → A2 (puerta) con sus cifras, o OFF con la explicación. El resultado se publica sea cual sea.

## C4 (sampling of the rows a human labels)

> Con `eval/calibration/sample.py` y una semilla escrita en el prerregistro **antes** de la corrida final: muestra estratificada de 60 filas de la repetición 1 del brazo default (regulares, holdout original, holdout v3; incluye pass y fail del juez sin mirarlos). Exporta con `export.py` una hoja **ciega** (sin columna de juez) `labels_v4_blind.csv`, y otra de 20 filas solapadas para el segundo etiquetador (`labels_v4_rater2_blind.csv`). La clave va aparte (`labels_v4_key.csv`).

**Seed, written here before the final run: `20261123`** (the delivery date). The sample is drawn with `random.Random(20261123)`, stratified by group
(regular, original holdout, holdout v3) over repetition 1 of the default arm, from case ids and their kind alone: it never reads a judge verdict, a
score or a label.

## Enmienda 1 (19-nov, antes de C1)

Written and committed BEFORE any measurement it governs. It changes how the host of `openai/gpt-oss-120b` is chosen; it does not touch the judge's host
(`alibaba`) nor any other rule above. The "Fixed hosts" row of the agents (`deepinfra`, `bf16`) holds until the rule below picks another host.

**Reason: the latency of the first smoke on that host.** Run `eval/reports/20261119-212738.json` (versioned in `acb44f1`; commit `4e53782`, clean tree, 3 regular
cases, no judge, 0 provider errors), against the five base repetitions of 2026-11-15 (`22423e2`, Fireworks):

| case | smoke on `deepinfra/bf16` | base repetitions (5) | smoke / median of the base |
|---|---|---|---|
| `q01` total latency | 84,590 ms | 4,986 to 9,942 ms, median 5,862 | 14.4 times |
| `q03` total latency | 23,246 ms | 3,247 to 5,632 ms, median 4,448 | 5.2 times |
| `m01` total latency | 5,392 ms (2 calls, 93 output tokens: 2,696 ms per call) | 1,283 to 1,798 ms (642 to 899 ms per call) | 3.4 times |

The work was the same: `q01` made 4 calls and 977 output tokens (base: 4 or 5 calls, 828 to 1,135 tokens), and its output rate was 11.5 tokens per second against 106 to 166
in the base. What the file cannot say: it has no timing per node, and the retries of the HTTP client (2, after a 60 s timeout, a 429 or a 5xx) do not reach the callbacks, so a
stall plus a retry may account for part of the 84.6 s of `q01` (`q03` and `m01` took under 60 s: a timeout cannot explain them, and a retry adds under a second). The measurement
below makes one attempt per call, with no client retries, so that a slow host and a retried stall show up apart. One run of three cases is not a measure of the host: it is the reason
to measure it.

**Rule (written before the measurement, copied literally from the owner's decision of 2026-11-19):**

> Candidatos: todos los hosts de openai/gpt-oss-120b en OpenRouter con cuantización bf16/fp16 o no declarada, que soporten tools y tengan uptime ≥ 99 %. Medición: 10 llamadas por host con un prompt de agente real (router y knowledge con tools) más q01 completo ×2. Se elige el host más barato cuyo p50 de latencia total esté dentro de 1.5× del más rápido y cuyas tools funcionen en el 100 % de las llamadas. Empate: el más rápido.

**Definitions that make the rule executable** (added with the amendment, before the measurement; the rule above is not edited):

1. **Candidates**: read at the start of the measurement from `GET /api/v1/models/openai/gpt-oss-120b/endpoints`, one candidate per endpoint tag. Quantization in `bf16`, `fp16` or
   `unknown` (not declared); `tools` among its `supported_parameters`; `uptime_last_30m` of 99 or more (a missing value does not meet it).
2. **The 10 calls per host**: 5 of the router and 5 of the knowledge agent, with the real `ROUTER_PROMPT` and `KNOWLEDGE_PROMPT` formatted as the nodes format them, one call for the
   message of each regular case `q01`, `q03`, `q06`, `q09` and `q10` (no holdout case). The router call asks for its `RoutingDecision` by function calling, as the router does; the knowledge call
   carries the three tool schemas of the agent (`search_getnet_kb`, `web_search`, `escalate_to_human`) with `tool_choice` auto, and the tools are not executed. The body is the one `app/llm.py`
   builds (the same `extra_body`), with `provider.order` set to the candidate's tag and `allow_fallbacks` false.
3. **Tools work** for a host if, in the 10 calls and in the `q01` runs, every call answers with the pinned host (the `provider` field of the response), no call errors or exceeds 120 s, every router
   call returns a function call whose arguments parse as a `RoutingDecision`, every knowledge call returns a tool call whose name is one of the three and whose arguments are valid JSON, and both
   `q01` runs end with the plan and the tool of the base (`knowledge`, `search_getnet_kb`).
4. **`q01` complete, twice**: `python -m eval.run_eval --only q01 --repeats 2 --no-judge` with `OPENROUTER_PROVIDER_ORDER` pinning the candidate. Its two total latencies are reported; they are not
   part of the p50.
5. **Latency** is the wall-clock time of the whole request (not the time to the first token); the **p50** is over the host's 10 calls. **The fastest** is the lowest p50 among the hosts whose
   tools work in 100 % of the calls (a fast host with broken tools does not set the bar).
6. **Cost** is the mean real `usage.cost` per call over the 10 calls. **A tie** is two costs within 5 % of each other: the faster p50 wins.
7. **One attempt per call**: the HTTP client makes no retries; a failure is data, and it counts against the host.
8. **Cap**: US$0.10 for the whole measurement, by the real cost the responses report. Hosts are measured from the lowest listed price up; when the next host could exceed the cap the measurement stops
   and the hosts left are listed as not measured.
9. **Applied by code**: `eval/host_latency.py` applies the rule and writes `eval/reports/HOST-LATENCY-<stamp>.md` (generated) with the table of every host and the winner, whatever it says. If no host
   meets the rule, `deepinfra/bf16` stays and the report says so. The default pinned host changes to the winner after the report, in its own commit.

### Enmienda 1a (19-nov, still before the measurement): a reference arm

When the candidate list was read (free, public endpoint, no call made to any model), `deepinfra/bf16`, the host pinned until now, did NOT meet the uptime filter of the rule (96.7 %
over the last 30 minutes; the same endpoint listed 100 % a few hours earlier), and neither did `groq` (96.7 %). By the rule `deepinfra/bf16` is therefore not a candidate today. To
tell whether the slowness of the first smoke belongs to that host or to our own retries, `deepinfra/bf16` is ALSO measured, in exactly the same way (10 calls, one attempt each,
`q01` twice), as a **reference**: it is listed apart in the report, it never enters the decision, it is never "the fastest" that sets the bar, and it cannot win. If it does meet the
filter at the time of the measurement it is simply a candidate, measured once. Its cost counts against the US$0.10 cap, and it is measured last.

### Result of amendment 1 (19-nov, after the measurement)

The rule was applied by code (`eval/host_latency.py`) to `eval/reports/HOST-LATENCY-20261119-214824.json` (the report is `.md`; versioned in `45c8a8e`; measured at commit `98f869f`, clean tree,
real cost US$0.0211 of the US$0.10 cap). **The host of `openai/gpt-oss-120b` is `groq`**: p50 of 450 ms over 10 calls (the fastest), tools working in all 10 calls and in both `q01` runs, US$0.00017 per call;
`sambanova` was within 1.5 times but dearer, `together` was 2.6 times the fastest. `dekallm/bf16`, `crusoe/bf16` and both `amazon-bedrock` endpoints answer the router's forced tool call with 404
("Filter by Tool Compatibility"), so their tools do not work. The reference `deepinfra/bf16` (not a candidate that day: 90 % uptime) had a p50 of 5,973 ms with one attempt per call and no client retries.
This result replaces the `deepinfra`, `bf16` row of "Fixed hosts" for the agents; the judge row stays. The default of `OPENROUTER_PROVIDER_ORDER` was changed to `groq` in its own commit.

### Enmienda 1b (19-nov): the measurement is repeated before the freeze

> El sábado antes del congelado se repite la medición de hosts con la misma regla. Si Groq no la pasa, se fija el siguiente host del ranking que sí la pase.

How this is read (written now, before the repetition): the same harness, rule, definitions and cap of amendment 1 and 1a, run on Saturday before the freeze of the evaluation commit. **Groq stays
pinned if it passes** the rule: it is a candidate that day (quantization, tools and uptime of the filter), its tools work in all 10 calls and both `q01` runs, and its p50 is within 1.5 times the
fastest. **If it does not pass**, the host pinned is the first of the ranking that does: the ranking is the rule's own order among the hosts that pass (the cheapest by real cost per call, a tie within 5 %
to the faster). A host that passes and is cheaper than Groq that day does not displace it: the amendment replaces a host that fails, it does not re-run the choice. The harness will get an `--incumbent` option for
this before Saturday's run; until then it reports its unconditional winner, as it did on the 19th.

#### 1b, aclaración, 19-nov 23:05, antes de cualquier medición del sábado

The owner confirmed the reading of amendment 1b written above, and this note fixes it so that it cannot be read otherwise later:

- **Groq stays pinned if it passes the rule. Another host replaces it only if Groq fails it.** "Fails" is: not a candidate that day (quantization, tools or uptime of the filter), not measured, tools not working
  in all 10 calls and both `q01` runs, or a p50 above 1.5 times the fastest of the hosts that pass. A host that passes and is cheaper, or faster, than Groq that day does **not** displace it.
- **Reason: C1 and C3 have to run on the same host to be comparable.** The model comparison (C1) and the final run (C3) are only comparable if the host of `openai/gpt-oss-120b` does not move between them; a
  host chosen again on every measurement would move it whenever another one is a little cheaper that day. The measurement of Saturday exists to catch a host that has become unusable, not to pick the best one again.
- **If Groq fails:** the host pinned is the first of the rule's own order among the hosts that pass, the rule applied without Groq (the cheapest within 1.5 times the fastest, a tie within 5 % to the faster).
  If no host passes, Groq stays and the report says so.
- **Implemented as `--incumbent`** of `eval/host_latency.py`: the incumbent defaults to the host pinned in the settings (`OPENROUTER_PROVIDER_ORDER`, today `groq`); `--incumbent none` gives the unconditional winner of the
  19th. The report states whether the incumbent passes and what the unconditional winner would have been.


## Enmienda 2 (20-nov): C2 did not meet its acceptance; the judge is declared new

C2 (`eval/calibration/JUDGE_EQUIV.md`) did not meet its rule: on the 15 rows of `sample_v3` the verdict agreement with Groq is 86.7 % (the 40 rows of repetition 1: 95 %), and there is one new false pass against the human (`q02_weather`).
No alternative host is tried: only fp8 hosts remain, and the author's unquantized host did not reproduce Groq. **This is a deviation from the plan and it is declared.**

- The judge is a **new judge**: Qwen3.8 27B on Alibaba, reasoning effort none. Its prompt and parser stay frozen.
- **C4/C5 (V4) is the only valid calibration.** V1-V3 describe the Groq judge.
- Every before/after comparison uses this judge; the baseline is `eval/reports/C2-full.judged.json` (repetition 1, re-judged).
- `q02_weather` and `m04_followup_product` (the two verdict disagreements with Groq) are reported with a note.
