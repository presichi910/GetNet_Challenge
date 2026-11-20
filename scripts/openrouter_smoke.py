"""Three REAL OpenRouter calls (about US$0.004 in all) that the offline tests cannot make, saved as evidence.

    python -m scripts.openrouter_smoke                 # writes docs/evidence/openrouter_smoke_<local date>.json

A. gpt-oss-120b through the code's own agent model, with one tool: does a tool call work on the pinned host, and which host answered?
B. the judge exactly as the code builds it (qwen3.8-27b, reasoning off, max_completion_tokens 500): parseable JSON? which host? what real cost?
C. the same judge request WITHOUT the reasoning field (the provider's default effort): how many tokens does it spend, does it truncate?

The request bodies are built by app.llm (the same `extra_body` the agents send) and sent with httpx, so the raw response can be read
(`provider`, the one field that names the host that answered, and `usage.cost`). The key is read from the settings and used only in the
Authorization header: neither the key nor any request header is stored. The messages are synthetic (no customer data).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
AGENT_MODEL = "openai/gpt-oss-120b"
JUDGE_MODEL = "qwen/qwen3.8-27b"
# the host each call is expected to land on (the pins of app/config.py); `provider` in the response is its display name
EXPECTED_HOST = {"agent": "groq", "judge": "alibaba"}
# the prices the judge call could have been ESTIMATED with: the pinned host's, the model's listed price, the old Groq table's (USD per 1M tokens)
JUDGE_PRICES = {"alibaba (the pinned host)": (0.425, 2.55), "listed model price": (0.06, 2.2), "old Groq table": (0.80, 4.00)}


def get_weather(city: str) -> str:
    """Current weather of a city."""
    return ""


def body_of(llm, messages, **kw) -> dict:
    payload = llm._get_request_payload(messages, **kw)
    payload.update(payload.pop("extra_body", None) or {})  # the OpenAI client merges extra_body into the body; do the same here
    return payload


def send(base_url: str, key: str, body: dict) -> dict:
    response = httpx.post(f"{base_url}/chat/completions", json=body, timeout=120,
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    data = response.json()
    if response.status_code != 200:
        raise SystemExit(f"HTTP {response.status_code}: {json.dumps(data.get('error', data))[:400]}")
    return data


def describe_request(body: dict) -> dict:
    """The non-secret fields of a request: what was asked, never the messages' text or any header."""
    return {"model": body["model"], "provider": body.get("provider"), "reasoning": body.get("reasoning", "omitted: the provider's default"),
            "max_completion_tokens": body.get("max_completion_tokens"), "temperature": body.get("temperature"),
            "tools": [t["function"]["name"] for t in body.get("tools", [])], "synthetic_messages": True}


def describe_response(data: dict, expected_host: str) -> dict:
    choice = data["choices"][0]
    usage = data.get("usage") or {}
    message = choice.get("message") or {}
    host = data.get("provider")
    return {
        "response_id": data.get("id"), "host_that_answered": host, "expected_host": expected_host,
        "pinned_host_answered": expected_host in str(host).lower(), "model": data.get("model"),
        "finish_reason": choice.get("finish_reason"), "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        "real_cost_usd": usage.get("cost"), "content_chars": len(message.get("content") or ""),
        "tool_calls": [t["function"]["name"] for t in message.get("tool_calls") or []],
    }


def run() -> dict:
    # this run must not follow whatever a developer's .env says about providers or models
    os.environ.update(AGENT_PROVIDER="openrouter", JUDGE_PROVIDER="openrouter", FALLBACK_PROVIDER="", REASONING_EFFORT="low", JUDGE_MODEL=JUDGE_MODEL)
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from app.config import get_settings
    from app.llm import OPENROUTER_BASE_URL, get_judge_llm, get_llm
    from app.prompts import JUDGE_PROMPT
    from eval.judge import parse_judgement
    from eval.provenance import repo_state

    get_settings.cache_clear()
    s = get_settings()
    if not s.openrouter_api_key:
        raise SystemExit("OPENROUTER_API_KEY is empty: nothing was sent.")
    state = repo_state()
    calls = []

    agent = get_llm(model=AGENT_MODEL, max_tokens=400)
    body = body_of(agent, [SystemMessage(content="Use the tool to answer."), HumanMessage(content="What is the weather in Monterrey?")],
                   tools=[convert_to_openai_tool(get_weather)], tool_choice="auto")
    data = send(OPENROUTER_BASE_URL, s.openrouter_api_key, body)
    calls.append({"call": "A: agent gpt-oss-120b with one tool", "request": describe_request(body),
                  "response": describe_response(data, EXPECTED_HOST["agent"])})

    judge = get_judge_llm()
    user = ("## User question\nHow do I enable Pix on my terminal?\n\n## Assistant answer\nOpen the Getnet app, go to Settings > Payment methods and "
            "turn on Pix. No other step is needed.\n\n## Evidence available to the assistant\nPix is enabled in the Getnet app under Settings > "
            "Payment methods.")
    messages = [SystemMessage(content=JUDGE_PROMPT), HumanMessage(content=user)]
    judge_body = body_of(judge, messages)
    for label, request_body in (("B: judge as the code builds it (reasoning off)", judge_body),
                                ("C: judge with the provider's default reasoning", {k: v for k, v in judge_body.items() if k != "reasoning"})):
        data = send(OPENROUTER_BASE_URL, s.openrouter_api_key, request_body)
        entry = {"call": label, "request": describe_request(request_body), "response": describe_response(data, EXPECTED_HOST["judge"])}
        try:
            entry["response"]["parseable_by_eval_judge"] = bool(parse_judgement(data["choices"][0]["message"].get("content") or ""))
        except Exception as exc:  # noqa: BLE001
            entry["response"].update(parseable_by_eval_judge=False, parse_error=str(exc)[:120])
        calls.append(entry)

    b = calls[1]["response"]
    estimates = {name: round((b["prompt_tokens"] * i + b["completion_tokens"] * o) / 1e6, 9) for name, (i, o) in JUDGE_PRICES.items()}
    return {
        "_capture": {
            "command": "python -m scripts.openrouter_smoke", "at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "commit": state["commit"], "tree_clean": state["tree_clean"],
            "stored": "the non-secret fields of each request and the usage fields of each response; never the key, a header or a message text",
            "shows": "which host answered each call, that a tool call works on the pinned host, what the judge costs with reasoning off and "
                     "at the default effort (tokens and the real usage.cost), and that the real cost equals the pinned host's price",
            "does_not_show": "agreement of this judge with the Groq judge (that is block C2), one call per case so no variation, latency, "
                             "or that the cap `max_completion_tokens` never truncates (it did not here: see call C)",
        },
        "settings": {"agent_model": AGENT_MODEL, "judge_model": JUDGE_MODEL, "openrouter_provider_order": s.openrouter_provider_order,
                     "allow_fallbacks": s.openrouter_allow_fallbacks, "agent_reasoning_effort": s.reasoning_effort, "judge_reasoning_effort": "none"},
        "calls": calls,
        "judge_call_B_estimates_usd": estimates,
        "judge_call_B_real_cost_usd": b["real_cost_usd"],
        "judge_default_effort_vs_off": {"completion_tokens": [calls[2]["response"]["completion_tokens"], b["completion_tokens"]],
                                        "reasoning_tokens": [calls[2]["response"]["reasoning_tokens"], b["reasoning_tokens"]],
                                        "real_cost_usd": [calls[2]["response"]["real_cost_usd"], b["real_cost_usd"]],
                                        "order": "[default effort, reasoning off]"},
        "total_real_cost_usd": round(sum(c["response"]["real_cost_usd"] or 0 for c in calls), 9),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--output", help="output file (default: docs/evidence/openrouter_smoke_<local date>.json)")
    args = parser.parse_args(argv)
    report = run()
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    out = Path(args.output) if args.output else ROOT / "docs" / "evidence" / f"openrouter_smoke_{day}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out}: total real cost US${report['total_real_cost_usd']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
