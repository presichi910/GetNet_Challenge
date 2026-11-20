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
    openrouter_api_key: str = ""

    # auth: RS256 tokens checked against the identity provider's public keys (JWKS). There is no shared secret in
    # the app. Merchants and operators are separate: each has its own issuer, audience and keys (in Keycloak, two
    # realms). Nothing has a default: the app refuses to start unless all six are set and the two sides differ.
    jwks_url: str = ""
    jwt_issuer: str = ""
    jwt_audience: str = ""
    operator_jwks_url: str = ""
    operator_jwt_issuer: str = ""
    operator_jwt_audience: str = ""
    # a token that lives longer than this is refused, whatever the identity provider is configured to issue
    jwt_max_lifetime_seconds: int = 900
    # revoked token ids (`jti`): their own SQLite file, in the same volume as the others, so a restart keeps them
    revocations_db_path: str = "./data/revocations.sqlite"
    jwks_cache_ttl_seconds: float = 600.0
    jwks_min_refresh_seconds: float = 30.0  # an unknown `kid` re-fetches the keys at most this often
    jwks_timeout_seconds: float = 5.0
    # requests per minute per authenticated caller (0 = no limit)
    rate_limit_per_minute: int = 30
    operator_rate_limit_per_minute: int = 120

    # models
    # provider of the agents: openrouter (default), groq or fireworks (the last two were the providers of the 2026-11-15 reports)
    agent_provider: str = "openrouter"
    # provider of the eval judge: openrouter (default) or groq. The judge's model family is never the agents' (app/llm.py)
    judge_provider: str = "openrouter"
    # the other provider that answers when AGENT_PROVIDER has an outage (5xx, 429, no connection, timeout); empty = no
    # failover. The same model under the other provider's id (app/llm.py::fallback_model_name). Needs its API key
    fallback_provider: str = ""
    # OpenRouter. Every model is pinned to ONE upstream host (a measurement must not move between hosts or between quantizations):
    # JSON {model id: [provider slugs]}, sent as `provider.order`. A model not listed is not pinned (and a warning says so).
    # `allow_fallbacks` false = a host that fails is an error, never another host. Setting this variable replaces the whole map.
    openrouter_provider_order: dict[str, list[str]] = {"openai/gpt-oss-120b": ["deepinfra/bf16"], "qwen/qwen3.8-27b": ["alibaba"]}
    openrouter_allow_fallbacks: bool = False
    llm_model: str = "openai/gpt-oss-120b"
    # router, synthesizer (unless SYNTH_MODEL is set), verifier, observe and summarizer
    light_model: str = "openai/gpt-oss-120b"
    # support agent. Empty = the same model as LIGHT_MODEL. A bigger model follows the "always call
    # get_recent_transactions on a decline" rule much better, at the cost of its quota.
    support_model: str = ""
    # the generation roles that the model comparison (plan 9.6, C1) changes. Empty = LLM_MODEL (knowledge) and LIGHT_MODEL (synthesizer)
    knowledge_model: str = ""
    synth_model: str = ""
    judge_model: str = "qwen/qwen3.8-27b"
    reasoning_effort: str = "low"  # gpt-oss only, keeps it fast
    embedding_model: str = "voyage-3.5"

    # RAG
    chroma_dir: str = "./data/chroma"
    chroma_collection: str = "getnet_kb"
    retrieval_k: int = 5
    # how the knowledge base is searched (app/rag/hybrid.py): "dense" (Voyage embeddings only), "hybrid" (dense + BM25,
    # fused with RRF) or "hybrid_rerank" (hybrid, then Voyage's reranker reorders the best `rag_pool`)
    rag_mode: str = "dense"
    rag_pool: int = 10  # candidates the reranker reads (chosen on the tune half, see eval/run_retrieval.py --grid)
    rag_rrf_k: int = 60  # RRF constant: the larger, the flatter the weight of the top ranks
    rerank_model: str = "rerank-2.5"
    # query embeddings and rerank scores, kept on disk (a hash of the query, never its text); "" = no cache
    rag_cache_path: str = "./data/rag_cache.sqlite"
    chunk_size: int = 1000
    chunk_overlap: int = 150
    ingest_batch_size: int = 16
    ingest_sleep_seconds: float = 21.0
    raw_dir: str = "./data/raw"

    # support / handoff files
    tickets_file: str = "./data/tickets.jsonl"
    # a ticket in one of these categories is NOT opened by the agent: it is requested, and a person approves or rejects it in
    # /operator/approvals (app/approvals.py). Default = every category. Empty = no approval, the ticket opens at once. A request
    # nobody decides within the time limit expires: no ticket, the customer is told, the case goes to the human queue
    approval_required_categories: str = "terminal,payout,billing,account,other"
    approval_ttl_minutes: float = 60.0
    approvals_db_path: str = "./data/approvals.sqlite"  # the requests AND the paused graphs: its own file, same volume
    # a second ticket of the same category from the same customer within this many seconds is the same issue worded
    # differently (a retried agent): the ticket already open is returned instead (0 = only the exact same summary counts)
    ticket_dedup_window_s: float = 600.0
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

    # a handoff nobody claims within this many minutes is ESCALATED once: priority high, an event in its audit trail, and a webhook (0 = off)
    handoff_sla_minutes: float = 30.0
    # push to the operators (app/webhook.py): empty URL = off. The secret is required with a URL (an unsigned push is not offered)
    webhook_url: str = ""
    webhook_signing_key: str = ""
    webhook_timeout_seconds: float = 3.0
    webhook_retries: int = 3

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

    # the final answer may not repeat this many consecutive words of any system prompt (app/guard.py); 0 = guard off
    prompt_leak_min_words: int = 10

    # how the verify node audits an answer: "statements" (the model lists what it finds; the original check) or
    # "segments" (the code splits the answer and the model must answer for every segment: nothing can be omitted)
    verifier_mode: str = "statements"

    # how many times the observe step may change course after the answer is written (app/agents/observe.py):
    # 0 = never (the graph is what it was), 1 = retry the knowledge base once with the unsupported claims,
    # 2 = ONE more action: add the support agent when the router sent only knowledge and an unsupported claim is about the customer's
    # account, otherwise offer a ticket or escalate when nothing actionable is left. Any value above 2 behaves exactly like 2.
    # Off by default until it is measured.
    replan_max: int = 0

    # observability (app/telemetry.py). /metrics exists only with a token (Prometheus sends it as a bearer); traces go out only with an OTLP endpoint
    metrics_token: str = ""
    otlp_endpoint: str = ""  # e.g. http://jaeger:4317 (gRPC); empty = no traces exported
    otel_service_name: str = "getnet-agents"

    log_level: str = "INFO"


    @property
    def approval_categories(self) -> frozenset[str]:
        return frozenset(c.strip().lower() for c in self.approval_required_categories.split(",") if c.strip())

    @property
    def effective_support_model(self) -> str:
        return self.support_model or self.light_model

    @property
    def effective_knowledge_model(self) -> str:
        return self.knowledge_model or self.llm_model

    @property
    def effective_synth_model(self) -> str:
        return self.synth_model or self.light_model


@lru_cache
def get_settings() -> Settings:
    return Settings()
