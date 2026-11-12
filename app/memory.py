"""Persistent conversation memory: LangGraph checkpoints in one SQLite file.

One connection shared by all request threads (FastAPI runs the sync `/chat` handler in a
threadpool, and the graph is called with `invoke`, so the sync `SqliteSaver` is the right one).
`SqliteSaver` serialises its own statements with `saver.lock`. The app runs with ONE uvicorn
worker: SQLite allows several processes, but nothing here is built for it.

The checkpoints hold the whole graph state, including `agent_outputs` and raw tool results,
that is, customer data at rest. See README, "Production hardening".
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

log = logging.getLogger("memory")


class Memory:
    """The SQLite connection and the LangGraph saver that sits on it."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the saver's lock is what keeps the threads apart
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.saver = SqliteSaver(self.conn)
        self.saver.setup()  # creates the tables, switches to WAL
        self._closed = False

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


def open_memory(path: str | Path) -> Memory:
    return Memory(path)
