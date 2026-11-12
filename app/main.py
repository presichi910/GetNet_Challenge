"""FastAPI app. POST /chat with {"message"} and `Authorization: Bearer <jwt>`.

The customer is the JWT `sub`. A body `user_id` is optional and must match it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Response

from app.auth import Principal, require_merchant, require_operator, validate_jwt_secret
from app.config import get_settings
from app.handoff_store import HandoffConflict, HandoffNotFound, HandoffStore, iso
from app.memory import Memory, ThreadLimitExceeded
from app.schemas import (ChatRequest, ChatResponse, ConversationDetail, ConversationList, HandoffDetail,
                         HandoffList, HandoffStatusName, HandoffSummary, OperatorMessageList, ReplyRequest,
                         ReplyResult, is_valid_conversation_id, is_valid_handoff_id)
from app.service import ChatService

log = logging.getLogger("api")


def _purge_once(memory: Memory, handoffs: HandoffStore | None) -> None:
    """One retention pass: idle threads, then idle handoffs (same THREAD_TTL_DAYS). One failing does
    not skip the other."""
    try:
        memory.purge_expired()
    finally:
        if handoffs is not None:
            handoffs.purge_expired(memory.ttl_days)


async def _purge_loop(memory: Memory, every_seconds: float, handoffs: HandoffStore | None = None) -> None:
    """Retention: delete idle threads and handoffs now and then. A failed pass is logged and tried again."""
    while True:
        await asyncio.sleep(every_seconds)
        try:
            await asyncio.to_thread(_purge_once, memory, handoffs)
        except Exception:  # noqa: BLE001 - never kill the loop
            log.exception("Thread purge failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=get_settings().log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    validate_jwt_secret(get_settings().jwt_secret)  # fail fast: never serve with a weak secret
    memory = None
    handoffs = None
    purge_task = None
    if getattr(app.state, "service", None) is None:  # tests set their own service
        from app.graph import build_default_graph
        from app.handoff import AuditNotifier, FanOutNotifier
        from app.memory import open_memory
        from app.rag.store import collection_size

        s = get_settings()
        memory = open_memory(s.checkpoint_db_path, ttl_days=s.thread_ttl_days,
                             max_threads_per_user=s.max_threads_per_user)
        # the human queue: its own SQLite file. The JSONL stays as an audit copy, and what an older
        # version left in it is imported (once: running it again adds nothing)
        handoffs = HandoffStore(s.handoffs_db_path, lease_seconds=s.handoff_lease_minutes * 60)
        try:
            handoffs.import_jsonl(s.handoffs_file)
        except Exception:  # noqa: BLE001 - an import problem must not stop the API
            log.exception("Handoff import from %s failed", s.handoffs_file)
        notifier = FanOutNotifier(handoffs, AuditNotifier(s.handoffs_file))
        app.state.service = ChatService(build_default_graph(checkpointer=memory.saver, handoff_notifier=notifier),
                                        memory=memory, handoffs=handoffs)
        log.info("Conversation memory: %s (ttl %s days, max %s threads per user)", memory.path,
                 s.thread_ttl_days or "off", s.max_threads_per_user or "unlimited")
        if s.thread_ttl_days:
            try:
                _purge_once(memory, handoffs)  # at startup
            except Exception:  # noqa: BLE001 - a purge problem must not stop the API
                log.exception("Startup thread purge failed")
            purge_task = asyncio.create_task(_purge_loop(memory, s.purge_interval_minutes * 60, handoffs))
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
        if handoffs is not None:
            handoffs.close()


app = FastAPI(title="Getnet Multi-Agent Support", version="1.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    from app.rag.store import collection_size

    return {"status": "ok", "kb_chunks": collection_size()}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, principal: Principal = Depends(require_merchant)) -> ChatResponse:
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


# ---- the caller's own conversations. Same auth as /chat. The owner comes from the token only: the
# path holds a conversation_id, never a user. Somebody else's, a missing one and a malformed id all
# answer the same 404, so the API never says whether a conversation exists. ----

def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Conversation not found.")


@app.get("/conversations", response_model=ConversationList)
def list_conversations(principal: Principal = Depends(require_merchant)) -> ConversationList:
    items = app.state.service.list_conversations(principal.user_id)
    return ConversationList(conversations=items, count=len(items), limit=app.state.service.conversation_limit())


@app.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: str, principal: Principal = Depends(require_merchant)) -> ConversationDetail:
    found = None
    if is_valid_conversation_id(conversation_id):
        found = app.state.service.get_conversation(principal.user_id, conversation_id)
    if found is None:
        raise _not_found()
    return ConversationDetail(**found)


@app.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, principal: Principal = Depends(require_merchant)) -> Response:
    deleted = False
    if is_valid_conversation_id(conversation_id):
        deleted = app.state.service.delete_conversation(principal.user_id, conversation_id)
    if not deleted:
        raise _not_found()
    log.info("conversation deleted user=%s conv=%s", principal.user_id, conversation_id)
    return Response(status_code=204)


@app.get("/conversations/{conversation_id}/operator-messages", response_model=OperatorMessageList)
def conversation_operator_messages(conversation_id: str,
                                   principal: Principal = Depends(require_merchant)) -> OperatorMessageList:
    """What a human operator answered in this conversation. The same messages also arrive once, in
    `operator_messages` of the next /chat response; this endpoint lets the customer read them (again)
    without sending a turn. The owner is the token sub: somebody else's conversation is the same 404."""
    found = None
    if is_valid_conversation_id(conversation_id):
        found = app.state.service.operator_messages(principal.user_id, conversation_id)
    if found is None:
        raise _not_found()
    return OperatorMessageList(conversation_id=conversation_id, messages=found)


# ---- human oversight: /operator/*. Only role operator (a merchant token is 403, whatever its scopes).
# An operator sees every merchant's handoffs, that is the job; each action is logged with the operator
# id and stored in handoff_events. ----

def _queue() -> HandoffStore:
    service = getattr(app.state, "service", None)
    store = getattr(service, "handoffs", None)
    if store is None:
        raise HTTPException(status_code=503, detail="The handoff queue is not available.")
    return store


def _handoff_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Handoff not found.")


def _summary_fields(row: dict) -> dict:
    return {**{k: row[k] for k in ("id", "conversation_id", "merchant_id", "reason", "priority", "summary",
                                   "status", "claimed_by", "resolution")},
            "claimed_at": iso(row["claimed_at"]), "lease_expires_at": iso(row["lease_expires_at"]),
            "resolved_at": iso(row["resolved_at"]),
            "created_at": iso(row["created_at"])}


def _detail(row: dict) -> dict:
    return {**_summary_fields(row), "operator_reply": row["operator_reply"], "record": row["record"]}


@app.get("/operator/handoffs", response_model=HandoffList)
def operator_list(status: HandoffStatusName | None = None, limit: int = Query(100, ge=1, le=500),
                  operator: Principal = Depends(require_operator)) -> HandoffList:
    """The queue, oldest first. `?status=pending` is what is waiting for somebody."""
    rows = _queue().list_queue(status, limit)
    return HandoffList(handoffs=[HandoffSummary(**_summary_fields(r)) for r in rows], count=len(rows))


@app.get("/operator/handoffs/{handoff_id}", response_model=HandoffDetail)
def operator_get(handoff_id: str, operator: Principal = Depends(require_operator)) -> HandoffDetail:
    row = _queue().get(handoff_id) if is_valid_handoff_id(handoff_id) else None
    if row is None:
        raise _handoff_not_found()
    log.info("handoff read id=%s operator=%s", handoff_id, operator.user_id)
    return HandoffDetail(**_detail(row))


@app.post("/operator/handoffs/{handoff_id}/claim", response_model=HandoffDetail)
def operator_claim(handoff_id: str, operator: Principal = Depends(require_operator)) -> HandoffDetail:
    """pending -> claimed. Claiming what you already hold is fine (same answer); someone else's is 409."""
    if not is_valid_handoff_id(handoff_id):
        raise _handoff_not_found()
    try:
        return HandoffDetail(**_detail(_queue().claim(handoff_id, operator.user_id)))
    except HandoffNotFound as e:
        raise _handoff_not_found() from e
    except HandoffConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@app.post("/operator/handoffs/{handoff_id}/reply", response_model=ReplyResult)
def operator_reply(handoff_id: str, req: ReplyRequest, operator: Principal = Depends(require_operator)) -> ReplyResult:
    """claimed -> resolved, only by the operator who claimed it. The message reaches the customer's
    conversation once, however many times the same idempotency_key is sent."""
    if not is_valid_handoff_id(handoff_id):
        raise _handoff_not_found()
    try:
        row, replayed = _queue().reply(handoff_id, operator.user_id, req.decision, req.message, req.idempotency_key)
    except HandoffNotFound as e:
        raise _handoff_not_found() from e
    except HandoffConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return ReplyResult(**_detail(row), replayed=replayed)
