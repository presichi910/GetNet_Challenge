"""Settings, read from env vars / .env."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    # keys
    groq_api_key: str = ""
    fireworks_api_key: str = ""
    tavily_api_key: str = ""
    voyage_api_key: str = ""

    # auth: HS256 JWT. The secret has no default, the app refuses to start without a real one.
    jwt_secret: str = ""
    jwt_issuer: str = "getnet-support-demo"
    jwt_audience: str = "getnet-support-api"

    # models
    # provider of the agents: groq (default) or fireworks (OpenAI-compatible API). The judge is always Groq.
    agent_provider: str = "groq"
    llm_model: str = "openai/gpt-oss-120b"
    # smaller model for router and synthesizer (Groq limits are per model)
    light_model: str = "openai/gpt-oss-20b"
    # support agent. Empty = the same model as LIGHT_MODEL. A bigger model follows the "always call
    # get_recent_transactions on a decline" rule much better (q08), at the cost of its quota.
    support_model: str = ""
    judge_model: str = "qwen/qwen3.8-27b"
    reasoning_effort: str = "low"  # gpt-oss only, keeps it fast
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

    # support / handoff files
    tickets_file: str = "./data/tickets.jsonl"
    handoffs_file: str = "./data/handoffs.jsonl"  # audit copy of every handoff (and the source of the one-time import)
    # the handoff queue the operators work through (claim / reply). Its own file: it does not depend on the
    # conversation checkpointer, and the conversation retention purge never touches it
    handoffs_db_path: str = "./data/handoffs.sqlite"
    # load the old queue file (HANDOFFS_FILE) into the queue at startup. Off by default: that file may hold
    # test cases (the eval writes to it) and, if it is from before the audit file was redacted, whole
    # conversation excerpts. Back it up and clean it first (README, "Human oversight"), then turn this on
    handoff_import_jsonl: bool = False
    # how long an operator's claim holds. After that the handoff is pending again, so one who claimed
    # and went away does not keep a customer waiting. Claiming again (same operator) renews it
    handoff_lease_minutes: float = 15.0

    # conversation memory: LangGraph checkpoints in one SQLite file (inside the data volume in Docker)
    checkpoint_db_path: str = "./data/checkpoints.sqlite"
    # retention (the checkpoints hold customer data): a thread idle for more than this many days is
    # deleted (0 = never), at startup and every `purge_interval_minutes`
    thread_ttl_days: int = 30
    purge_interval_minutes: float = 60.0
    # conversations per customer. Above it /chat answers 429; nothing is ever deleted to make room (0 = no limit)
    max_threads_per_user: int = 20

    # how many past messages the agents see
    history_window: int = 6
    # rolling summary: above this many messages, the older ones are folded into `summary` and removed,
    # keeping the last `history_window`. Uses LIGHT_MODEL. If it fails or takes longer than the
    # timeout, the turn goes on without it and the next turn tries again.
    summary_threshold: int = 12
    summary_timeout_seconds: float = 20.0

    # observability (app/observability.py): per-turn traces and spans (ids, tokens, cost, timings; no text)
    traces_db_path: str = "./data/traces.sqlite"
    # extra or corrected prices, JSON {model: [usd_per_1M_input, usd_per_1M_output]}
    model_prices_json: str = ""
    # a turn above either limit logs a WARNING "ALERT ..." (0 = off). A whole turn on gpt-oss-120b is ~2-10K
    # tokens and well under a cent, so these only fire on a runaway turn
    max_turn_tokens: int = 30000
    max_turn_cost_usd: float = 0.02

    log_level: str = "INFO"


    @property
    def effective_support_model(self) -> str:
        return self.support_model or self.light_model


@lru_cache
def get_settings() -> Settings:
    return Settings()
