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
        expiry = f" or wait until old ones expire ({ttl_days} days without activity)" if ttl_days else ""
        super().__init__(
            f"You already have {limit} conversations, which is the maximum. "
            f"Continue one of them (send its conversation_id){expiry}. No conversation was deleted.")


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
        self.saver = SqliteSaver(self.conn)
        self.saver.setup()  # creates the checkpoint tables, switches to WAL
        with self.saver.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    thread_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_active REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS threads_user ON threads(user_id);
                CREATE INDEX IF NOT EXISTS threads_active ON threads(last_active);
                """
            )
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
