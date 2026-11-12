"""Creates the chat models.

The agents (router, knowledge, support, synthesizer, summarizer) use AGENT_PROVIDER:
- `groq` (default): ChatGroq with GROQ_API_KEY.
- `fireworks`: ChatOpenAI against Fireworks' OpenAI-compatible endpoint with FIREWORKS_API_KEY. The model
  names still come from LLM_MODEL / LIGHT_MODEL / SUPPORT_MODEL (Fireworks ids, e.g.
  `accounts/fireworks/models/gpt-oss-120b`).
The eval judge always runs on Groq (`get_judge_llm`), whatever AGENT_PROVIDER says, so it stays a
different provider and model family from the agents.
"""
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_groq import ChatGroq

from app.config import get_settings

FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"
PROVIDERS = ("groq", "fireworks")


def is_gpt_oss(name: str) -> bool:
    """gpt-oss accepts a reasoning effort: `openai/gpt-oss-*` on Groq, `accounts/fireworks/models/gpt-oss-*`."""
    return "gpt-oss" in name


def _groq(name: str, temperature: float, max_tokens: int | None, timeout: float, max_retries: int) -> ChatGroq:
    s = get_settings()
    extra = {"reasoning_effort": s.reasoning_effort} if is_gpt_oss(name) else {}
    return ChatGroq(model=name, temperature=temperature, api_key=s.groq_api_key or None, max_retries=max_retries,
                    timeout=timeout, max_tokens=max_tokens, **extra)


def _fireworks(name: str, temperature: float, max_tokens: int | None, timeout: float, max_retries: int):
    from langchain_openai import ChatOpenAI  # only needed with AGENT_PROVIDER=fireworks

    s = get_settings()
    # Fireworks takes `reasoning_effort` (low|medium|high) as a top-level body field for gpt-oss;
    # extra_body sends it as is, without the OpenAI client reinterpreting it
    extra_body = {"reasoning_effort": s.reasoning_effort} if is_gpt_oss(name) else None
    return ChatOpenAI(model=name, temperature=temperature, api_key=s.fireworks_api_key or "missing-fireworks-key",
                      base_url=FIREWORKS_BASE_URL, max_retries=max_retries, timeout=timeout, max_tokens=max_tokens,
                      extra_body=extra_body, stream_usage=True)


def get_llm(temperature: float = 0.0, model: str | None = None, max_tokens: int | None = None,
            timeout: float = 60, max_retries: int = 2) -> BaseChatModel:
    """The model of an agent, on AGENT_PROVIDER."""
    s = get_settings()
    name = model or s.llm_model
    provider = s.agent_provider.lower()
    if provider not in PROVIDERS:
        raise ValueError(f"AGENT_PROVIDER must be one of {PROVIDERS}, got {s.agent_provider!r}")
    build = _fireworks if provider == "fireworks" else _groq
    return build(name, temperature, max_tokens, timeout, max_retries)


def get_judge_llm(temperature: float = 0.0, max_tokens: int | None = 500, timeout: float = 60,
                  max_retries: int = 2) -> ChatGroq:
    """The eval judge: always Groq (GROQ_API_KEY, JUDGE_MODEL)."""
    s = get_settings()
    return _groq(s.judge_model, temperature, max_tokens, timeout, max_retries)
