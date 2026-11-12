"""Persistent conversation memory: LangGraph checkpoints in one SQLite file, plus a thread registry.

One connection shared by all request threads (FastAPI runs the sync `/chat` handler in a
threadpool, and the graph is called with `invoke`, so the sync `SqliteSaver` is the right one).
`SqliteSaver` serialises its own statements with `saver.lock`, a plain (not re-entrant) lock:
the registry uses the same lock and NEVER calls a saver method while holding it.
The app runs with ONE uvicorn worker: SQLite allows several processes, but nothing here is built
for it.

The registry (table `threads`: thread_id, owner, created_at, last_active) exists because the
checkpoints carry neither an owner nor a date. It gives the three things retention needs:
who owns a thread, how many threads a user has, and when a thread was last used.

The checkpoints hold the whole graph state, including `agent_outputs` and raw tool results,
that is, customer data at rest. See README, "Production hardening".
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

log = logging.getLogger("memory")

DAY = 86400


class ThreadLimitExceeded(Exception):
    """The user already has the maximum number of conversations. Nothing was deleted."""

    def __init__(self, user_id: str, limit: int, ttl_days: int):
        self.user_id, self.limit, self.ttl_days = user_id, limit, ttl_days
        expiry = f", or wait until old ones expire ({ttl_days} days without activity)" if ttl_days else ""
        super().__init__(
            f"You already have {limit} conversations, which is the maximum. "
            f"Continue one of them (send its conversation_id), delete one you no longer need "
            f"(DELETE /conversations/{{conversation_id}}){expiry}. No conversation was deleted.")


class ThreadOwnershipError(PermissionError):
    """A thread is being used by someone who does not own it. The API never builds such a key
    (thread ids start with the authenticated user), so this is a last line of defence."""


class Memory:
    """The SQLite connection, the LangGraph saver that sits on it, and the thread registry.

    `ttl_days` 0 = threads never expire. `max_threads_per_user` 0 = no limit."""

    def __init__(self, path: str | Path, *, ttl_days: int = 0, max_threads_per_user: int = 0):
        self.path = Path(path)
        self.ttl_days = ttl_days
        self.max_threads = max_threads_per_user
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the saver's lock is what keeps the threads apart
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        # deleted rows are overwritten with zeros, not just unlinked: without this the text of a
        # deleted or purged conversation stays readable in the file's free pages
        self.conn.execute("PRAGMA secure_delete=ON")
        self.saver = SqliteSaver(self.conn)
        self.saver.setup()  # creates the checkpoint tables, switches to WAL
        with self.saver.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    thread_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_active REAL NOT NULL,
                    turns INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS threads_user ON threads(user_id);
                CREATE INDEX IF NOT EXISTS threads_active ON threads(last_active);
                """
            )
            if "turns" not in {r[1] for r in self.conn.execute("PRAGMA table_info(threads)")}:
                self.conn.execute("ALTER TABLE threads ADD COLUMN turns INTEGER NOT NULL DEFAULT 0")  # older file
                self.conn.commit()
        self._closed = False

    # ---- registry -------------------------------------------------------------------------

    def begin_turn(self, user_id: str, thread_id: str, now: float | None = None) -> bool:
        """Register (or refresh) the thread of a turn, atomically. Returns True if the thread is new.

        Raises ThreadLimitExceeded if it would be a new thread above the limit. It never deletes
        anything to make room."""
        now = time.time() if now is None else now
        if not thread_id.startswith(f"{user_id}:"):
            raise ThreadOwnershipError(f"thread {thread_id!r} does not belong to {user_id!r}")
        with self.saver.lock:
            row = self.conn.execute("SELECT user_id FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
            if row:
                if row[0] != user_id:
                    raise ThreadOwnershipError(f"thread {thread_id!r} belongs to another user")
                self.conn.execute("UPDATE threads SET last_active = ? WHERE thread_id = ?", (now, thread_id))
                self.conn.commit()
                return False
            if self.max_threads:
                (n,) = self.conn.execute("SELECT COUNT(*) FROM threads WHERE user_id = ?", (user_id,)).fetchone()
                if n >= self.max_threads:
                    log.warning("thread limit reached user=%s threads=%d limit=%d", user_id, n, self.max_threads)
                    raise ThreadLimitExceeded(user_id, self.max_threads, self.ttl_days)
            self.conn.execute("INSERT INTO threads (thread_id, user_id, created_at, last_active) VALUES (?, ?, ?, ?)",
                              (thread_id, user_id, now, now))
            self.conn.commit()
            return True

    def abort_new_thread(self, thread_id: str) -> None:
        """The very first turn of a thread failed: free its slot and drop whatever it wrote."""
        with self.saver.lock:
            self.conn.execute("DELETE FROM threads WHERE thread_id = ?", (thread_id,))
            self.conn.commit()
        self.saver.delete_thread(thread_id)

    def prune_thread(self, thread_id: str) -> None:
        """Keep only the latest checkpoint of the thread. LangGraph writes several per turn and
        keeps them all; without this the table grows with every turn, and the messages that the
        rolling summary removed would stay readable in the older rows.

        SqliteSaver.prune is not implemented in langgraph-checkpoint-sqlite 3.1.3 (it raises
        NotImplementedError), so this is plain SQL. Checkpoint ids are uuid6, so the largest id is
        the newest one (it is how the saver itself orders them)."""
        with self.saver.lock:
            self.conn.execute(
                "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_id < ("
                " SELECT MAX(c.checkpoint_id) FROM checkpoints c"
                " WHERE c.thread_id = checkpoints.thread_id AND c.checkpoint_ns = checkpoints.checkpoint_ns)",
                (thread_id,))
            self.conn.execute(
                "DELETE FROM writes WHERE thread_id = ? AND NOT EXISTS ("
                " SELECT 1 FROM checkpoints c WHERE c.thread_id = writes.thread_id"
                " AND c.checkpoint_ns = writes.checkpoint_ns AND c.checkpoint_id = writes.checkpoint_id)",
                (thread_id,))
            self.conn.commit()

    def complete_turn(self, thread_id: str) -> None:
        """A turn answered: count it. (begin_turn can not, the turn may still fail.)"""
        with self.saver.lock:
            self.conn.execute("UPDATE threads SET turns = turns + 1 WHERE thread_id = ?", (thread_id,))
            self.conn.commit()

    def list_threads(self, user_id: str) -> list[dict]:
        """The user's own threads, most recent first. Metadata only, never content."""
        with self.saver.lock:
            rows = self.conn.execute(
                "SELECT thread_id, created_at, last_active, turns FROM threads "
                "WHERE user_id = ? ORDER BY last_active DESC, thread_id", (user_id,)).fetchall()
        prefix = f"{user_id}:"
        return [{"conversation_id": t[len(prefix):], "created_at": c, "last_active": a, "turns": n}
                for t, c, a, n in rows]

    def get_thread(self, user_id: str, thread_id: str) -> dict | None:
        """Metadata of the thread if it exists AND is the user's. Anything else is None, so callers
        can not tell a missing thread from somebody else's."""
        with self.saver.lock:
            row = self.conn.execute(
                "SELECT created_at, last_active, turns FROM threads WHERE thread_id = ? AND user_id = ?",
                (thread_id, user_id)).fetchone()
        if not row or not thread_id.startswith(f"{user_id}:"):
            return None
        return {"conversation_id": thread_id[len(user_id) + 1:], "created_at": row[0],
                "last_active": row[1], "turns": row[2]}

    def delete_thread_for(self, user_id: str, thread_id: str) -> bool:
        """Delete the user's thread: registry row (frees its slot against the limit), checkpoints and
        writes. False, and nothing touched, if it does not exist or is not the user's."""
        with self.saver.lock:
            row = self.conn.execute("SELECT user_id FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
            if not row or row[0] != user_id or not thread_id.startswith(f"{user_id}:"):
                return False
            self.conn.execute("DELETE FROM threads WHERE thread_id = ?", (thread_id,))
            self.conn.commit()
        self.saver.delete_thread(thread_id)
        self._scrub()
        return True

    def _scrub(self) -> None:
        """Fold the WAL into the main file and empty it: the pages it holds are older copies of what
        was just deleted. (With secure_delete the main file has zeros where the text was.)"""
        with self.saver.lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                log.warning("WAL checkpoint failed after a delete", exc_info=True)

    def thread_count(self, user_id: str) -> int:
        with self.saver.lock:
            return self.conn.execute("SELECT COUNT(*) FROM threads WHERE user_id = ?", (user_id,)).fetchone()[0]

    def owner_of(self, thread_id: str) -> str | None:
        with self.saver.lock:
            row = self.conn.execute("SELECT user_id FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
        return row[0] if row else None

    # ---- retention ------------------------------------------------------------------------

    def purge_expired(self, now: float | None = None) -> int:
        """Delete, by thread_id, every thread idle for more than `ttl_days`, with its checkpoints.
        Also deletes checkpoints that have no registry row (an interrupted purge or abort): they
        would hold customer data with no owner and no expiry. Returns how many threads went."""
        if not self.ttl_days or self._closed:
            return 0
        now = time.time() if now is None else now
        cutoff = now - self.ttl_days * DAY
        try:
            with self.saver.lock:
                expired = [r[0] for r in self.conn.execute(
                    "SELECT thread_id FROM threads WHERE last_active < ?", (cutoff,))]
            purged = 0
            for thread_id in expired:
                with self.saver.lock:
                    # re-check: the customer may have written again since the SELECT
                    cur = self.conn.execute("DELETE FROM threads WHERE thread_id = ? AND last_active < ?",
                                            (thread_id, cutoff))
                    self.conn.commit()
                    gone = cur.rowcount == 1
                if gone:
                    self.saver.delete_thread(thread_id)
                    purged += 1
            with self.saver.lock:
                orphans = [r[0] for r in self.conn.execute(
                    "SELECT thread_id FROM checkpoints WHERE thread_id NOT IN (SELECT thread_id FROM threads) "
                    "UNION SELECT thread_id FROM writes WHERE thread_id NOT IN (SELECT thread_id FROM threads)")]
            for thread_id in orphans:
                self.saver.delete_thread(thread_id)
        except sqlite3.ProgrammingError:  # closed while we ran (shutdown)
            return 0
        if purged or orphans:
            self._scrub()
            log.info("retention: purged %d expired threads (> %d days idle) and %d orphans",
                     purged, self.ttl_days, len(orphans))
        return purged + len(orphans)

    # ---- lifecycle ------------------------------------------------------------------------

    def close(self) -> None:
        """Flush the WAL into the main file and close. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        with self.saver.lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                log.warning("WAL checkpoint failed on close", exc_info=True)
            self.conn.close()
        log.info("memory closed: %s", self.path)


def open_memory(path: str | Path, **limits) -> Memory:
    return Memory(path, **limits)
