from app.rag.ingest import chunk_pages, clean_markdown, doc_id, page_title

PAGE = """# Get Smart

![hero](https://cdn/x.png)
Menu
Home
Menu
Home

The [Get Smart](https://www.getnet.com.br/get-smart) card machine accepts credit, debit and Pix. """ + (
    "It has a touch screen and prints receipts. " * 40)


def test_clean_removes_images_links_and_repeated_nav():
    out = clean_markdown(PAGE)
    assert "![" not in out and "https://cdn" not in out and "(https://www.getnet" not in out
    assert out.count("Menu") == 1 and "Get Smart card machine" in out


def test_title_from_heading_or_url():
    assert page_title("https://x/y", PAGE) == "Get Smart"
    assert page_title("https://www.getnet.com.br/link-de-pagamento", "no heading here") == "link de pagamento"


def test_chunks_have_metadata_and_stable_ids():
    docs = chunk_pages({"https://www.getnet.com.br/get-smart": PAGE, "https://x/tiny": "short"})
    assert docs and all(d.metadata["url"] == "https://www.getnet.com.br/get-smart" for d in docs)  # tiny page is dropped
    assert docs[0].page_content.startswith("Get Smart")
    ids = [doc_id(d) for d in docs]
    assert len(set(ids)) == len(ids)
    assert ids == [doc_id(d) for d in chunk_pages({"https://www.getnet.com.br/get-smart": PAGE})]


def test_retrieval_uses_separate_pools_and_embeds_once(monkeypatch):
    """Web pages get k-2 slots, PDFs at most 2, one embedding call."""
    from langchain_core.documents import Document

    from app.rag import store as store_mod
    from app.tools import knowledge_tools as kt

    calls = {"embed": 0, "filters": []}

    class FakeEmb:
        def embed_query(self, q):
            calls["embed"] += 1
            return [0.0]

    class FakeStore:
        def similarity_search_by_vector_with_relevance_scores(self, vec, k, filter):
            calls["filters"].append((k, filter))
            tag = "pdf" if filter["is_pdf"] else "web"
            return [(Document(page_content=f"{tag}{i}", metadata={"url": f"u-{tag}{i}"}), 0.2 + 0.1 * i) for i in range(k)]

    monkeypatch.setattr(store_mod, "get_embeddings", lambda: FakeEmb())
    monkeypatch.setattr(store_mod, "get_vector_store", lambda: FakeStore())
    hits = kt.retrieve("q", 5)
    assert calls["embed"] == 1
    assert calls["filters"] == [(3, {"is_pdf": False}), (2, {"is_pdf": True})]
    assert len(hits) == 5 and sum(d.page_content.startswith("pdf") for d, _ in hits) == 2
    dists = [dist for _, dist in hits]
    assert dists == sorted(dists) and dists[0] == 0.2  # best first


def test_embedding_backoff_retries_on_rate_limit_then_succeeds(monkeypatch):
    from app.tools import knowledge_tools as kt

    class RateLimitError(Exception):
        pass

    class Emb:
        n = 0

        def embed_query(self, q):
            Emb.n += 1
            if Emb.n < 3:
                raise RateLimitError("3 RPM")
            return [1.0]

    monkeypatch.setattr(kt.time, "sleep", lambda s: None)
    assert kt._embed_with_backoff(Emb(), "q") == [1.0] and Emb.n == 3


def test_embedding_backoff_does_not_retry_other_errors(monkeypatch):
    import pytest

    from app.tools import knowledge_tools as kt

    class Emb:
        def embed_query(self, q):
            raise ValueError("bad key")

    monkeypatch.setattr(kt.time, "sleep", lambda s: pytest.fail("must not sleep"))
    with pytest.raises(ValueError):
        kt._embed_with_backoff(Emb(), "q")
