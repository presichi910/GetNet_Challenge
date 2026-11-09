import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas import ChatResponse, Routing


class StubService:
    def handle(self, message, user_id, thread_id=None):
        return ChatResponse(response=f"echo:{message}", user_id=user_id, agents_used=["knowledge"],
                            routing=Routing(plan=["knowledge"]), latency_ms=1, trace_id="t")


@pytest.fixture
def client():
    app.state.service = StubService()
    with TestClient(app) as c:
        yield c
    app.state.service = None


def test_chat_ok(client):
    r = client.post("/chat", json={"message": "hello", "user_id": "cliente1988"})
    assert r.status_code == 200
    body = r.json()
    assert body["response"] == "echo:hello" and body["user_id"] == "cliente1988"
    assert set(body) >= {"response", "agents_used", "routing", "sources", "tool_calls", "latency_ms", "trace_id"}


@pytest.mark.parametrize("payload", [
    {"message": "", "user_id": "u"},
    {"message": "hi"},
    {"user_id": "u"},
    {"message": "hi", "user_id": "bad id with spaces"},
    {"message": "x" * 2001, "user_id": "u"},
])
def test_chat_validation(client, payload):
    assert client.post("/chat", json=payload).status_code == 422


def test_chat_internal_error_is_sanitized(client):
    class Boom:
        def handle(self, *a, **k):
            raise RuntimeError("secret internals")

    app.state.service = Boom()
    r = client.post("/chat", json={"message": "hi", "user_id": "u"})
    assert r.status_code == 500 and "secret" not in r.text


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"
