import json

from app.tools.customer_tools import make_support_tools


def tools_by_name(user_id, repo):
    return {t.name: t for t in make_support_tools(user_id, repo)}


def test_support_has_at_least_two_tools(repo):
    assert len(make_support_tools("cliente1988", repo)) >= 2


def test_payout_schedule_resolves_dates(repo):
    out = json.loads(tools_by_name("cliente1988", repo)["get_payout_schedule"].invoke({}))
    first = out[0]
    assert first["sales_date"] == "2026-01-14"      # yesterday
    assert first["expected_date"] == "2026-01-16"   # tomorrow
    assert first["status"] == "scheduled"


def test_terminal_status_offline(repo):
    out = json.loads(tools_by_name("cliente1988", repo)["get_terminal_status"].invoke({}))
    assert out["status"] == "offline" and out["signal_strength"] == "weak"


def test_declined_filter(repo):
    out = json.loads(tools_by_name("cliente1988", repo)["get_recent_transactions"].invoke({"status": "declined"}))
    assert out and all(t["status"] == "declined" for t in out)
    assert {t["decline_code"] for t in out} == {"51", "05"}


def test_unknown_user_returns_not_found(repo):
    out = json.loads(tools_by_name("ghost", repo)["get_customer_profile"].invoke({}))
    assert out["error"] == "customer_not_found"


def test_tools_are_bound_to_one_user(repo):
    """The model cannot pick a different customer: tools take no user_id argument."""
    for t in make_support_tools("cliente2024", repo):
        assert "user_id" not in t.args
    profile = json.loads(tools_by_name("cliente2024", repo)["get_customer_profile"].invoke({}))
    assert profile["name"] == "Marina Souza"


def test_create_ticket_persists(repo, tmp_path):
    out = json.loads(tools_by_name("cliente1988", repo)["create_support_ticket"].invoke(
        {"category": "terminal", "summary": "NET_TIMEOUT on Get Smart"}))
    assert out["ticket_id"].startswith("TKT-") and out["status"] == "open"
    tickets = json.loads(tools_by_name("cliente1988", repo)["get_support_tickets"].invoke({}))
    assert any(t["ticket_id"] == out["ticket_id"] for t in tickets)
    assert (tmp_path / "tickets.jsonl").read_text(encoding="utf-8").count("NET_TIMEOUT") == 1
