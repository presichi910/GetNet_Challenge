"""Central configuration, loaded from environment variables / .env."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    # API keys
    groq_api_key: str = ""
    tavily_api_key: str = ""
    voyage_api_key: str = ""

    # Models
    llm_model: str = "openai/gpt-oss-120b"
    # Right-sized model for the router, support and synthesizer. Also spreads load: Groq TPM limits are per model.
    light_model: str = "openai/gpt-oss-20b"
    judge_model: str = "qwen/qwen3.8-27b"
    reasoning_effort: str = "low"  # gpt-oss reasoning budget; "low" cuts latency and tokens a lot
    embedding_model: str = "voyage-3.5"

    # RAG
    chroma_dir: str = "./data/chroma"
    chroma_collection: str = "getnet_kb"
    retrieval_k: int = 5
    chunk_size: int = 1000
    chunk_overlap: int = 150
    ingest_batch_size: int = 16
    ingest_sleep_seconds: float = 21.0
    raw_dir: str = "./data/raw"

    # Support agent
    tickets_file: str = "./data/tickets.jsonl"
    handoffs_file: str = "./data/handoffs.jsonl"  # queue a human operator would consume

    # Conversation window handed to the router / agents (number of messages)
    history_window: int = 6

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
