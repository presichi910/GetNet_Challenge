"""Authentication: who is calling.

The API trusts a signed JWT (HS256) and nothing else. The `sub` claim is the customer id
(`user_id`), and it is what the tools, the memory thread, the tickets and the logs use.
The LLM never sees it or chooses it.

    401  no token / bad signature / expired / wrong iss, aud / malformed
    403  valid token but the required scope is missing

In production use an identity provider with RS256 + JWKS instead of a shared secret.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings
from app.schemas import USER_ID_CHARS, USER_ID_MAX

log = logging.getLogger("auth")

ALGORITHM = "HS256"  # fixed on purpose: the algorithm never comes from the token
REQUIRED_CLAIMS = ["exp", "iss", "aud", "sub"]
MIN_SECRET_LENGTH = 32
MIN_DISTINCT_CHARS = 10
# if the secret contains any of these it is an example value somebody forgot to replace
PLACEHOLDER_WORDS = ("change", "replace", "placeholder", "example", "your", "secret", "password", "default", "todo")

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller."""

    user_id: str
    scopes: frozenset[str]


def validate_jwt_secret(secret: str) -> None:
    """Refuse to run with a missing, short or placeholder secret."""
    problem = None
    lowered = secret.lower()
    if not secret:
        problem = "is empty"
    elif len(secret) < MIN_SECRET_LENGTH:
        problem = f"is shorter than {MIN_SECRET_LENGTH} characters"
    elif any(w in lowered for w in PLACEHOLDER_WORDS):
        problem = "looks like a placeholder"
    elif len(set(secret)) < MIN_DISTINCT_CHARS:
        problem = "is too repetitive"
    if problem:
        raise RuntimeError(
            f"JWT_SECRET {problem}. Generate one with: "
            "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )


def _unauthorized(reason: str) -> HTTPException:
    # generic message for the client, the real reason goes to the log (never the token)
    log.warning("auth rejected: %s", reason)
    return HTTPException(status_code=401, detail="Invalid or missing credentials.",
                         headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})


def decode_token(token: str) -> Principal:
    """Verify signature, exp, iss and aud and build the Principal. Raises HTTPException 401."""
    s = get_settings()
    validate_jwt_secret(s.jwt_secret)  # an empty key would make forged tokens valid
    try:
        claims = jwt.decode(token, s.jwt_secret, algorithms=[ALGORITHM], issuer=s.jwt_issuer,
                            audience=s.jwt_audience, options={"require": REQUIRED_CLAIMS})
    except jwt.PyJWTError as e:
        raise _unauthorized(type(e).__name__) from e
    sub = claims["sub"]
    if not isinstance(sub, str) or len(sub) > USER_ID_MAX or not re.fullmatch(USER_ID_CHARS, sub):
        raise _unauthorized("invalid sub")
    scope = claims.get("scope", "")  # OAuth style: "chat operator"
    if not isinstance(scope, str):
        raise _unauthorized("invalid scope claim")
    return Principal(user_id=sub, scopes=frozenset(scope.split()))


def get_principal(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    if creds is None:
        raise _unauthorized("missing bearer token")
    return decode_token(creds.credentials)


def require_scope(scope: str) -> Callable[[Principal], Principal]:
    """Dependency: authenticated AND allowed to do `scope`."""

    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if scope not in principal.scopes:
            log.warning("auth forbidden: user=%s missing scope=%s", principal.user_id, scope)
            raise HTTPException(status_code=403, detail=f"Missing required scope: {scope}.")
        return principal

    return dependency
