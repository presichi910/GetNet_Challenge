"""The handoff queue: SQLite, with the state machine the operators work through.

    pending --claim(operator A)--> claimed --reply(operator A)--> resolved
                                     |  ^ claim again by A: same result, nothing changes
                                     +-- claim/reply by B: 409

Every transition is ONE statement, `UPDATE ... WHERE status = <expected> [AND claimed_by = <me>]`,
and the rows it touched are counted: 1 means this caller won, 0 means somebody else got there first
(or it is not allowed), and a SELECT then says which 409/404 it is. That statement is what gives
exactly-once, not the Python lock: two connections, two processes or two threads can race and
SQLite lets one UPDATE through. `operator_messages.handoff_id` is UNIQUE as a second guard, so a
handoff can never deliver two messages whatever the code does.

This file is separate from the conversation checkpoints on purpose: handoffs must not depend on
which checkpointer the conversations use, and the retention purge of conversations must not eat the
queue. The customer's message lives here too, so it survives any change of checkpointer.

Everything an operator does is appended to `handoff_events` with the operator id and a timestamp.

Customer data at rest: `record` is the whole handoff record (the recent conversation, what the
agents found, the tool calls). It lives only here, so deleting a conversation or the TTL purge can
erase it: the JSONL audit file gets a redacted copy (no excerpt). See README, "Human oversight".
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("handoff_store")

STATUSES = ("pending", "claimed", "resolved")
DECISIONS = ("approve", "reject")

SCHEMA = """
CREATE TABLE IF NOT EXISTS handoffs (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    merchant_id     TEXT NOT NULL,
    reason          TEXT NOT NULL,
    priority        TEXT NOT NULL DEFAULT 'normal',
    summary         TEXT NOT NULL,
    record          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'claimed', 'resolved')),
    claimed_by      TEXT,
    claimed_at      REAL,
    resolution      TEXT CHECK (resolution IS NULL OR resolution IN ('approve', 'reject')),
    operator_reply  TEXT,
    resolved_at     REAL,
    created_at      REAL NOT NULL,
    idempotency_key TEXT
);
CREATE INDEX IF NOT EXISTS handoffs_queue ON handoffs(status, created_at);
CREATE INDEX IF NOT EXISTS handoffs_conversation ON handoffs(merchant_id, conversation_id);

CREATE TABLE IF NOT EXISTS operator_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id      TEXT NOT NULL UNIQUE REFERENCES handoffs(id),
    merchant_id     TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    decision        TEXT NOT NULL,
    body            TEXT NOT NULL,
    created_at      REAL NOT NULL,
    delivered_at    REAL
);
CREATE INDEX IF NOT EXISTS operator_messages_conversation ON operator_messages(merchant_id, conversation_id);

CREATE TABLE IF NOT EXISTS handoff_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id TEXT NOT NULL,
    at         REAL NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS handoff_events_handoff ON handoff_events(handoff_id);
"""

_LIST_COLUMNS = ("id, conversation_id, merchant_id, reason, priority, summary, status, claimed_by, claimed_at, "
                 "resolution, resolved_at, created_at")


class HandoffNotFound(Exception):
    """No handoff has that id."""


class HandoffConflict(Exception):
    """The handoff is not in a state that allows this (or belongs to another operator). Maps to 409."""


def iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat() if epoch is not None else None


def case_summary(record: dict) -> str:
    """The short case description an operator reads in the list. Deterministic, no LLM."""
    parts = [f"Customer: {str(record.get('user_message', ''))[:300]}"]
    findings = record.get("agent_findings") or {}
    if findings:
        parts.append("Agents found: " + "; ".join(f"{a}: {str(t)[:200]}" for a, t in findings.items()))
    if record.get("earlier_summary"):
        parts.append(f"Earlier in the conversation: {str(record['earlier_summary'])[:200]}")
    return " | ".join(parts)


def _epoch(value) -> float:
    """created_at of a JSONL record (ISO string) as epoch seconds. A bad value becomes 'now'."""
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return time.time()


class HandoffStore:
    """One SQLite connection, serialised with a lock (the sync API handlers run in a threadpool).
    The lock is only there to share the connection safely: correctness comes from the UPDATEs."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # deleted rows are overwritten with zeros, not just unlinked: without this the text of a
        # deleted or purged case stays readable in the file's free pages
        self.conn.execute("PRAGMA secure_delete=ON")
        self._lock = threading.Lock()
        with self._lock:
            self.conn.executescript(SCHEMA)
        self._closed = False

    @contextmanager
    def _tx(self):
        """One write transaction. BEGIN IMMEDIATE takes the write lock up front, so two processes
        do not deadlock upgrading a read lock."""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def _read(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    @staticmethod
    def _event(conn, handoff_id: str, actor: str, action: str, detail: str | None, at: float) -> None:
        conn.execute("INSERT INTO handoff_events (handoff_id, at, actor, action, detail) VALUES (?, ?, ?, ?, ?)",
                     (handoff_id, at, actor, action, detail))

    # ---- creation: this class is also a HandoffNotifier (app.handoff) ---------------------------

    def notify(self, record: dict) -> None:
        """Queue a handoff record (the dict the handoff node builds). Raises if the id already exists."""
        with self._tx() as c:
            self._insert(c, record, "created")

    def _insert(self, conn, record: dict, action: str) -> None:
        created = _epoch(record.get("created_at")) if action != "created" else time.time()
        conn.execute(
            "INSERT INTO handoffs (id, conversation_id, merchant_id, reason, priority, summary, record, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (record["handoff_id"], record.get("conversation_id") or "default", record["user_id"], record["reason"],
             record.get("priority") or "normal", record.get("case_summary") or case_summary(record),
             json.dumps(record, ensure_ascii=False), created))
        self._event(conn, record["handoff_id"], "system", action, record["reason"], time.time())

    def import_jsonl(self, path: str | Path) -> dict:
        """Load the records of the old queue file. Run it as often as you like: a record whose id is
        already here is skipped, so nothing is duplicated and a handoff somebody already worked on
        keeps its state. Everything in that file was waiting, so everything comes in as pending.
        A line that is not a usable record is counted and skipped, never fatal."""
        path = Path(path)
        result = {"imported": 0, "already_there": 0, "invalid": 0, "redacted": 0}
        if not path.exists():
            return result
        with path.open(encoding="utf-8") as f:
            lines = [ln for ln in (raw.strip() for raw in f) if ln]
        with self._tx() as c:
            for n, line in enumerate(lines, 1):
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict) and rec.get("redacted"):
                        result["redacted"] += 1  # an audit line, written without the case: nothing to work on
                        continue
                    if not all(isinstance(rec.get(k), str) and rec[k] for k in ("handoff_id", "user_id", "reason")):
                        raise ValueError("missing handoff_id, user_id or reason")
                    if c.execute("SELECT 1 FROM handoffs WHERE id = ?", (rec["handoff_id"],)).fetchone():
                        result["already_there"] += 1
                        continue
                    self._insert(c, rec, "imported")
                    result["imported"] += 1
                except (ValueError, TypeError, AttributeError) as e:
                    result["invalid"] += 1
                    log.warning("handoff import: line %d of %s skipped (%s)", n, path, e)
        if result["imported"] or result["invalid"]:
            log.info("handoff import from %s: %s", path, result)
        return result

    # ---- operators -------------------------------------------------------------------------------

    @staticmethod
    def _full(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["record"] = json.loads(d["record"])
        return d

    def get(self, handoff_id: str) -> dict | None:
        rows = self._read("SELECT * FROM handoffs WHERE id = ?", (handoff_id,))
        return self._full(rows[0]) if rows else None

    def list_queue(self, status: str | None = None, limit: int = 100) -> list[dict]:
        """Oldest first: the one that has waited longest is first."""
        if status is not None and status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        where, args = ("WHERE status = ?", (status,)) if status else ("", ())
        rows = self._read(f"SELECT {_LIST_COLUMNS} FROM handoffs {where} ORDER BY created_at ASC, rowid ASC LIMIT ?",
                          (*args, limit))
        return [dict(r) for r in rows]

    def claim(self, handoff_id: str, operator: str, now: float | None = None) -> dict:
        """pending -> claimed by `operator`. Claiming what you already hold is a no-op that returns the
        same handoff. Raises HandoffNotFound (404) or HandoffConflict (409: held by someone else, or resolved)."""
        now = time.time() if now is None else now
        with self._tx() as c:
            cur = c.execute("UPDATE handoffs SET status = 'claimed', claimed_by = ?, claimed_at = ? "
                            "WHERE id = ? AND status = 'pending'", (operator, now, handoff_id))
            if cur.rowcount == 1:  # we won
                self._event(c, handoff_id, operator, "claim", None, now)
                log.info("handoff claimed id=%s operator=%s", handoff_id, operator)
            else:
                row = c.execute("SELECT status, claimed_by FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
                if row is None:
                    raise HandoffNotFound(handoff_id)
                if not (row["status"] == "claimed" and row["claimed_by"] == operator):
                    raise HandoffConflict(
                        "Already resolved." if row["status"] == "resolved" else "Already claimed by another operator.")
            return self._full(c.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone())

    def reply(self, handoff_id: str, operator: str, decision: str, message: str, idempotency_key: str,
              now: float | None = None) -> tuple[dict, bool]:
        """claimed -> resolved, only by the operator who claimed it. The message is stored for the
        customer in the same transaction. Returns (handoff, replayed).

        The same idempotency_key again (same operator, same decision and message) returns the same
        result with replayed=True and stores nothing. The same key with other content, a different key
        on a resolved handoff, a handoff nobody claimed, or one claimed by someone else: HandoffConflict."""
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}")
        now = time.time() if now is None else now
        with self._tx() as c:
            cur = c.execute(
                "UPDATE handoffs SET status = 'resolved', resolution = ?, operator_reply = ?, resolved_at = ?, "
                "idempotency_key = ? WHERE id = ? AND status = 'claimed' AND claimed_by = ?",
                (decision, message, now, idempotency_key, handoff_id, operator))
            row = c.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
            if cur.rowcount == 1:  # we won
                c.execute("INSERT INTO operator_messages (handoff_id, merchant_id, conversation_id, decision, body, "
                          "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                          (handoff_id, row["merchant_id"], row["conversation_id"], decision, message, now))
                self._event(c, handoff_id, operator, "reply", decision, now)
                log.info("handoff resolved id=%s operator=%s decision=%s", handoff_id, operator, decision)
                return self._full(row), False
            if row is None:
                raise HandoffNotFound(handoff_id)
            if row["status"] == "resolved" and row["claimed_by"] == operator and row["idempotency_key"] == idempotency_key:
                if row["resolution"] == decision and row["operator_reply"] == message:
                    return self._full(row), True
                raise HandoffConflict("This idempotency_key was already used with a different decision or message.")
            if row["status"] == "resolved":
                raise HandoffConflict("Already resolved.")
            if row["status"] == "pending":
                raise HandoffConflict("Claim the handoff before replying.")
            raise HandoffConflict("Claimed by another operator.")

    def events(self, handoff_id: str) -> list[dict]:
        """The audit trail of one handoff, oldest first."""
        rows = self._read("SELECT at, actor, action, detail FROM handoff_events WHERE handoff_id = ? ORDER BY id",
                          (handoff_id,))
        return [dict(r) for r in rows]

    # ---- the merchant side: every query is filtered by merchant_id, the caller's token ------------

    def status_for(self, merchant_id: str, handoff_id: str) -> str | None:
        """Status of the handoff if it is this merchant's, else None (same answer for 'not yours')."""
        rows = self._read("SELECT status FROM handoffs WHERE id = ? AND merchant_id = ?", (handoff_id, merchant_id))
        return rows[0]["status"] if rows else None

    def open_for_conversation(self, merchant_id: str, conversation_id: str, limit: int = 10) -> list[dict]:
        """Handoffs of this conversation still waiting for a person: [{id, status}], oldest first."""
        rows = self._read("SELECT id, status FROM handoffs WHERE merchant_id = ? AND conversation_id = ? "
                          "AND status != 'resolved' ORDER BY created_at ASC, rowid ASC LIMIT ?",
                          (merchant_id, conversation_id, limit))
        return [dict(r) for r in rows]

    def pop_undelivered(self, merchant_id: str, conversation_id: str, now: float | None = None) -> list[dict]:
        """The operator messages of this conversation that were never delivered, oldest first, marking
        them delivered in the same transaction: two simultaneous turns cannot both receive one."""
        now = time.time() if now is None else now
        with self._tx() as c:
            rows = c.execute("SELECT id, handoff_id, decision, body, created_at FROM operator_messages "
                             "WHERE merchant_id = ? AND conversation_id = ? AND delivered_at IS NULL "
                             "ORDER BY created_at ASC, id ASC", (merchant_id, conversation_id)).fetchall()
            for r in rows:
                c.execute("UPDATE operator_messages SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
                          (now, r["id"]))
        return [{"handoff_id": r["handoff_id"], "decision": r["decision"], "message": r["body"],
                 "created_at": r["created_at"]} for r in rows]

    def messages_for_conversation(self, merchant_id: str, conversation_id: str) -> list[dict]:
        """All operator messages of the merchant's conversation, delivered or not. Read only."""
        rows = self._read("SELECT handoff_id, decision, body, created_at, delivered_at FROM operator_messages "
                          "WHERE merchant_id = ? AND conversation_id = ? ORDER BY created_at ASC, id ASC",
                          (merchant_id, conversation_id))
        return [{"handoff_id": r["handoff_id"], "decision": r["decision"], "message": r["body"],
                 "created_at": r["created_at"], "delivered": r["delivered_at"] is not None} for r in rows]

    def has_conversation(self, merchant_id: str, conversation_id: str) -> bool:
        """Did this merchant ever hand off a case in this conversation?"""
        return bool(self._read("SELECT 1 FROM handoffs WHERE merchant_id = ? AND conversation_id = ? LIMIT 1",
                               (merchant_id, conversation_id)))

    # ---- erasing (privacy) ---------------------------------------------------------------------

    def _scrub(self) -> None:
        """Fold the WAL into the main file and empty it: the pages it holds are older copies of what
        was just deleted. (With secure_delete the main file has zeros where the text was.)"""
        with self._lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                log.warning("WAL checkpoint failed after a delete", exc_info=True)

    @staticmethod
    def _erase(conn, ids: list[str]) -> None:
        """Delete these handoffs and everything that hangs from them. Children first (foreign key)."""
        for i in range(0, len(ids), 500):  # stay under SQLite's variable limit
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM operator_messages WHERE handoff_id IN ({marks})", chunk)
            conn.execute(f"DELETE FROM handoff_events WHERE handoff_id IN ({marks})", chunk)
            conn.execute(f"DELETE FROM handoffs WHERE id IN ({marks})", chunk)

    def delete_conversation(self, merchant_id: str, conversation_id: str) -> int:
        """Erase every handoff of this merchant's conversation, with its operator messages and its
        audit events, then zero the freed pages and truncate the WAL. Filtered by merchant_id, so a
        conversation_id that is somebody else's matches nothing. Returns how many handoffs went."""
        with self._tx() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM handoffs WHERE merchant_id = ? AND conversation_id = ?",
                                              (merchant_id, conversation_id))]
            self._erase(c, ids)
        if ids:
            self._scrub()
            log.info("handoffs erased with their conversation: user=%s conv=%s count=%d", merchant_id, conversation_id, len(ids))
        return len(ids)

    # ---- lifecycle -------------------------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                log.warning("WAL checkpoint failed on close", exc_info=True)
            self.conn.close()
