FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# deps first so this layer is cached
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY eval ./eval

# Chroma, crawl cache and tickets live in a mounted volume
RUN mkdir -p /app/data
ENV CHROMA_DIR=/app/data/chroma \
    RAW_DIR=/app/data/raw \
    TICKETS_FILE=/app/data/tickets.jsonl \
    HANDOFFS_FILE=/app/data/handoffs.jsonl

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
