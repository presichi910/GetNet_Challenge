"""Save, as evidence, what OpenRouter says about the models of plan 9.6: prices, parameters, reasoning options and, for the models
that are pinned to a host, the hosts that serve them (quantization, price, uptime).

    python -m scripts.openrouter_models                  # writes docs/evidence/openrouter_models_<today>.json
    python -m scripts.openrouter_models -o some.json

Only PUBLIC endpoints are read (`GET /api/v1/models` and `/api/v1/models/<id>/endpoints`): no key is sent and none is stored.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

BASE = "https://openrouter.ai/api/v1"
ROOT = Path(__file__).resolve().parent.parent
# the agents' model, the judge, and the candidates of the generation comparison (plan 9.6, C1)
MODELS = (
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "anthropic/claude-haiku-5.5",
    "anthropic/claude-sonnet-5.5",
    "openai/gpt-5-mini",
    "openai/gpt-5.4-mini",
    "deepseek/deepseek-v4.1-flash",
)
# the models whose host is pinned (app/config.py: openrouter_provider_order): their hosts are listed too
PINNED = ("openai/gpt-oss-120b", "qwen/qwen3.8-27b")
ENDPOINT_FIELDS = ("provider_name", "tag", "quantization", "pricing", "context_length", "max_completion_tokens", "uptime_last_30m",
                   "supported_parameters", "status")


def fetch(client: httpx.Client, path: str) -> dict:
    response = client.get(f"{BASE}{path}", timeout=30)
    response.raise_for_status()
    return response.json()["data"]


def build() -> dict:
    with httpx.Client() as client:
        listed = {m["id"]: m for m in fetch(client, "/models")}
        missing = [m for m in MODELS if m not in listed]
        if missing:
            raise SystemExit(f"not listed by OpenRouter: {missing}")
        models = []
        for model_id in MODELS:
            m = listed[model_id]
            entry = {k: m.get(k) for k in ("id", "name", "context_length", "pricing", "supported_parameters", "reasoning", "default_parameters")}
            if model_id in PINNED:
                endpoints = fetch(client, f"/models/{model_id}/endpoints")["endpoints"]
                entry["endpoints"] = [{k: e.get(k) for k in ENDPOINT_FIELDS} for e in endpoints]
            models.append(entry)
    now = datetime.now(timezone.utc)
    return {
        "_capture": {
            "fetched_at_utc": now.strftime("%Y-%m-%d %H:%M:%S"),
            "command": "python -m scripts.openrouter_models",
            "sources": [f"GET {BASE}/models", *[f"GET {BASE}/models/{m}/endpoints" for m in PINNED]],
            "note": "Public endpoints, no key. Prices are USD per token as OpenRouter lists them (multiply by 1e6 for USD per million tokens). "
                    "`endpoints` is a snapshot: uptime and prices move.",
        },
        "models": models,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--output", help="output file (default: docs/evidence/openrouter_models_<date>.json)")
    args = parser.parse_args(argv)
    data = build()
    # the file is named by the LOCAL date (the project's evidence is dated in local time); the capture carries the UTC time
    local_day = datetime.now().astimezone().strftime("%Y-%m-%d")
    out = Path(args.output) if args.output else ROOT / "docs" / "evidence" / f"openrouter_models_{local_day}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(data['models'])} models)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
