import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas import ChatResponse, Routing
from tests.conftest import bearer, make_jwt


class StubService:
    def handle(self, message, user_id, conversation_id=None):
        return ChatResponse(response=f"echo:{message}", user_id=user_id, conversation_id=conversation_id or "default",
                            agents_used=["knowledge"], routing=Routing(plan=["knowledge"]), latency_ms=1, trace_id="t")


@pytest.fixture
def client(auth_env):
    app.state.service = StubService()
    with TestClient(app) as c:
        yield c
    app.state.service = None


@pytest.fixture
def auth():
    return bearer(make_jwt("cliente1988"))


def test_chat_ok(client, auth):
    r = client.post("/chat", json={"message": "hello"}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["response"] == "echo:hello" and body["user_id"] == "cliente1988"
    assert set(body) >= {"response", "agents_used", "routing", "sources", "tool_calls", "latency_ms", "trace_id"}


def test_chat_accepts_the_original_contract_when_user_id_matches_the_token(client, auth):
    r = client.post("/chat", json={"message": "hello", "user_id": "cliente1988"}, headers=auth)
    assert r.status_code == 200 and r.json()["user_id"] == "cliente1988"


@pytest.mark.parametrize("payload", [
    {"message": ""},
    {},
    {"user_id": "cliente1988"},
    {"message": "hi", "user_id": "bad id with spaces"},
    {"message": "hi", "conversation_id": "bad id"},
    {"message": "hi", "conversation_id": "other:thread"},
    {"message": "hi", "conversation_id": "x" * 65},
    {"message": "x" * 2001},
])
def test_chat_validation(client, auth, payload):
    assert client.post("/chat", json=payload, headers=auth).status_code == 422


def test_chat_internal_error_is_sanitized(client, auth):
    class Boom:
        def handle(self, *a, **k):
            raise RuntimeError("secret internals")

    app.state.service = Boom()
    r = client.post("/chat", json={"message": "hi"}, headers=auth)
    assert r.status_code == 500 and "secret" not in r.text


def test_health_is_public(client):
    assert client.get("/health").json()["status"] == "ok"
