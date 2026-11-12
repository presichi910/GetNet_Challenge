FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# deps first so this layer is cached
COPY requirements.txt .
RUN pip install -r requirements.txt

# Unprivileged user with a fixed uid/gid, so volume ownership is predictable.
# Created after pip install so the dependency layer stays cached.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app

# Code stays owned by root and read-only for the app user: the process cannot rewrite its own code.
COPY app ./app
COPY eval ./eval
COPY scripts ./scripts

# Only two places are writable: /app/data (Chroma, crawl cache, tickets, handoffs) and
# /app/eval/reports. /app/data is a named volume in docker-compose.yml: Docker initialises a new
# volume with this directory's owner, so no root entrypoint and no chown at start are needed.
RUN mkdir -p /app/data /app/eval/reports \
 && chown -R app:app /app/data /app/eval/reports
ENV CHROMA_DIR=/app/data/chroma \
    RAW_DIR=/app/data/raw \
    TICKETS_FILE=/app/data/tickets.jsonl \
    HANDOFFS_FILE=/app/data/handoffs.jsonl \
    HANDOFFS_DB_PATH=/app/data/handoffs.sqlite \
    CHECKPOINT_DB_PATH=/app/data/checkpoints.sqlite

USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
