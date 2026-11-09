"""FastAPI entrypoint.   POST /chat  {"message": "...", "user_id": "..."}"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.config import get_settings
from app.schemas import ChatRequest, ChatResponse
from app.service import ChatService

log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=get_settings().log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if getattr(app.state, "service", None) is None:  # tests inject their own service
        from app.graph import build_default_graph
        from app.rag.store import collection_size

        app.state.service = ChatService(build_default_graph())
        n = collection_size()
        if n == 0:
            log.warning("Knowledge base is EMPTY. Run `python -m app.rag.ingest` "
                        "(docker: `docker compose run --rm ingest`).")
        else:
            log.info("Knowledge base ready: %d chunks", n)
    yield


app = FastAPI(title="Getnet Multi-Agent Support", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    from app.rag.store import collection_size

    return {"status": "ok", "kb_chunks": collection_size()}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    try:
        return app.state.service.handle(req.message, req.user_id)
    except Exception as e:
        log.exception("Unhandled error in /chat")
        raise HTTPException(status_code=500, detail="Internal error while processing the request.") from e
