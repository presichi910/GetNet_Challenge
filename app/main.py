"""FastAPI app. POST /chat with {"message"} and `Authorization: Bearer <jwt>`.

The customer is the JWT `sub`. A body `user_id` is optional and must match it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException

from app.auth import Principal, require_scope, validate_jwt_secret
from app.config import get_settings
from app.memory import Memory, ThreadLimitExceeded
from app.schemas import ChatRequest, ChatResponse
from app.service import ChatService

log = logging.getLogger("api")


async def _purge_loop(memory: Memory, every_seconds: float) -> None:
    """Retention: delete idle threads now and then. A failed pass is logged and tried again."""
    while True:
        await asyncio.sleep(every_seconds)
        try:
            await asyncio.to_thread(memory.purge_expired)
        except Exception:  # noqa: BLE001 - never kill the loop
            log.exception("Thread purge failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=get_settings().log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    validate_jwt_secret(get_settings().jwt_secret)  # fail fast: never serve with a weak secret
    memory = None
    purge_task = None
    if getattr(app.state, "service", None) is None:  # tests set their own service
        from app.graph import build_default_graph
        from app.memory import open_memory
        from app.rag.store import collection_size

        s = get_settings()
        memory = open_memory(s.checkpoint_db_path, ttl_days=s.thread_ttl_days,
                             max_threads_per_user=s.max_threads_per_user)
        app.state.service = ChatService(build_default_graph(checkpointer=memory.saver), memory=memory)
        log.info("Conversation memory: %s (ttl %s days, max %s threads per user)", memory.path,
                 s.thread_ttl_days or "off", s.max_threads_per_user or "unlimited")
        if s.thread_ttl_days:
            try:
                memory.purge_expired()  # at startup
            except Exception:  # noqa: BLE001 - a purge problem must not stop the API
                log.exception("Startup thread purge failed")
            purge_task = asyncio.create_task(_purge_loop(memory, s.purge_interval_minutes * 60))
        n = collection_size()
        if n == 0:
            log.warning("Knowledge base is EMPTY. Run `python -m app.rag.ingest` "
                        "(docker: `docker compose run --rm ingest`).")
        else:
            log.info("Knowledge base ready: %d chunks", n)
    try:
        yield
    finally:
        if purge_task is not None:
            purge_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await purge_task
        if memory is not None:
            memory.close()  # flush the WAL, release the file


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
    except ThreadLimitExceeded as e:  # a new conversation above the per-user limit; nothing was deleted
        raise HTTPException(status_code=429, detail=str(e)) from e
    except Exception as e:
        log.exception("Unhandled error in /chat")
        raise HTTPException(status_code=500, detail="Internal error while processing the request.") from e
