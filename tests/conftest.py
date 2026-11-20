import functools
import json
import time
import uuid
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


# ---- auth helpers: RSA keys generated here and a fake identity provider. No network, no real keys. ----
TEST_ISSUER = "https://idp.test/realms/merchants"  # the merchant side
TEST_AUDIENCE = "getnet-support-api"
OPERATOR_ISSUER = "https://idp.test/realms/operators"  # the operator side: another issuer, audience and keys
OPERATOR_AUDIENCE = "getnet-operator-api"
MERCHANT_JWKS_URL = "https://idp.test/realms/merchants/certs"
OPERATOR_JWKS_URL = "https://idp.test/realms/operators/certs"
KIDS = {"merchants": "merchant-key-1", "operators": "operator-key-1"}
_DEFAULT = object()


@functools.lru_cache(maxsize=None)
def rsa_key(name: str):
    """A private key per name, generated once per test run: "merchants", "operators", "other" (an attacker's), "rotated"."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk_of(name: str, kid: str, **extra) -> dict:
    import jwt

    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key(name).public_key()))
    return {**d, "kid": kid, "use": "sig", "alg": "RS256", **extra}


class FakeIdp:
    """What the identity provider publishes. A test can rotate a key, add keys, take it down and count the fetches."""

    def __init__(self):
        self.published = {MERCHANT_JWKS_URL: [("merchant-key-1", "merchants")], OPERATOR_JWKS_URL: [("operator-key-1", "operators")]}
        self.extra: dict[str, list[dict]] = {}
        self.fetches: list[str] = []
        self.down = False

    def fetch(self, url: str, timeout: float) -> dict:
        self.fetches.append(url)
        if self.down:
            raise OSError("identity provider unreachable")
        return {"keys": [jwk_of(name, kid) for kid, name in self.published[url]] + self.extra.get(url, [])}


@pytest.fixture
def auth_env(monkeypatch):
    """App settings for two issuers (merchants, operators) and a fake IdP behind their JWKS URLs."""
    from app import auth, jwks
    from app.config import get_settings

    for k, v in {"JWKS_URL": MERCHANT_JWKS_URL, "JWT_ISSUER": TEST_ISSUER, "JWT_AUDIENCE": TEST_AUDIENCE,
                 "OPERATOR_JWKS_URL": OPERATOR_JWKS_URL, "OPERATOR_JWT_ISSUER": OPERATOR_ISSUER,
                 "OPERATOR_JWT_AUDIENCE": OPERATOR_AUDIENCE}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    idp = FakeIdp()
    monkeypatch.setattr(jwks, "fetch_jwks", idp.fetch)
    auth.reset_auth_state()
    yield idp
    get_settings.cache_clear()
    auth.reset_auth_state()


def make_jwt(user: str = "cliente1988", scope: str | None = "chat", *, key=_DEFAULT, algorithm: str = "RS256",
             minutes: float = 15, drop: tuple[str, ...] = (), realm: str = "merchants", kid: str | None = _DEFAULT,
             headers: dict | None = None, drop_headers: tuple[str, ...] = (), **overrides) -> str:
    """Build a token signed by the realm's key. Valid by default.
    `drop` removes claims, `overrides` replaces them, `key` signs with another key, `kid`/`headers`/`drop_headers` shape the header."""
    import jwt

    now = int(time.time())
    iss, aud = (TEST_ISSUER, TEST_AUDIENCE) if realm == "merchants" else (OPERATOR_ISSUER, OPERATOR_AUDIENCE)
    claims = {"sub": user, "iss": iss, "aud": aud, "iat": now, "exp": now + int(minutes * 60), "jti": uuid.uuid4().hex}
    if scope is not None:
        claims["scope"] = scope
    claims.update(overrides)
    for k in drop:
        claims.pop(k, None)
    signing = rsa_key(realm) if key is _DEFAULT else key
    header = {"kid": KIDS[realm] if kid is _DEFAULT else kid, **(headers or {})}
    for h in drop_headers:
        header.pop(h, None)
    header = {k: v for k, v in header.items() if v is not None}
    return jwt.encode(claims, signing, algorithm=algorithm, headers=header)


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def operator_jwt(operator: str = "ops1", scope: str | None = "operator", **kw) -> str:
    """A token for support staff: signed by the operator issuer, for the operator audience."""
    return make_jwt(operator, scope, realm="operators", **kw)


@pytest.fixture(autouse=True)
def _conversation_ids_lenient(request, monkeypatch):
    """In production `/chat` rejects a conversation_id the server did not give the caller (404). The many tests that open a
    conversation with a name of their own are about other things, so they run with that one check off; tests/test_conversation_ids.py
    marks itself `strict_ids` and exercises the real rule."""
    if request.node.get_closest_marker("strict_ids"):
        return
    from app.service import ChatService

    monkeypatch.setattr(ChatService, "conversation_known", lambda self, user_id, conversation_id: True)


@pytest.fixture(autouse=True)
def _isolated_traces(tmp_path, monkeypatch):
    """No test ever writes the real ./data/traces.sqlite (the API lifespan opens TRACES_DB_PATH) nor the real
    ./data/rag_cache.sqlite: a test that embeds with a fake model would leave a FAKE vector there, and the next real search
    for the same text would read it back. Tests get an empty path (no cache) unless they build their own `DiskCache`."""
    from app.config import get_settings
    from app.rag.hybrid import get_cache

    monkeypatch.setenv("TRACES_DB_PATH", str(tmp_path / "traces.sqlite"))
    monkeypatch.setenv("RAG_CACHE_PATH", "")
    get_settings.cache_clear()
    get_cache.cache_clear()
    yield
    get_settings.cache_clear()
    get_cache.cache_clear()


@pytest.fixture(autouse=True)
def _cost_guard_never_reaches_the_network_or_the_real_ledger(tmp_path, monkeypatch):
    """The cost guard (eval/cost_guard.py) reads the REAL key's counter and the public price listing, and writes eval/reports/cost_ledger.json. A test that runs `run_eval` or
    `rejudge` without `--no-cost-guard` did exactly that once (it wrote two entries into the real ledger and slowed the suite): now it fails loudly instead. The tests of the guard
    itself inject their own reader, fetcher and paths."""
    from eval import cost_guard

    def refuse(*_a, **_k):
        raise AssertionError("a test reached the OpenRouter key counter or price listing: pass --no-cost-guard, or inject read_key / fetch")

    monkeypatch.setattr(cost_guard, "read_key_usage", refuse)
    monkeypatch.setattr(cost_guard, "fetch_endpoints", refuse)
    monkeypatch.setattr(cost_guard, "LEDGER_JSON", tmp_path / "cost_ledger.json")
    monkeypatch.setattr(cost_guard, "LEDGER_MD", tmp_path / "COST_LEDGER.md")
