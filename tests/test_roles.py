"""Roles: a merchant is not an operator and an operator is not a merchant. No network, no keys."""
import pytest
from fastapi.testclient import TestClient

from app.auth import Principal, decode_token
from app.main import app
from app.schemas import ChatResponse, Routing
from fastapi import HTTPException
from scripts.issue_token import main as issue_token_main
from tests.conftest import TEST_AUDIENCE, TEST_ISSUER, TEST_SECRET, bearer, make_jwt, operator_jwt


class StubService:
    def __init__(self):
        self.calls = 0

    def handle(self, message, user_id, conversation_id=None):
        self.calls += 1
        return ChatResponse(response="ok", user_id=user_id, agents_used=[], routing=Routing(plan=[]),
                            latency_ms=1, trace_id="t")

    def list_conversations(self, user_id):
        self.calls += 1
        return []

    def conversation_limit(self):
        return None

    def get_conversation(self, user_id, conversation_id):
        self.calls += 1
        return None

    def delete_conversation(self, user_id, conversation_id):
        self.calls += 1
        return False


@pytest.fixture
def svc():
    return StubService()


@pytest.fixture
def client(auth_env, svc):
    app.state.service = svc
    with TestClient(app) as c:
        yield c
    app.state.service = None


def test_a_token_without_a_role_claim_is_a_merchant(auth_env):
    assert decode_token(make_jwt("cliente1988")) == Principal("cliente1988", frozenset({"chat"}), "merchant")


def test_the_role_claim_is_read(auth_env):
    p = decode_token(operator_jwt("ops1"))
    assert (p.user_id, p.role, p.scopes) == ("ops1", "operator", frozenset({"operator"}))


@pytest.mark.parametrize("role", ["admin", "OPERATOR", "", " operator", ["operator"], 1, None])
def test_an_unknown_role_is_401_never_a_privilege(auth_env, role):
    with pytest.raises(HTTPException) as e:
        decode_token(make_jwt("ops1", "operator", role=role))
    assert e.value.status_code == 401


@pytest.mark.parametrize("scope", ["operator", "chat", "chat operator"])
def test_an_operator_can_not_use_chat_whatever_its_scopes(client, svc, scope):
    r = client.post("/chat", json={"message": "hi"}, headers=bearer(operator_jwt(scope=scope)))
    assert r.status_code == 403 and svc.calls == 0  # refused before anything ran


@pytest.mark.parametrize("method, path", [("GET", "/conversations"), ("GET", "/conversations/c1"),
                                           ("DELETE", "/conversations/c1")])
def test_an_operator_can_not_use_the_conversation_endpoints_either(client, svc, method, path):
    r = client.request(method, path, headers=bearer(operator_jwt(scope="chat operator")))
    assert r.status_code == 403 and svc.calls == 0


def test_a_merchant_still_uses_chat_even_with_the_operator_scope(client):
    r = client.post("/chat", json={"message": "hi"}, headers=bearer(make_jwt("cliente1988", "chat operator")))
    assert r.status_code == 200  # the scope gives a merchant nothing extra, see test_operator_api


def test_issue_token_makes_an_operator_token(auth_env, capsys):
    issue_token_main(["--role", "operator", "--operator", "ops1"])
    out = capsys.readouterr().out
    p = decode_token(out.splitlines()[0])
    assert (p.user_id, p.role, p.scopes) == ("ops1", "operator", frozenset({"operator"}))
    assert "/operator/handoffs" in out and "/chat" not in out


def test_issue_token_always_writes_the_role(auth_env, capsys):
    import jwt

    issue_token_main(["--user", "cliente1988"])
    claims = jwt.decode(capsys.readouterr().out.splitlines()[0], TEST_SECRET, algorithms=["HS256"],
                        audience=TEST_AUDIENCE, issuer=TEST_ISSUER)
    assert claims["role"] == "merchant"


@pytest.mark.parametrize("argv", [["--role", "operator", "--user", "x"], ["--role", "operator"],
                                  ["--operator", "ops1"], ["--user", "x", "--operator", "ops1"], []])
def test_issue_token_refuses_mixed_up_arguments(auth_env, argv):
    with pytest.raises(SystemExit):
        issue_token_main(argv)
