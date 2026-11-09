"""Tools for the Customer Support Agent.

`make_support_tools(user_id, repo)` returns tools *already bound* to one customer. The LLM never
chooses which user_id to query, so a prompt-injection ("look up cliente2024 instead") cannot leak
another customer's data. This is the tenant-isolation boundary.
"""
from __future__ import annotations

import json
from typing import Literal

from langchain_core.tools import BaseTool, tool

from app.data.repository import CustomerRepository

NOT_FOUND = json.dumps({"error": "customer_not_found", "message": "No account data was found for this user."})


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def make_support_tools(user_id: str, repo: CustomerRepository) -> list[BaseTool]:
    @tool
    def get_customer_profile() -> str:
        """Get the customer's profile: name, business, plan, account status, linked bank account,
        whether Pix and receivables advance are enabled."""
        profile = repo.get_profile(user_id)
        return _dump(profile) if profile else NOT_FOUND

    @tool
    def get_recent_transactions(limit: int = 10, status: Literal["approved", "declined", "all"] = "all") -> str:
        """Get the customer's most recent sales/transactions (newest first). Declined transactions
        include a decline code and reason. Use status='declined' to focus on declines."""
        txs = repo.get_transactions(user_id, limit=limit, status=None if status == "all" else status)
        return _dump(txs) if txs is not None else NOT_FOUND

    @tool
    def get_payout_schedule() -> str:
        """Get the customer's payouts (deposits/settlements): which sales each payout covers, net
        amount after fees, expected deposit date and status (scheduled / processing / paid).
        Use this to answer 'when will I receive the money for my sales?'."""
        payouts = repo.get_payouts(user_id)
        return _dump(payouts) if payouts is not None else NOT_FOUND

    @tool
    def get_terminal_status() -> str:
        """Get the customer's card machine (terminal) status: online/offline, connection type,
        signal strength, minutes since last seen, last error, firmware and battery."""
        terminal = repo.get_terminal(user_id)
        return _dump(terminal) if terminal else NOT_FOUND

    @tool
    def get_support_tickets() -> str:
        """List the customer's existing support tickets and their status."""
        tickets = repo.get_tickets(user_id)
        return _dump(tickets) if tickets is not None else NOT_FOUND

    @tool
    def create_support_ticket(category: Literal["terminal", "payout", "billing", "account", "other"], summary: str) -> str:
        """Open a support ticket for a human agent/technician. Only use when the issue cannot be
        solved with the available data or the customer asks for it. `summary` must be a concise,
        self-contained description of the problem (include codes/errors found)."""
        ticket = repo.create_ticket(user_id, category, summary)
        return _dump(ticket) if ticket else NOT_FOUND

    return [
        get_customer_profile,
        get_recent_transactions,
        get_payout_schedule,
        get_terminal_status,
        get_support_tickets,
        create_support_ticket,
    ]
