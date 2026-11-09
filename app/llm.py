"""LLM factory. Everything that talks to Groq goes through here."""
from langchain_groq import ChatGroq

from app.config import get_settings


def get_llm(temperature: float = 0.0, model: str | None = None, max_tokens: int | None = None) -> ChatGroq:
    s = get_settings()
    name = model or s.llm_model
    extra = {"reasoning_effort": s.reasoning_effort} if name.startswith("openai/gpt-oss") else {}
    return ChatGroq(
        model=name,
        temperature=temperature,
        api_key=s.groq_api_key or None,
        max_retries=2,
        timeout=60,
        max_tokens=max_tokens,
        **extra,
    )
