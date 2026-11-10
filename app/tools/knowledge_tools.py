"""Tools for the Knowledge agent: search the Getnet KB and search the web.

The LLM only sees the text. The sources travel separately (as an artifact) so the API can return them.
"""
from __future__ import annotations

import logging
import time
from langchain_core.tools import tool
from tavily import TavilyClient

from app.config import get_settings

log = logging.getLogger(__name__)

PDF_SLOTS = 2  # max PDF results out of k


def _embed_with_backoff(embeddings, query: str, attempts: int = 3, wait_s: float = 21.0):
    """Embed the query, waiting if Voyage rate limits us (free tier: 3 requests/min)."""
    for attempt in range(1, attempts + 1):
        try:
            return embeddings.embed_query(query)
        except Exception as e:  # noqa: BLE001
            limited = "ratelimit" in type(e).__name__.lower() or "rate limit" in str(e).lower()
            if not limited or attempt == attempts:
                raise
            log.warning("Embedding rate-limited; waiting %.0fs (attempt %d/%d)", wait_s, attempt, attempts)
            time.sleep(wait_s)


def retrieve(query: str, k: int) -> list[tuple]:
    """Search web pages and PDFs separately, with a cap on PDFs.

    PDFs are ~60% of the chunks and would crowd out the pages that actually answer the question.
    The query is embedded once for both searches.
    """
    from app.rag.store import get_embeddings, get_vector_store

    store = get_vector_store()
    vec = _embed_with_backoff(get_embeddings(), query)
    pages = store.similarity_search_by_vector_with_relevance_scores(vec, k=k - PDF_SLOTS, filter={"is_pdf": False})
    pdfs = store.similarity_search_by_vector_with_relevance_scores(vec, k=PDF_SLOTS, filter={"is_pdf": True})
    # careful: despite the name, this returns cosine distance (lower is better), relevance = 1 - distance
    return sorted(pages + pdfs, key=lambda x: x[1])


@tool(response_format="content_and_artifact")
def search_getnet_kb(query: str) -> tuple[str, list[dict]]:
    """Semantic search over Getnet's official website content (products, plans, fees, Pix,
    receivables advance, payment links, terminals, troubleshooting). Returns the most relevant
    passages with their source URLs. Write the query as a self-contained question or keywords;
    English or Portuguese both work."""
    from app.rag.store import collection_size, get_vector_store  # lazy import

    s = get_settings()
    if collection_size() == 0:
        return ("The knowledge base is empty (ingestion has not been run). "
                "No Getnet passages are available."), []
    try:
        hits = retrieve(query, s.retrieval_k)
    except Exception as e:  # network or rate limit
        log.exception("KB search failed")
        return f"Knowledge base search failed: {e}", []

    if not hits:
        return "No relevant passages found.", []

    blocks, sources, seen = [], [], set()
    for i, (doc, dist) in enumerate(hits, 1):
        url = doc.metadata.get("url", "")
        title = doc.metadata.get("title", "")
        blocks.append(f"[{i}] (relevance={1 - dist:.2f}) {title} <{url}>\n{doc.page_content}")
        if url not in seen:
            seen.add(url)
            sources.append({"url": url, "title": title, "type": "kb", "relevance": round(1 - dist, 3)})
    return "\n\n".join(blocks), sources


@tool(response_format="content_and_artifact")
def web_search(query: str, topic: str = "general") -> tuple[str, list[dict]]:
    """Live web search for general-purpose or time-sensitive questions that are NOT about
    Getnet (weather, exchange rates, news, general facts). Use topic='finance' for currency /
    market questions and topic='news' for current events. Include the place/date in the query."""
    topic = topic if topic in ("general", "news", "finance") else "general"  # the model may send something else
    s = get_settings()
    if not s.tavily_api_key:
        return "Web search is not configured (missing TAVILY_API_KEY).", []
    try:
        res = TavilyClient(api_key=s.tavily_api_key).search(
            query=query, topic=topic, max_results=4, search_depth="basic", include_answer="basic"
        )
    except Exception as e:
        log.exception("Web search failed")
        return f"Web search failed: {e}", []

    parts, sources = [], []
    if res.get("answer"):
        parts.append(f"Summary: {res['answer']}")
    for i, r in enumerate(res.get("results", []), 1):
        parts.append(f"[{i}] {r.get('title', '')} <{r.get('url', '')}>\n{r.get('content', '')}")
        sources.append({"url": r.get("url", ""), "title": r.get("title", ""), "type": "web"})
    return ("\n\n".join(parts) or "No web results."), sources
