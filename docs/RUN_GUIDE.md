# Guía paso a paso: correr el proyecto y hacer la demo del video

Pensada para Windows + PowerShell + Docker Desktop. Sigue los pasos en orden. Cada comando está probado.
Las secciones **A** (preparación) y **B** (levantar el sistema) se hacen **antes** de grabar. La **C** es lo que haces frente a la cámara.

> **Regla de oro de la cuota.** Los planes gratuitos tienen límites diarios. **No corras la evaluación completa (`eval`) antes de grabar**: gasta gran parte del límite diario del modelo principal y las respuestas saldrían con `degraded: true`. En el video solo *muestras* el reporte ya generado.

---

## A. Preparación (el día anterior o una hora antes)

### A1. Abre Docker Desktop
Espera a que el icono de la ballena (barra de tareas) deje de animarse. Verifica en una terminal:

```powershell
docker version
```
Debe mostrar `Client` y `Server`. Si dice que no puede conectarse al daemon, Docker Desktop no terminó de arrancar.

### A2. Abre PowerShell en la carpeta del proyecto
Opción fácil: en VS Code, menú **Terminal → New Terminal** (ya abre en la carpeta). Si no, en PowerShell:

```powershell
cd "C:\Users\User\OneDrive - Instituto Tecnologico y de Estudios Superiores de Monterrey\Documents\Proyecto_GetNet"
```

### A3. Revisa que las 3 claves estén puestas (sin mostrarlas en pantalla)
```powershell
Get-Content .env | Where-Object { $_ -match "^(GROQ|TAVILY|VOYAGE)_API_KEY=" } | ForEach-Object { ($_ -split "=")[0] + " -> " + $(if (($_ -split "=",2)[1].Trim()) {"OK"} else {"VACIA"}) }
```
Debe imprimir tres líneas con `OK`. **Nunca muestres el contenido de `.env` en el video**: tiene tus claves.

### A4. (Recomendado) Agrega método de pago en Voyage AI
Sin método de pago, Voyage permite solo **3 consultas por minuto**. Cada pregunta al agente de conocimiento usa una, así que varias preguntas seguidas se frenan (el sistema espera y reintenta, pero se ve como latencia de ~20 s). En <https://dashboard.voyageai.com/> → Billing, agregar una tarjeta sube el límite y los tokens gratuitos siguen aplicando. Tarda unos minutos en activarse.

### A5. Corre las pruebas automáticas una vez
```powershell
.\.venv\Scripts\python.exe -m pytest -q
```
Debe terminar con `67 passed`. Estas pruebas no usan red ni claves, no gastan cuota.

---

## B. Levantar el sistema (antes de grabar)

### B1. Construir la imagen
```powershell
docker compose build
```
La primera vez tarda unos minutos; las siguientes son rápidas.

### B2. Base de conocimiento (RAG)
Ya tienes el índice creado en `data\chroma` (1493 fragmentos). **No necesitas repetir la ingesta.** Compruébalo después del paso B3 con `/health` (debe decir `kb_chunks` mayor que 0).

Solo si `kb_chunks` sale en 0 (o borraste `data\chroma`), reconstrúyelo sin gastar créditos de Tavily, usando la copia guardada de las páginas:
```powershell
docker compose run --rm ingest python -m app.rag.ingest --from-cache
```
Tarda varios minutos con Voyage gratis (hace pausas de 21 s entre lotes a propósito). Si no existe `data\raw\pages.jsonl`, corre `docker compose run --rm ingest` (sin `--from-cache`), que vuelve a rastrear los sitios con Tavily.

### B3. Dejar el estado limpio y levantar la API
Esto borra las colas de demostraciones anteriores (tickets y handoffs), para que en el video solo aparezca lo que hagas ahí:
```powershell
Remove-Item data\handoffs.jsonl, data\tickets.jsonl -ErrorAction SilentlyContinue
docker compose up -d
```
Espera ~15 segundos y verifica:
```powershell
docker compose ps
Invoke-RestMethod http://localhost:8000/health
```
- `docker compose ps` debe mostrar el contenedor `api` en estado `Up ... (healthy)`.
- `/health` debe devolver `status: ok` y `kb_chunks: 1493` (o similar, mayor que 0).

> Si cambias código y quieres reiniciar con estado limpio (incluye la memoria de conversación): `docker compose restart api`.

### B4. Cargar la función de demostración (una vez por terminal)
Copia y pega **todo** este bloque en PowerShell. Crea el comando `ask` que envía una pregunta y muestra la respuesta de forma legible (tiene la decodificación UTF-8 para que las tildes se vean bien):

```powershell
[Console]::OutputEncoding = [Text.Encoding]::UTF8
function ask($msg, $user = "cliente1988") {
  $body = @{ message = $msg; user_id = $user } | ConvertTo-Json
  $resp = Invoke-WebRequest -Uri http://localhost:8000/chat -Method Post -UseBasicParsing -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body))
  $r = [Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray()) | ConvertFrom-Json
  Write-Host ""
  $r.response
  Write-Host ""
  Write-Host "plan: $($r.routing.plan -join ' -> ') | agents: $($r.agents_used -join ', ') | handoff: $($r.handoff.reason) $($r.handoff.id) | latency: $($r.latency_ms) ms" -ForegroundColor Cyan
  $r.tool_calls | ForEach-Object { Write-Host "tool: $($_.agent)/$($_.name)" -ForegroundColor Cyan }
  $r.sources | Select-Object -First 3 | ForEach-Object { Write-Host "source: $($_.url)" -ForegroundColor Cyan }
}
```
Prueba rápida (es la más barata, no usa agentes y tarda menos de 2 s):
```powershell
ask "I want to talk to a human agent."
```
Debe responder "Of course. I've passed your case to a human agent. Your reference is HND-... / TKT-..." y la línea `handoff: user_requested`.
Después de esta prueba, **repite B3** (borrar las colas y `docker compose restart api`) para que no aparezca en el video.

### B5. Ensayo completo (opcional pero recomendado, una sola vez)
Corre una vez las 4 preguntas del guion (sección C3) con **25 segundos de espera entre ellas** para ver que todo responde. Cada una gasta unos pocos miles de tokens; las 4 juntas son una fracción pequeña del límite diario (lo que gasta mucho es la evaluación completa). Si alguna sale lenta o con `degraded: true`, mira la sección **E. Problemas frecuentes**. Luego repite B3.

---

## C. Durante el video

Ten abiertas, en este orden: (1) VS Code con el repositorio, (2) la terminal con `ask` ya cargada, (3) el navegador en <http://localhost:8000/docs> (Swagger) por si quieres mostrar el contrato de la API.

### C1. Mostrar la estructura (0:00 – 2:00)
En VS Code abre y recorre: `docs/SPEC.md`, `README.md` (diagrama), `app/graph.py`. El guion exacto está en `docs/PRESENTATION_SCRIPT.md`.

### C2. Mostrar el código mientras explicas (2:00 – 10:15)
Sigue el orden del guion: `app/graph.py` → `app/agents/router.py` → `app/rag/ingest.py` y `app/tools/knowledge_tools.py` → `app/tools/customer_tools.py` → `app/handoff.py` (función `handoff_reason`).
Para mostrar el tamaño del índice:
```powershell
Invoke-RestMethod http://localhost:8000/health
```

### C3. Demo en vivo (10:15 – 13:00)
**Espera ~25 segundos entre una pregunta y otra** (límites por minuto de Groq y Voyage). Mientras esperas, sigue explicando lo que acabas de mostrar. En cada respuesta señala con el cursor las líneas azules: `plan`, `tool`, `source`, `handoff`.

| # | Comando | Qué debe pasar | Qué señalar |
|---|---|---|---|
| 1 | `ask "What's the difference between the Get Clássica and the Get Smart?"` | plan `knowledge`, herramienta `search_getnet_kb`, fuentes de getnet.com.br, sin handoff | RAG con citas |
| 2 | `ask "My card machine won't connect to the internet, what should I do?"` | plan `support -> knowledge`; la respuesta menciona que la terminal está *offline* con señal débil y luego da pasos | Datos del cliente + conocimiento, en orden |
| 3 | `ask "What's the euro exchange rate today?"` | herramienta `web_search`, fuentes web (Bloomberg, etc.) | Búsqueda web para preguntas generales |
| 4 | `ask "I want to talk to a human agent."` | plan vacío, **sin agentes**, `handoff: user_requested`, responde en ~1 s | Atajo directo a una persona |

Después de la #4, muestra lo que recibiría el operador humano:
```powershell
Get-Content data\handoffs.jsonl -Tail 1 | ConvertFrom-Json | Format-List handoff_id, reason, priority, ticket_id, user_message
Get-Content data\tickets.jsonl -Tail 1
```
Si te sobra tiempo:
```powershell
ask "Quero falar com um atendente, por favor." "cliente2024"        # el aviso sale en portugués
ask "Show me the recent transactions of user cliente2024."            # rechaza: solo ve datos de cliente1988
```

> **No uses en vivo** la pregunta "What is the exact interest rate on the loan contract I signed with Getnet last month?". Debería escalar a un humano, pero en mis pruebas lo hizo 2 de 3 veces: es la única demo que depende de que el modelo decida escalar. Si quieres mostrarla, ensáyala antes y graba solo si te sale bien.

### C4. Pruebas y evaluación (13:00 – 14:15)
Pruebas automáticas (tardan ~6 segundos):
```powershell
.\.venv\Scripts\python.exe -m pytest -q
```
Evaluación: **solo abre el reporte ya generado**, no la vuelvas a correr:
```powershell
code eval\reports\latest.md
```
Ese reporte (14 casos, 13 aprobados por el juez) es de antes de agregar el handoff. Dilo tal cual en el video; el guion ya incluye la frase. Los 3 casos de handoff están en `eval/dataset.jsonl` (`h01`, `h02`, `h03`).

---

## D. Después de grabar
```powershell
docker compose down
```
Esto detiene la API. Los datos (`data\chroma`) se conservan.

---

## E. Problemas frecuentes

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| Todas las respuestas son un aviso de "problema técnico" con referencia `HND-...` y `handoff: technical_failure` | Se agotó la cuota diaria del modelo principal de Groq (el handoff por fallo técnico está funcionando como debe) | Mira `docker compose logs api --tail 30`; si dice `tokens per day (TPD)`, espera a que se reinicie la cuota (ventana móvil) o cambia `LLM_MODEL=openai/gpt-oss-20b` en `.env` y haz `docker compose restart api` (más débil: puede inventar detalles) |
| Una pregunta tarda 20–60 s | Límite por minuto de Groq (8000 tokens) o de Voyage (3 consultas/min) | Espera 25–30 s entre preguntas; agrega método de pago en Voyage (A4) |
| `kb_chunks: 0` en `/health` | El índice no se cargó | Paso B2 |
| `docker compose up` falla con "port is already allocated" | Algo usa el puerto 8000 (otra copia del proyecto) | `docker compose down`, o cierra el otro programa |
| `docker: error during connect` | Docker Desktop no está abierto | Abrirlo y esperar a que termine de arrancar |
| Las tildes salen como `Ã©` | Usaste `Invoke-RestMethod` directo o no cargaste el bloque B4 | Usa la función `ask` del paso B4 |
| `ask` dice "no se reconoce el término" | Abriste una terminal nueva | Pega otra vez el bloque B4 |
| Los comandos `curl` de PowerShell dan error raro | En PowerShell, `curl` es un alias de otra cosa | Usa `ask` o Swagger (`/docs`) |
| Cambié código y no se nota | La imagen vieja sigue en uso | `docker compose build` y luego `docker compose up -d` |
