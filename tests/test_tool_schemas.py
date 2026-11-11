"""No tool may let the model say WHOSE data to touch.

The customer is the authenticated JWT `sub`, bound into the tools when they are built. If a tool
ever gets a parameter like `user_id`, `customer` or `account`, the model could pick another
customer, so these tests fail. They cover every tool the agents use, found automatically.
"""
import inspect
import re

import pytest
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel

from app.tools import customer_tools, handoff_tools, knowledge_tools
from app.tools.customer_tools import make_support_tools

# a parameter whose name contains any of these is about identity
IDENTITY_WORDS = ("user", "customer", "client", "merchant", "account", "owner", "tenant", "principal",
                  "subject", "login", "email", "document", "cpf", "cnpj")
IDENTITY_NAMES = {"id", "uid", "sub", "thread", "threadid", "conversation", "conversationid"}


def parameter_names(schema) -> set[str]:
    """Every parameter name in a JSON schema, including nested objects."""
    names: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for name, sub in node.get("properties", {}).items():
                names.add(name)
                walk(sub)
            for key, sub in node.items():
                if key != "properties":
                    walk(sub)
        elif isinstance(node, list):
            for sub in node:
                walk(sub)

    walk(schema)
    return names


def identity_parameters(t: BaseTool) -> list[str]:
    bad = []
    for name in parameter_names(t.get_input_schema().model_json_schema()):
        flat = re.sub(r"[^a-z]", "", name.lower())
        if flat in IDENTITY_NAMES or any(w in flat for w in IDENTITY_WORDS):
            bad.append(name)
    return sorted(bad)


def all_agent_tools(repo) -> list[BaseTool]:
    """The support tools (built per user) plus every tool defined at module level."""
    found = list(make_support_tools("cliente1988", repo))
    for module in (customer_tools, handoff_tools, knowledge_tools):
        found += [obj for _, obj in inspect.getmembers(module, lambda o: isinstance(o, BaseTool))]
    unique = {t.name: t for t in found}
    return list(unique.values())


def test_the_tool_inventory_is_found(repo):
    names = {t.name for t in all_agent_tools(repo)}
    assert {"get_customer_profile", "get_recent_transactions", "get_payout_schedule", "get_terminal_status",
            "get_support_tickets", "create_support_ticket", "escalate_to_human", "search_getnet_kb",
            "web_search"} <= names


def test_no_tool_exposes_a_parameter_that_selects_a_user(repo):
    offenders = {t.name: identity_parameters(t) for t in all_agent_tools(repo) if identity_parameters(t)}
    assert offenders == {}, f"these tools let the model choose whose data to use: {offenders}"


# ---- the detector itself must be able to fail ----

def test_the_check_catches_user_like_parameters():
    @tool
    def by_user_id(user_id: str) -> str:
        """x"""
        return ""

    @tool
    def by_customer(customer: str) -> str:
        """x"""
        return ""

    @tool
    def by_merchant_id(merchantId: str) -> str:
        """x"""
        return ""

    @tool
    def by_account(account_number: str, limit: int = 5) -> str:
        """x"""
        return ""

    @tool
    def by_plain_id(id: str) -> str:
        """x"""
        return ""

    class Target(BaseModel):
        owner_email: str

    @tool
    def nested(target: Target) -> str:
        """x"""
        return ""

    for t in (by_user_id, by_customer, by_merchant_id, by_account, by_plain_id, nested):
        assert identity_parameters(t), f"{t.name} should have been flagged"


def test_the_check_accepts_normal_parameters():
    @tool
    def fine(query: str, limit: int = 5, topic: str = "general", status: str = "all") -> str:
        """x"""
        return ""

    assert identity_parameters(fine) == []
