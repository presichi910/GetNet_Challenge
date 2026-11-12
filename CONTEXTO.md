# CONTEXTO.md — GetNet Multi-Agent Support System

Fuente única de contexto para cualquier sesión nueva (Claude Code o chat). Sustituye a `PASO2_CONTEXTO.md`, `docs/CONTEXT_FOR_NEW_CHAT.md`, `contexto_p1.md` y `Contexto_p4.md`.
Instantánea: **2026-11-12**. El repo avanza más rápido que este archivo: **antes de actuar, verifica con `git status`, `git log origin/main..HEAD --oneline` y `pytest`**. Este archivo no contiene secretos. Nunca leas ni edites `.env`.

---

## 1. El reto

"AI Hardcore Engineer – Multi-Agent Support System" para Getnet (adquirente de pagos de Brasil). Enunciado en `AI_Hardcore_Engineer_Multi-Agent_(3)_2.md` (raíz del repo). Requisitos:

- Al menos 3 tipos de agente: **Router** (entrada, decide agente o secuencia), **Knowledge** (RAG sobre getnet.net + búsqueda web), **Customer Support** (datos del usuario, al menos 2 tools).
- Endpoint HTTP `POST` con payload `{"message": ..., "user_id": ...}` y respuesta JSON útil.
- Dockerfile y docker-compose.
- Estrategia de testing (incluida la integración de la orquestación).
- README completo: build, configuración, ejecución, tests, arquitectura, workflow, pipeline RAG y uso de tools.
- **Entrega:** link al repo de GitHub + **video de máximo 15 min** explicando el razonamiento y la arquitectura.
- Bonus: 4.º agente de escalamiento humano, guardrails, handoff, estrategia de evaluación y observabilidad (dashboards, trazas, logs estructurados, datasets de evaluación, alertas).
- 10 escenarios de ejemplo (q01–q10) con `user_id: cliente1988`.

Repo: `https://github.com/presichi910/GetNet_Challenge` (rama `main`). Ruta local: `C:\Users\User\OneDrive - Instituto Tecnologico y de Estudios Superiores de Monterrey\Documents\Proyecto_GetNet` (Windows + OneDrive). El usuario habla español; código, commits y docs del repo van en inglés.

## 2. Feedback V4 recibido (resumen)

Nota oficial **6.0/10**, técnica antes del gate **≈ 7.3**. La limitó el **Critical Security Gate**: `POST /chat` aceptaba un `user_id` sin autenticar que elegía a la vez los datos del cliente y el `thread_id` de la memoria (`thread_id = user_id`, `MemorySaver`).

| Criterio | Peso | Nota V4 | Qué pedía el revisor |
|---|---:|---:|---|
| Business & Challenge Fit | 8% | 9.8 | — |
| Agentic Reasoning & Orchestration | 15% | 8.5 | Replanificación adaptativa (observe → replan) ausente |
| Context, Knowledge & RAG | 10% | 8.8 | BM25/híbrido/RRF/reranker, recall@k, MRR, nDCG, holdout de retrieval |
| Tool & Action Engineering | 10% | 9.5 | Proteger tal cual |
| Security & Identity | 13% | 2.5 | JWT/OIDC, principal autenticado, ownership de conversación, tests negativos |
| Memory, State & Resilience | 8% | 5.5 | Persistencia, resumen rodante, `conversation_id` separado, ownership |
| Human Oversight & Risk | 7% | 4.5 | Operador: claim → reply → approve/reject → resume |
| Evaluation & Quality | 14% | 8.7 | Reporte con los 17 casos, calibración humana, corridas repetidas, acuerdo del juez, holdout ciego |
| Observability, Reliability & Cost | 6% | 5.5 | Tracing persistente, OTel/Langfuse, tokens, costo, dashboards, alertas |
| Production Engineering | 5% | 7.5 | Docker no-root, CI, memoria que sobrevive reinicios |
| Engineering Judgment | 4% | 9.8 | Proteger tal cual |

Prioridades de evolución del revisor: 1 JWT/OIDC · 2 `conversation_id` ≠ `user_id` · 3 ownership · 4 checkpointer SQLite/Postgres · 5 tests negativos entre usuarios · 6 operador reply/approve/reject/resume · 7 replanificación adaptativa · 8 calibración humana del juez · 9 reporte versionado con los 17 casos · 10 CI + Docker no-root + tracing/costo.

## 3. Matriz feedback → estado

Leyenda: ✅ hecho · 🟡 a medias (hecho en código, falta evidencia o visibilidad) · ❌ falta.
"Visible en GitHub" = está en `origin/main` (`022c38a`). Lo marcado como **local** está en commits sin push y el revisor **no lo ve**.

| # | Punto del feedback | Estado | Evidencia / qué falta |
|---|---|---|---|
| 1 | JWT + principal autenticado (Gate) | ✅ visible | `app/auth.py` (PyJWT, HS256 fijo, exige `exp/iss/aud/sub`, secreto débil = no arranca). `user_id` del body opcional y debe igualar `sub` (403). Verificado con LLM real el 2026-11-12. |
| 2 | `conversation_id` separado de `user_id` | ✅ visible | `thread_id = "<sub>:<conversation_id>"` (`app/service.py`). |
| 3 | Ownership de conversación | ✅ visible | Tabla `threads` en `app/memory.py`; `GET/DELETE /conversations/{id}`; ajeno/inexistente = mismo 404. |
| 4 | Tests negativos entre usuarios | ✅ visible | `tests/test_auth.py` (`test_body_user_id_of_another_customer_is_403`, `test_prompt_asking_for_another_customer_still_runs_as_the_token_user`), `tests/test_memory.py::test_two_users_never_cross_threads_with_the_persistent_saver`, `tests/test_operator_api.py`, `tests/test_tool_schemas.py`. |
| 5 | Checkpointer persistente | ✅ visible / 🟡 sin LLM real | `SqliteSaver` (`langgraph-checkpoint-sqlite==3.1.3`) en el volumen `data`. Falta deuda 7 (reinicio con conversación viva y LLM real). |
| 6 | Resumen rodante | ✅ visible / 🟡 sin LLM real | Nodo `summarize` (`app/agents/summarizer.py`), `SUMMARY_THRESHOLD=12`, `RemoveMessage`, nunca rompe el turno. Falta deuda 8 (qué escribe el LLM real, `x04` con resumen presente, ruta de timeout). |
| 7 | Retención / privacidad de memoria | ✅ visible | TTL, `MAX_THREADS_PER_USER` (429), `secure_delete` + truncado del WAL, una fila de checkpoint por hilo. Deuda 9 opcional (sin tokens). |
| 8 | HITL operador: claim/reply/approve/reject/resume | ✅ visible / 🟡 sin LLM real | `app/handoff_store.py` (cola SQLite, exactamente-una-vez por SQL, leases, idempotencia), `/operator/*`, rol `operator`, la respuesta del operador entra al contexto como `[Operador humano]`. Falta deuda 11–13, y la 12 es la más sensible (tratarla como cambio de prompt, correr `x04`). |
| 9 | Reporte versionado con los 17 casos | 🟡 **local** | En GitHub `eval/reports/latest.md` sigue siendo el de 14 filas (2026-11-09, sin `h01–h03`). Según los chats previos, el reporte de 17 casos (routing/tools/handoff/juez 100%, groundedness 93%, completitud 94%, p50 4,2 s, p95 8,6 s) está en commits locales sin push. **Hay que hacer push.** |
| 10 | Docker no-root | ✅ visible | `Dockerfile` con `USER app` (uid 10001), código de solo lectura, volumen nombrado `data`. |
| 11 | CI | 🟡 | `.github/workflows/ci.yml` (pytest en 3.12 + build + chequeo uid 10001) existe en `origin/main`, pero **nadie ha visto una ejecución verde en GitHub**. |
| 12 | Tracing, tokens, costo, dashboards, alertas | ❌ | Solo hay `trace_id`, latencia y un log `key=value` por request (`app/service.py`). El eval no registra tokens. README: "Not done: guardrails, tracing/dashboards". **Es el bloque más grande que falta (paso 5).** |
| 13 | Calibración humana del juez | ❌ | No hay etiquetas humanas ni métrica de acuerdo. Requiere que el usuario etiquete casos. |
| 14 | Corridas repetidas sistemáticas | 🟡 | Hay repeticiones puntuales (`q05` ×3, `q08` ×4, `x04` ×5, `BASELINE-REPEATS-20261112.md`, local), no un reporte de varianza del eval completo. |
| 15 | Acuerdo del juez / holdout ciego | ❌ | Un solo juez (`qwen/qwen3.8-27b`). Sin holdout. |
| 16 | Replanificación adaptativa | ❌ por decisión | El usuario defiende "plan first, then run" (README línea ~199). Falta un argumento escrito con evidencia (qué mediría para cambiarlo). |
| 17 | RAG híbrido + métricas de retrieval | ❌ | Solo denso (Voyage + Chroma, máx. 2 PDFs). Sin recall@k, MRR ni nDCG. |
| 18 | Seguimientos sobre la propia conversación ("¿qué te dije?") | 🟡 | No lo citó el revisor, pero afecta a la memoria percibida: el Router manda `m01–m03` a Knowledge (0/3 cada uno, 12/12 en total); `m04–m05` aciertan 8/8. La memoria funciona (`tests/test_followup_context.py`); el fallo está en el prompt del Router. Decisión pendiente: (a) arreglar el prompt y medir o (b) dejarlo como limitación. |
| 19 | Falso positivo de `x04` (1 de 5 escala a humano) | 🟡 documentado | Rechaza bien 5/5; arreglarlo exige tocar el prompt de Support (prohibido sin medir). Se deja como limitación. |
| 20 | Video de entrega actualizado | ❌ (usuario) | El enunciado pide video ≤ 15 min. |

## 4. Arquitectura

```
POST /chat (JWT) -> [summarize] -> router -> {support | knowledge}* -> synthesizer -> respuesta
                                       \-> handoff (pide humano / nadie resolvió / falla técnica)
```

- **Router** (`LIGHT_MODEL`): plan ordenado (support antes que knowledge), idioma, `wants_human`. Plan vacío = saludo/fuera de alcance.
- **Knowledge** (`LLM_MODEL`): RAG en Chroma (getnet.net + máx. 2 PDFs) o Tavily para preguntas generales. Embeddings Voyage.
- **Support** (`SUPPORT_MODEL`, vacío = `LIGHT_MODEL`): tools ligadas por closure al `sub` del token (`make_support_tools(user_id, repo)`); ninguna tool tiene parámetro de usuario. Datos falsos: `app/data/customers.json` (`cliente1988`, `cliente2024`, `cliente3030`).
- **Synthesizer** (`LIGHT_MODEL`): 0 agentes responde directo, 1 lo pasa, 2 los fusiona.
- **Handoff**: ticket + caso en la cola SQLite; aviso al cliente.
- **Summarize**: por encima de `SUMMARY_THRESHOLD` pliega mensajes viejos en `summary`.
- Falla suave: un fallo de LLM o tool degrada (`degraded: true`); si todo falla, va a un humano. Un solo worker de uvicorn.
- `build_default_graph()` sin argumentos usa `MemorySaver` (el eval no escribe conversaciones en disco); la API usa `SqliteSaver`.

### Mapa de archivos

| Ruta | Contenido |
|---|---|
| `app/main.py` | Rutas, lifespan (abre memoria y cola, purga al arrancar y periódica) |
| `app/auth.py` | JWT, scopes, roles `merchant`/`operator` |
| `app/service.py` | `ChatService`: turno, hilos, endpoints de conversación, mensajes del operador, log por request |
| `app/graph.py`, `app/state.py` | Grafo y estado (el router resetea los campos por turno) |
| `app/agents/` | `router`, `knowledge`, `support`, `synthesizer`, `summarizer`, `common` (`recent_dialogue`) |
| `app/prompts.py` | Todos los prompts (**frágiles**, ver §5) |
| `app/llm.py` | `get_llm()` → `ChatGroq` |
| `app/memory.py` | SqliteSaver, tabla `threads`, TTL, límite, prune, `secure_delete` |
| `app/handoff.py`, `app/handoff_store.py` | Nodo handoff, notificadores, cola SQLite |
| `app/rag/` | `sources`, `ingest`, `store` |
| `app/tools/` | `customer_tools`, `knowledge_tools`, `handoff_tools` |
| `eval/` | `run_eval.py`, `judge.py`, `dataset.jsonl`, `reports/` |
| `scripts/issue_token.py` | JWT de demo |
| `docs/SPEC.md` | Requisitos F1–F41 / N1–N7 con su check |
| `docs/VERIFICATION_DEBT.md` | Verificación pendiente con LLM real (items 1–13; 1–6 cerrados en local) |

### Endpoints

- `GET /health` (público).
- `POST /chat` (rol merchant, scope `chat`): `{message, user_id?, conversation_id?}`. La respuesta incluye `routing`, `agents_used`, `tool_calls`, `degraded`, `handoff`, `latency_ms`, `trace_id`, `operator_messages`, `open_handoffs`.
- `GET /conversations`, `GET /conversations/{id}`, `DELETE /conversations/{id}` (204), `GET /conversations/{id}/operator-messages`.
- `GET /operator/handoffs`, `GET /operator/handoffs/{id}`, `POST .../claim`, `POST .../reply {decision, message, idempotency_key}`, `POST .../reject {reason, close}`.
- Códigos: 401 token inválido, 403 scope/rol/`user_id` ajeno, 404 ajeno o inexistente (mismo cuerpo), 409 conflicto de claim/lease/idempotencia, 429 límite de hilos.

### Stack

Python 3.12 (imagen y CI) / 3.13 (`.venv` local), FastAPI, LangGraph 1.2.20 (StateGraph + SqliteSaver), LangChain 1.4.x, `langchain-groq` (Groq), Voyage (embeddings), Chroma, Tavily, PyJWT, SQLite, Docker, GitHub Actions.

### Variables de entorno (nombres; valores en `.env`, nunca en el repo)

- Claves: `GROQ_API_KEY`, `TAVILY_API_KEY`, `VOYAGE_API_KEY`.
- Auth: `JWT_SECRET` (≥ 32 caracteres, sin patrón de plantilla), `JWT_ISSUER`, `JWT_AUDIENCE`.
- Modelos: `LLM_MODEL` (120b), `LIGHT_MODEL` (20b), `SUPPORT_MODEL` (`.env.example` = `openai/gpt-oss-120b`), `JUDGE_MODEL` (`qwen/qwen3.8-27b`), `REASONING_EFFORT`.
- Memoria: `CHECKPOINT_DB_PATH`, `SUMMARY_THRESHOLD`=12, `HISTORY_WINDOW`=6, `SUMMARY_TIMEOUT_SECONDS`=20, `THREAD_TTL_DAYS`=30, `PURGE_INTERVAL_MINUTES`=60, `MAX_THREADS_PER_USER`=20.
- Cola: `HANDOFFS_DB_PATH`, `HANDOFF_LEASE_MINUTES`=15, `HANDOFF_IMPORT_JSONL`=false.

## 5. Decisiones ya tomadas (no revertir sin medir)

1. **El prompt de Support no se toca.** Muestra el `user_id` derivado del token. Quitarlo hizo fallar `x04` 5/5; tres reformulaciones rompieron `h03` o escalaron preguntas normales. Si algo lo afecta, medir `x04` + `h03` + sondas "mis datos" juntos.
2. **Sin chequeo determinista** del mensaje contra datos ajenos. Defensa: tools ligadas al token + prompt + eval `x04`.
3. **`SUPPORT_MODEL` en el 120b**: con el 20b, `q08` se salta `get_recent_transactions` (5 de 6); con el 120b la llama 4 de 4.
4. **Auth**: HS256 fijo en código (nunca el algoritmo del token); `role` ausente = merchant, desconocido = 401.
5. **Volumen nombrado `data`** (en el proyecto actual, `proyecto_getnet_data`), contenedor uid 10001, código de solo lectura.
6. **Una fila de checkpoint por hilo** (`prune_thread` en SQL); `SqliteSaver.prune` no existe en 3.1.3.
7. **`secure_delete=ON` + truncar WAL** tras DELETE o purga; hay tests que buscan texto en los bytes de `.sqlite` y `-wal`.
8. **Mensaje del operador** en el contexto como `[Operador humano] (decision) texto`; si el cliente escribe la etiqueta, se convierte en `[cita: Operador humano]`. Si regresa `x04`, se desactiva quitando `context=` en `ChatService.handle`.
9. **Copia JSONL de handoffs redactada**; el caso completo vive solo en `handoffs.sqlite`.
10. **Cola**: cada transición es `UPDATE ... WHERE status=<esperado>` con conteo de filas; leases perezosos; reloj inyectable.
11. **"Plan first, then run"** en vez de bucle supervisor: más barato, predecible y testeable.
12. **El score de retrieval no se usa como gate de escalamiento** (no separaba preguntas con respuesta y sin respuesta); el escalamiento es una tool (`escalate_to_human`).
13. Scope `chat` también para los endpoints de conversación.

## 6. Lo que ya funciona (con evidencia)

- 408 tests en verde con `pytest -m "not eval"` según los chats previos; 25 archivos en `tests/`; sin red ni claves.
- Eval: 22 casos en local (17 de un turno: 10 del reto + extras + 3 de handoff; 5 multi-turno `m01–m05`). En GitHub hay 17. Mide routing, tools, handoff, latencia, tasa de degradados y un juez de otra familia (relevancia, groundedness, completitud, tono) que recibe la evidencia real de los agentes. Un error de proveedor se marca `error`, no cuenta y nunca pisa `latest.md`; una corrida parcial (`--only`) tampoco.
- Evidencia local: `latest.md` (17 casos), `BASELINE-FULL-DELIVERED-20261112-092550.md`, `BASELINE-HEAD-DEFAULT-SUBSET-20261112-092912.md`, `BASELINE-BAB3729-VS-HEAD-SUBSET-20261112.md`, `BASELINE-REPEATS-20261112.md`, `BASELINE-MULTITURN-20261112.md`.
- Docker verificado el 2026-11-12: uid 10001, Support con tools para dos clientes sin cruce, rechazo de datos ajenos, handoffs en el volumen, eval dentro del contenedor.

## 7. Lo que falta

1. **Push** de los commits locales (reporte de 17 casos, deuda 1–6 cerrada, `m01–m05`, docs) y ver el CI verde en GitHub.
2. **Observabilidad (paso 5)**: tokens y costo por agente/modelo/turno, trazas persistentes por nodo, métricas agregadas, alerta mínima.
3. **Evaluación**: holdout, corridas repetidas con varianza, herramienta de calibración humana (+ etiquetas del usuario), acuerdo del juez.
4. **Verificación con LLM real**: deuda 7–13 de `docs/VERIFICATION_DEBT.md`.
5. **Router y seguimientos conversacionales** (`m01–m03`), si el usuario elige arreglarlo.
6. **RAG**: métricas de retrieval y, opcionalmente, híbrido BM25 + RRF.
7. **Documento para el revisor** que cruce cada punto del feedback con commits, tests y reportes.
8. **Docs al día**: README (cuenta de tests 399 → real, Limitations, Re-evaluation), `docs/SPEC.md`, `docs/VERIFICATION_DEBT.md`.
9. **Video ≤ 15 min** y re-entrega (lo hace el usuario).

Riesgos conocidos que se quedan documentados (no los pidió el feedback): `tickets.jsonl` guarda 200 caracteres de la petición fuera de DELETE/TTL; sin cifrado en reposo; auth demo HS256 (en producción haría falta IdP RS256/JWKS, refresh, revocación y rate limiting); sin UI de operador ni notificaciones; un solo worker; un reintento tras fallo puede duplicar el mensaje del operador en el hilo; el 20b manda `{'': ''}` a tools sin argumentos.

## 8. Cómo correr y probar

```bash
# Tests (sin red ni claves). En Windows el python global no tiene pytest:
.venv/Scripts/python.exe -m pytest -q -m "not eval"

# Tokens de demo (requiere JWT_SECRET en el entorno)
python -m scripts.issue_token --user cliente1988
python -m scripts.issue_token --role operator --operator ops1

# API
docker compose build && docker compose up -d          # :8000, volumen data
docker compose run --rm ingest                        # reconstruye la KB (Voyage/Tavily, gasta cuota)

# Eval (GASTA CUOTA del LLM; requiere GROQ_API_KEY como variable de entorno)
python -m eval.run_eval --only q05 --pause 25 --retries 3   # parcial, no pisa latest.md
python -m eval.run_eval --pause 25 --retries 3              # completa; pisa latest.md si no hay errores
git checkout eval/reports/latest.md                          # restaurar tras una corrida que no se quiera guardar
```

- **Entorno limpio sin `.env`**: `git archive HEAD | tar -x -C <carpeta>` y pytest allí con `JWT_SECRET` falso; conservar `USERPROFILE` (chromadb lo necesita).
- **Docker sin tocar el contenedor del usuario** (:8000): `docker compose -p getnet-ci -f docker-compose.yml -f <override con ports: !override ["8001:8000"]> --project-directory .` y al final `docker compose -p getnet-ci ... --profile tools down -v` (solo de **ese** proyecto).
- Smoke manual: `GET /health`; `POST /chat` sin token → 401; con token de `cliente1988` → 200; `user_id` ajeno en el body → 403; `GET /conversations/<id ajeno>` → 404.

## 9. Cuota y costo del LLM

- Plan gratuito de Groq: por modelo **8.000 tokens/min y 200.000/día**; Knowledge y Support comparten el bucket del 120b. Un día de pruebas lo agota y los agentes degradan (comportamiento diseñado, pero invalida las mediciones).
- Consumo de una corrida de 17 casos (chats previos): ~56,7K tokens en el 120b, ~18,8K en el 20b, ~26K en el juez.
- Una llamada pequeña que pasa no prueba margen diario. Si el router da 429, la app cae a Knowledge y gasta el 120b. **Nunca bucles A/B en el plan gratuito.**
- En el plan Developer (pago por uso) los mismos modelos no tienen tope diario y el límite por minuto sube mucho; no requiere cambios de código.

## 10. Reglas de trabajo con el usuario

1. Al empezar, listar decisiones con defaults y esperar confirmación (si el prompt ya trae las decisiones, aplicarlas y reportarlas).
2. No tocar `.env`. Nunca escribir claves en código, docs, commits ni logs.
3. No hacer push sin confirmación explícita.
4. No cambiar prompts, agentes ni routing salvo que el usuario lo apruebe; si se hace, eval antes y después.
5. Un commit por cambio lógico, en inglés, con tests; pytest en verde antes y después (encadenar con `&&`, nunca `;`).
6. Preguntar antes de: añadir dependencias, borrar archivos o datos, reconstruir el contenedor del usuario, gastar cuota o dinero fuera de lo acordado.
7. Reportar con honestidad lo no verificado y anotarlo en `docs/VERIFICATION_DEBT.md`.

## 11. Trampas del entorno

- Heredocs de Bash con apóstrofes o `\`, y `python - <<EOF` con `"\n"`, se rompen. Usa Write/Edit o un script en archivo; `chr(92)` para backslashes.
- Varios archivos usan **CRLF** (`schemas.py`, docs): respétalo. "LF will be replaced by CRLF" es ruido.
- Los tests deben aislar `./data` (`tmp_path` + `HANDOFFS_DB_PATH`, `HANDOFFS_FILE`, `CHECKPOINT_DB_PATH`, `TICKETS_FILE`).
- Git Bash: `MSYS_NO_PATHCONV=1` y `$(pwd -W)` en `docker -v`. `curl -o /tmp/x` no escribe donde crees.
- `docker compose down -v` **borra el volumen `data`** (Chroma, colas, checkpoints). Respaldo probado en README ("Production hardening"). Los `.tgz` no están en `.gitignore`: nunca `git add .`.
- No guardes reportes de eval en un worktree temporal. Borrar worktrees en OneDrive: PowerShell `Remove-Item -Recurse -Force .git\worktrees\<nombre>`.
- `docker compose config` no muestra `eval` sin `--profile tools`.
- `data/handoffs.jsonl` local tiene ~85 líneas de pruebas viejas con extractos (en `.gitignore`); la importación está apagada por defecto.
- Docker Desktop puede estar apagado: comprobar, no asumir.

## 12. Estado de git (2026-11-12)

- `origin/main` = `022c38a`.
- Commits locales sin push (según los chats previos): `f55a085`, `011c184`, `7e69060` (cierre deuda 1–6 + reporte 17 casos), `f48b5bb` (test historial turno 2), `2396958` (casos `m01–m05`), `2dbffca` (docs Router y `x04`).
- Archivos de contexto sueltos sin commitear: `PASO2_CONTEXTO.md`, `docs/CONTEXT_FOR_NEW_CHAT.md` (y posiblemente `contexto_p1.md`, `Contexto_p4.md`). Este `CONTEXTO.md` los sustituye; borrarlos solo con permiso del usuario.
