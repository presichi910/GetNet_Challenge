"""Customer data (mock). Swap this class for a real CRM and nothing else changes."""
from __future__ import annotations

import json
import threading
import uuid
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DATA_FILE = Path(__file__).with_name("customers.json")


class CustomerRepository:
    def __init__(self, data_file: Path | None = None, tickets_file: str | Path | None = None,
                 today: date | None = None):
        raw = json.loads(Path(data_file or DATA_FILE).read_text(encoding="utf-8"))
        self._data = {k: v for k, v in raw.items() if not k.startswith("_")}
        self._today = today or date.today()
        self._tickets_file = Path(tickets_file) if tickets_file else None
        self._lock = threading.Lock()

    # helpers
    def _iso(self, offset_days: int) -> str:
        return (self._today + timedelta(days=offset_days)).isoformat()

    def exists(self, user_id: str) -> bool:
        return user_id in self._data

    # reads
    def get_profile(self, user_id: str) -> dict | None:
        c = self._data.get(user_id)
        return deepcopy(c["profile"]) if c else None

    def get_terminal(self, user_id: str) -> dict | None:
        c = self._data.get(user_id)
        return deepcopy(c["terminal"]) if c else None

    def get_transactions(self, user_id: str, limit: int = 10, status: str | None = None) -> list[dict] | None:
        c = self._data.get(user_id)
        if not c:
            return None
        out = []
        for t in c["transactions"]:
            if status and t["status"] != status:
                continue
            t = deepcopy(t)
            t["date"] = self._iso(-t.pop("days_ago"))
            out.append(t)
        return out[:limit]

    def get_payouts(self, user_id: str) -> list[dict] | None:
        c = self._data.get(user_id)
        if not c:
            return None
        out = []
        for p in c["payouts"]:
            p = deepcopy(p)
            p["sales_date"] = self._iso(p.pop("sales_day_offset"))
            days = p.pop("expected_in_days")
            p["expected_date"] = self._iso(days)
            p["days_until_deposit"] = days  # so the model doesn't do date math
            out.append(p)
        return out

    def get_tickets(self, user_id: str) -> list[dict] | None:
        c = self._data.get(user_id)
        if not c:
            return None
        out = []
        for t in c["tickets"]:
            t = deepcopy(t)
            t["opened_on"] = self._iso(-t.pop("days_ago"))
            out.append(t)
        return out

    # writes
    def create_ticket(self, user_id: str, category: str, summary: str) -> dict | None:
        c = self._data.get(user_id)
        if not c:
            return None
        ticket = {
            "ticket_id": f"TKT-{uuid.uuid4().hex[:6].upper()}",
            "category": category,
            "summary": summary,
            "status": "open",
            "days_ago": 0,
        }
        with self._lock:
            c["tickets"].append(ticket)
            if self._tickets_file:
                self._tickets_file.parent.mkdir(parents=True, exist_ok=True)
                record = {**ticket, "user_id": user_id,
                          "created_at": datetime.now(timezone.utc).isoformat()}
                with self._tickets_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {**ticket, "opened_on": self._iso(0)}
