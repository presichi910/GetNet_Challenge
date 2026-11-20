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
