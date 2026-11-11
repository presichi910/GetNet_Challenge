"""Issue a demo JWT for the API (HS256, same secret/issuer/audience as the app).

    python -m scripts.issue_token --user cliente1988
    python -m scripts.issue_token --user cliente1988 --scopes "chat" --minutes 60
    docker compose exec api python -m scripts.issue_token --user cliente1988

Demo only. In production the identity provider issues the tokens.
"""
from __future__ import annotations

import argparse
import time

import jwt

from app.auth import ALGORITHM, validate_jwt_secret
from app.config import get_settings


def make_token(user: str, scopes: str, minutes: float, secret: str, issuer: str, audience: str,
               now: float | None = None) -> str:
    iat = int(now if now is not None else time.time())
    claims = {"sub": user, "scope": scopes, "iss": issuer, "aud": audience,
              "iat": iat, "exp": iat + int(minutes * 60)}
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--user", required=True, help="customer id, becomes the `sub` claim (e.g. cliente1988)")
    ap.add_argument("--scopes", default="chat", help='space separated scopes (default: "chat")')
    ap.add_argument("--minutes", type=float, default=15, help="token lifetime (default: 15)")
    ap.add_argument("--url", default="http://localhost:8000", help="base URL used in the example curl")
    args = ap.parse_args(argv)

    s = get_settings()
    validate_jwt_secret(s.jwt_secret)
    token = make_token(args.user, args.scopes, args.minutes, s.jwt_secret, s.jwt_issuer, s.jwt_audience)
    print(token)
    print(f"\n# valid for {args.minutes:g} min, user={args.user}, scopes={args.scopes!r}. Try it:\n")
    print(f"curl -s -X POST {args.url}/chat \\n"
          f"  -H \"Authorization: Bearer {token}\" \\n"
          "  -H \"Content-Type: application/json\" \\n"
          "  -d '{\"message\": \"When will I receive my payout?\"}'")


if __name__ == "__main__":
    main()
