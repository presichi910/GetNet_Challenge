"""Builds the knowledge base: Tavily -> clean -> chunk -> Voyage embeddings -> Chroma.

    python -m app.rag.ingest                # fetch with Tavily and index
    python -m app.rag.ingest --from-cache   # reuse data/raw/pages.jsonl, no Tavily credits
    python -m app.rag.ingest --reset        # wipe the collection first
    python -m app.rag.ingest --url <page>   # add a specific page
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from langchain_core.documents import Document
from langchain_text_splitters import Language, RecursiveCharacterTextSplitter
from tavily import TavilyClient

from app.config import get_settings
from app.rag.sources import ALLOWED_DOMAINS, CRAWL_SOURCES, TOPIC_QUERIES

log = logging.getLogger("ingest")

MIN_PAGE_CHARS = 200


# fetching
def crawl_sites(client: TavilyClient) -> dict[str, str]:
    pages: dict[str, str] = {}
    for src in CRAWL_SOURCES:
        log.info("Crawling %s (depth=%s, limit=%s)", src.url, src.max_depth, src.limit)
        try:
            res = client.crawl(url=src.url, max_depth=src.max_depth, limit=src.limit,
                               extract_depth="basic", format="markdown", allow_external=False)
        except Exception:
            log.exception("Crawl failed for %s (continuing)", src.url)
            continue
        for r in res.get("results", []):
            if r.get("raw_content"):
                pages[r["url"]] = r["raw_content"]
        log.info("  -> %d pages so far", len(pages))
    return pages


def discover_urls(client: TavilyClient) -> set[str]:
    urls: set[str] = set()
    for q in TOPIC_QUERIES:
        try:
            res = client.search(query=q, include_domains=ALLOWED_DOMAINS, max_results=5, search_depth="basic")
        except Exception:
            log.exception("Topic search failed for %r (continuing)", q)
            continue
        urls.update(r["url"] for r in res.get("results", []))
    log.info("Topic discovery found %d candidate URLs", len(urls))
    return urls


def extract_urls(client: TavilyClient, urls: list[str]) -> dict[str, str]:
    pages: dict[str, str] = {}
    for i in range(0, len(urls), 20):  # Tavily takes 20 URLs max per call
        batch = urls[i:i + 20]
        try:
            res = client.extract(urls=batch, extract_depth="basic", format="markdown")
        except Exception:
            log.exception("Extract failed for batch starting at %d (continuing)", i)
            continue
        for r in res.get("results", []):
            if r.get("raw_content"):
                pages[r["url"]] = r["raw_content"]
    return pages


def fetch_pages(extra_urls: list[str]) -> dict[str, str]:
    s = get_settings()
    if not s.tavily_api_key:
        raise SystemExit("TAVILY_API_KEY is not set. Add it to .env or use --from-cache.")
    client = TavilyClient(api_key=s.tavily_api_key)
    pages = crawl_sites(client)
    to_extract = (discover_urls(client) | set(extra_urls)) - set(pages)
    if to_extract:
        pages.update(extract_urls(client, sorted(to_extract)))
    return pages


# cache
def cache_path() -> Path:
    return Path(get_settings().raw_dir) / "pages.jsonl"


def save_cache(pages: dict[str, str]) -> None:
    p = cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for url, content in pages.items():
            f.write(json.dumps({"url": url, "content": content}, ensure_ascii=False) + "\n")
    log.info("Saved %d raw pages to %s", len(pages), p)


def load_cache() -> dict[str, str]:
    p = cache_path()
    if not p.exists():
        raise SystemExit(f"No cache at {p}. Run ingestion without --from-cache first.")
    with p.open(encoding="utf-8") as f:
        return {r["url"]: r["content"] for r in map(json.loads, f)}


# cleaning and chunking
_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def clean_markdown(text: str) -> str:
    """Remove images and link targets, and repeated lines (menus and footers)."""
    text = _IMG.sub("", text)
    text = _LINK.sub(r"\1", text)
    seen, out = set(), []
    for line in text.splitlines():
        line = line.rstrip()
        key = line.strip().lower()
        if len(key) < 3:
            out.append("")  # keep blank lines
            continue
        if key in seen and len(key) < 80:  # short repeated line = menu
            continue
        seen.add(key)
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def page_title(url: str, text: str) -> str:
    m = re.search(r"^#{1,2}\s+(.+)$", text, flags=re.M)
    if m:
        return m.group(1).strip()[:120]
    return urlparse(url).path.strip("/").replace("-", " ").replace("/", " > ") or urlparse(url).netloc


def chunk_pages(pages: dict[str, str]) -> list[Document]:
    s = get_settings()
    splitter = RecursiveCharacterTextSplitter.from_language(
        Language.MARKDOWN, chunk_size=s.chunk_size, chunk_overlap=s.chunk_overlap
    )
    docs: list[Document] = []
    for url, raw in pages.items():
        text = clean_markdown(raw)
        if len(text) < MIN_PAGE_CHARS:
            continue
        title = page_title(url, text)
        for i, chunk in enumerate(splitter.split_text(text)):
            docs.append(Document(
                page_content=f"{title}\n\n{chunk}",  # the title helps find short chunks
                metadata={"url": url, "title": title, "chunk": i, "lang": "en" if "/en" in url or "getnet.net" in url else "pt",
                          "is_pdf": url.lower().endswith(".pdf")},
            ))
    return docs


def doc_id(d: Document) -> str:
    return hashlib.sha1(f"{d.metadata['url']}#{d.metadata['chunk']}".encode()).hexdigest()


# indexing
def index_documents(docs: list[Document], reset: bool = False) -> int:
    from app.rag.store import get_vector_store

    s = get_settings()
    store = get_vector_store()
    if reset:
        log.info("Resetting collection %s", s.chroma_collection)
        store.delete_collection()
        get_vector_store.cache_clear()
        store = get_vector_store()

    total = 0
    bs = max(1, s.ingest_batch_size)
    for i in range(0, len(docs), bs):
        batch = docs[i:i + bs]
        for attempt in range(1, 6):
            try:
                store.add_documents(batch, ids=[doc_id(d) for d in batch])  # same ids = upsert
                break
            except Exception as e:
                wait = 30 * attempt
                log.warning("Batch %d failed (%s). Retry %d/5 in %ds", i // bs, str(e)[:120], attempt, wait)
                time.sleep(wait)
        else:
            raise RuntimeError(f"Batch {i // bs} failed after 5 attempts")
        total += len(batch)
        log.info("Indexed %d/%d chunks", total, len(docs))
        if s.ingest_sleep_seconds and total < len(docs):
            time.sleep(s.ingest_sleep_seconds)  # avoid Voyage rate limits
    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-cache", action="store_true", help="skip Tavily, reuse data/raw/pages.jsonl")
    ap.add_argument("--reset", action="store_true", help="delete the Chroma collection before indexing")
    ap.add_argument("--url", action="append", default=[], help="extra URL to extract (repeatable)")
    args = ap.parse_args()

    if args.from_cache:
        pages = load_cache()
    else:
        pages = fetch_pages(args.url)
        save_cache(pages)
    log.info("Fetched %d pages", len(pages))

    docs = chunk_pages(pages)
    if not docs:
        raise SystemExit("No usable content after cleaning. Check the sources / Tavily output.")
    avg = sum(len(d.page_content) for d in docs) // len(docs)
    log.info("Created %d chunks from %d pages (avg %d chars)", len(docs), len(pages), avg)

    n = index_documents(docs, reset=args.reset)
    log.info("Done. %d chunks indexed into '%s'.", n, get_settings().chroma_collection)


if __name__ == "__main__":
    main()
