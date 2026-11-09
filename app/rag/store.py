"""Chroma vector store + Voyage AI embeddings (shared by ingestion and retrieval)."""
from __future__ import annotations

from functools import lru_cache

from langchain_chroma import Chroma
from langchain_voyageai import VoyageAIEmbeddings

from app.config import get_settings


@lru_cache
def get_embeddings() -> VoyageAIEmbeddings:
    s = get_settings()
    return VoyageAIEmbeddings(model=s.embedding_model, voyage_api_key=s.voyage_api_key or None,
                              batch_size=s.ingest_batch_size)


@lru_cache
def get_vector_store() -> Chroma:
    s = get_settings()
    return Chroma(
        collection_name=s.chroma_collection,
        embedding_function=get_embeddings(),
        persist_directory=s.chroma_dir,
        collection_metadata={"hnsw:space": "cosine"},
    )


def collection_size() -> int:
    try:
        return get_vector_store()._collection.count()
    except Exception:
        return 0
