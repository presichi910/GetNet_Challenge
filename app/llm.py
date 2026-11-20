"""Creates the chat models.

The agents (router, knowledge, support, synthesizer, summarizer) use AGENT_PROVIDER:
- `openrouter` (default): ChatOpenAI against OpenRouter's OpenAI-compatible endpoint with OPENROUTER_API_KEY. Every model is
  pinned to one upstream host (OPENROUTER_PROVIDER_ORDER) with no fallback to another host, so a measurement is made on one host.
- `groq`: ChatGroq with GROQ_API_KEY.
- `fireworks`: ChatOpenAI against Fireworks' OpenAI-compatible endpoint with FIREWORKS_API_KEY. The model
  names still come from LLM_MODEL / LIGHT_MODEL / SUPPORT_MODEL (Fireworks ids, e.g.
  `accounts/fireworks/models/gpt-oss-120b`). The 2026-11-15 reports were made with this provider.
The eval judge (`get_judge_llm`) uses JUDGE_PROVIDER (`openrouter` by default, or `groq`), whatever AGENT_PROVIDER says,
and its model family is never the agents' (a test fails if an agent model id is a Qwen).
"""
import logging
from typing import Any

from langchain_groq import ChatGroq

from app.config import get_settings

log = logging.getLogger(__name__)

FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
PROVIDERS = ("groq", "fireworks", "openrouter")
JUDGE_PROVIDERS = ("groq", "openrouter")
FIREWORKS_PREFIX = "accounts/fireworks/models/"

_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: object) -> None:
    """A warning that does not repeat on every call: get_llm runs once per agent per turn."""
    if key not in _warned:
        _warned.add(key)
        log.warning(message, *args)


def is_gpt_oss(name: str) -> bool:
    """gpt-oss accepts a reasoning effort: `openai/gpt-oss-*` on Groq, `accounts/fireworks/models/gpt-oss-*`."""
    return "gpt-oss" in name


def _groq(name: str, temperature: float, max_tokens: int | None, timeout: float, max_retries: int) -> ChatGroq:
    s = get_settings()
    extra: dict[str, Any] = {"reasoning_effort": s.reasoning_effort} if is_gpt_oss(name) else {}
    return ChatGroq(model=name, temperature=temperature, api_key=s.groq_api_key or None, max_retries=max_retries,  # type: ignore[arg-type]
                    timeout=timeout, max_tokens=max_tokens, **extra)


def _fireworks(name: str, temperature: float, max_tokens: int | None, timeout: float, max_retries: int):
    from langchain_openai import ChatOpenAI  # only needed with AGENT_PROVIDER=fireworks

    s = get_settings()
    # Fireworks takes `reasoning_effort` (low|medium|high) as a top-level body field for gpt-oss;
    # extra_body sends it as is, without the OpenAI client reinterpreting it
    extra_body = {"reasoning_effort": s.reasoning_effort} if is_gpt_oss(name) else None
    return ChatOpenAI(model=name, temperature=temperature, api_key=s.fireworks_api_key or "missing-fireworks-key",  # type: ignore[arg-type,call-arg]
                      base_url=FIREWORKS_BASE_URL, max_retries=max_retries, timeout=timeout, max_tokens=max_tokens,
                      extra_body=extra_body, stream_usage=True)


def openrouter_model_name(name: str) -> str:
    """The OpenRouter id of a model. A Fireworks id (`accounts/fireworks/models/gpt-oss-120b`, what an older `.env` still has) is the
    same model as `openai/gpt-oss-120b`; the translation is logged (once per id), never silent."""
    if name.startswith(FIREWORKS_PREFIX):
        translated = "openai/" + name[len(FIREWORKS_PREFIX):]
        _warn_once(f"translate:{name}", "model id %r is a Fireworks id: using %r on OpenRouter (set the OpenRouter id in .env to silence this)",
                   name, translated)
        return translated
    return name


def _openrouter(name: str, temperature: float, max_tokens: int | None, timeout: float, max_retries: int,
                reasoning_effort: str | None = None):
    from langchain_openai import ChatOpenAI  # only needed with an OpenRouter model

    s = get_settings()
    model = openrouter_model_name(name)
    order = s.openrouter_provider_order.get(model) or []
    provider: dict[str, Any] = {"allow_fallbacks": s.openrouter_allow_fallbacks}
    if order:
        provider["order"] = list(order)
    else:
        _warn_once(f"unpinned:{model}", "OPENROUTER_PROVIDER_ORDER has no host for %r: OpenRouter picks the host, so two calls may not "
                                        "be served by the same one (add the model to OPENROUTER_PROVIDER_ORDER)", model)
    # `provider` and `reasoning` are OpenRouter's request fields. `reasoning.effort` goes to every model: OpenRouter maps it to what each
    # model accepts (gpt-oss takes low|medium|high) and ignores it for a model that does not reason
    extra_body = {"provider": provider, "reasoning": {"effort": reasoning_effort or s.reasoning_effort}}
    # the host that is paid for: the estimate of a call that comes back without a cost (app/observability.py) must use ITS price,
    # not the list price of the model (which is the cheapest host's, or another provider's)
    price_key = f"{model}@{order[0]}" if order else f"{model}@openrouter"
    return ChatOpenAI(model=model, temperature=temperature, api_key=s.openrouter_api_key or "missing-openrouter-key",  # type: ignore[arg-type,call-arg]
                      base_url=OPENROUTER_BASE_URL, max_retries=max_retries, timeout=timeout, max_tokens=max_tokens,
                      extra_body=extra_body, stream_usage=True, metadata={"price_key": price_key})


def outage_errors() -> tuple[type[BaseException], ...]:
    """What a provider outage looks like to either SDK: no connection, a timeout, a 429, a 5xx. A 400 (a broken tool call,
    a bad request) is the model's or our own problem, not an outage: another provider would not fix it, and
    `run_tool_agent` already retries the broken tool call."""
    import groq
    import openai

    return tuple(getattr(sdk, n) for sdk in (openai, groq) for n in ("APIConnectionError", "RateLimitError", "InternalServerError"))


def fallback_model_name(name: str, provider: str) -> str:
    """The same model under the other provider's id: Fireworks `accounts/fireworks/models/gpt-oss-120b` is Groq and OpenRouter
    `openai/gpt-oss-120b`. A name that has no Fireworks prefix is used as it is."""
    if provider in ("groq", "openrouter") and name.startswith(FIREWORKS_PREFIX):
        return "openai/" + name[len(FIREWORKS_PREFIX):]
    if provider == "fireworks" and name.startswith("openai/gpt-oss"):
        return FIREWORKS_PREFIX + name[len("openai/"):]
    return name


def _builder(provider: str):
    return {"groq": _groq, "fireworks": _fireworks, "openrouter": _openrouter}[provider]


def get_llm(temperature: float = 0.0, model: str | None = None, max_tokens: int | None = None,
            timeout: float = 60, max_retries: int = 2) -> Any:  # a chat model, or that model wrapped with its fallback (RunnableWithFallbacks)
    """The model of an agent, on AGENT_PROVIDER. With FALLBACK_PROVIDER set (and different), a provider outage
    (see `outage_errors`) is answered by the same model on the other provider instead of failing the turn: the customer
    does not end in the human queue because the primary is down. `bind_tools` and `with_structured_output` reach both."""
    s = get_settings()
    name = model or s.llm_model
    provider = s.agent_provider.lower()
    if provider not in PROVIDERS:
        raise ValueError(f"AGENT_PROVIDER must be one of {PROVIDERS}, got {s.agent_provider!r}")
    primary = _builder(provider)(name, temperature, max_tokens, timeout, max_retries)
    backup = s.fallback_provider.lower()
    if not backup or backup == provider:
        return primary
    if backup not in PROVIDERS:
        raise ValueError(f"FALLBACK_PROVIDER must be one of {PROVIDERS} or empty, got {s.fallback_provider!r}")
    secondary = _builder(backup)(fallback_model_name(name, backup), temperature, max_tokens, timeout, max_retries)
    return primary.with_fallbacks([secondary], exceptions_to_handle=outage_errors())


def get_judge_llm(temperature: float = 0.0, max_tokens: int | None = 500, timeout: float = 60,
                  max_retries: int = 2) -> Any:
    """The eval judge, on JUDGE_PROVIDER (JUDGE_MODEL). On OpenRouter the judge runs with reasoning OFF, to behave as it did on Groq, where it
    used a median of 92 output tokens per call (it did not reason). The model's default effort is the highest: in two measured calls (the
    second is docs/evidence/openrouter_smoke_2026-11-19.json) it spent 880 to 954 reasoning tokens and cost about 5 times more, with the same JSON.
    It was NOT seen to truncate: at the pinned host the `max_completion_tokens` cap did not count the reasoning tokens (997 and 1070
    completion tokens returned against a cap of 500)."""
    s = get_settings()
    provider = s.judge_provider.lower()
    if provider not in JUDGE_PROVIDERS:
        raise ValueError(f"JUDGE_PROVIDER must be one of {JUDGE_PROVIDERS}, got {s.judge_provider!r}")
    if provider == "groq":
        return _groq(s.judge_model, temperature, max_tokens, timeout, max_retries)
    return _openrouter(s.judge_model, temperature, max_tokens, timeout, max_retries, reasoning_effort="none")
