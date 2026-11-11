from datetime import date

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.data.repository import CustomerRepository


@pytest.fixture
def repo(tmp_path):
    return CustomerRepository(tickets_file=tmp_path / "tickets.jsonl", today=date(2026, 1, 15))


class ToolFakeModel(GenericFakeChatModel):
    """Fake chat model that accepts `bind_tools`."""

    def bind_tools(self, tools, **kwargs):
        return self


def scripted(*messages: AIMessage) -> ToolFakeModel:
    return ToolFakeModel(messages=iter(messages))


# ---- auth helpers (no network, no real keys) ----
TEST_SECRET = "k3Jf9vQx2LmT7pRz8WnB4sYd6HcA1eUg0XoVtZiN"  # passes validate_jwt_secret
TEST_ISSUER = "test-issuer"
TEST_AUDIENCE = "test-audience"


@pytest.fixture
def auth_env(monkeypatch):
    """App settings with a known JWT secret/issuer/audience."""
    from app.config import get_settings

    monkeypatch.setenv("JWT_SECRET", TEST_SECRET)
    monkeypatch.setenv("JWT_ISSUER", TEST_ISSUER)
    monkeypatch.setenv("JWT_AUDIENCE", TEST_AUDIENCE)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def make_jwt(user: str = "cliente1988", scope: str | None = "chat", *, key: str = TEST_SECRET,
             algorithm: str = "HS256", minutes: float = 15, drop: tuple[str, ...] = (), **overrides) -> str:
    """Build a token. `drop` removes claims, `overrides` replaces them. Valid by default."""
    import time

    import jwt

    now = int(time.time())
    claims = {"sub": user, "iss": TEST_ISSUER, "aud": TEST_AUDIENCE, "iat": now, "exp": now + int(minutes * 60)}
    if scope is not None:
        claims["scope"] = scope
    claims.update(overrides)
    for k in drop:
        claims.pop(k, None)
    return jwt.encode(claims, key, algorithm=algorithm)


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
