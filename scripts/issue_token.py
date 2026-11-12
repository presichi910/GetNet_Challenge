"""Issue a demo JWT for the API (HS256, same secret/issuer/audience as the app).

    python -m scripts.issue_token --user cliente1988
    python -m scripts.issue_token --user cliente1988 --scopes "chat" --minutes 60
    docker compose exec api python -m scripts.issue_token --user cliente1988
    python -m scripts.issue_token --role operator --operator ops1      # for /operator/*

Demo only. In production the identity provider issues the tokens.
"""
from __future__ import annotations

import argparse
import time

import jwt

from app.auth import ALGORITHM, validate_jwt_secret
from app.config import get_settings


def make_token(user: str, scopes: str, minutes: float, secret: str, issuer: str, audience: str,
               now: float | None = None, role: str = "merchant") -> str:
    iat = int(now if now is not None else time.time())
    claims = {"sub": user, "scope": scopes, "role": role, "iss": issuer, "aud": audience,
              "iat": iat, "exp": iat + int(minutes * 60)}
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--role", choices=("merchant", "operator"), default="merchant",
                    help="merchant (default) uses /chat; operator uses /operator/* and can not use /chat")
    ap.add_argument("--user", help="merchant id, becomes the `sub` claim (e.g. cliente1988). Role merchant only")
    ap.add_argument("--operator", help="operator id, becomes the `sub` claim (e.g. ops1). Role operator only")
    ap.add_argument("--scopes", default=None, help='space separated scopes (default: "chat", or "operator" for an operator)')
    ap.add_argument("--minutes", type=float, default=15, help="token lifetime (default: 15)")
    ap.add_argument("--url", default="http://localhost:8000", help="base URL used in the example curl")
    args = ap.parse_args(argv)
    if args.role == "operator":
        if not args.operator or args.user:
            ap.error("--role operator needs --operator <id> and takes no --user")
        subject, scopes = args.operator, args.scopes or "operator"
    else:
        if not args.user or args.operator:
            ap.error("--role merchant needs --user <id> and takes no --operator")
        subject, scopes = args.user, args.scopes or "chat"

    s = get_settings()
    validate_jwt_secret(s.jwt_secret)
    token = make_token(subject, scopes, args.minutes, s.jwt_secret, s.jwt_issuer, s.jwt_audience, role=args.role)
    print(token)
    print(f"\n# valid for {args.minutes:g} min, {args.role}={subject}, scopes={scopes!r}. Try it:\n")
    if args.role == "operator":
        print(f"curl -s \"{args.url}/operator/handoffs?status=pending\" \\\n"
              f"  -H \"Authorization: Bearer {token}\"")
    else:
        print(f"curl -s -X POST {args.url}/chat \\\n"
              f"  -H \"Authorization: Bearer {token}\" \\\n"
              "  -H \"Content-Type: application/json\" \\\n"
              "  -d '{\"message\": \"When will I receive my payout?\"}'")


if __name__ == "__main__":
    main()
