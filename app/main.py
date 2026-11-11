"""FastAPI app. POST /chat with {"message"} and `Authorization: Bearer <jwt>`.

The customer is the JWT `sub`. A body `user_id` is optional and must match it.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException

from app.auth import Principal, require_scope, validate_jwt_secret
from app.config import get_settings
from app.schemas import ChatRequest, ChatResponse
from app.service import ChatService

log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=get_settings().log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    validate_jwt_secret(get_settings().jwt_secret)  # fail fast: never serve with a weak secret
    if getattr(app.state, "service", None) is None:  # tests set their own service
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


app = FastAPI(title="Getnet Multi-Agent Support", version="1.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    from app.rag.store import collection_size

    return {"status": "ok", "kb_chunks": collection_size()}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, principal: Principal = Depends(require_scope("chat"))) -> ChatResponse:
    # the customer is the token's `sub`, a body user_id is never trusted
    if req.user_id is not None and req.user_id != principal.user_id:
        raise HTTPException(status_code=403, detail="user_id does not match the authenticated user.")
    try:
        return app.state.service.handle(req.message, principal.user_id, conversation_id=req.conversation_id)
    except Exception as e:
        log.exception("Unhandled error in /chat")
        raise HTTPException(status_code=500, detail="Internal error while processing the request.") from e
