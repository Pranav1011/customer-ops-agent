"""Knowledge base index backed by Chroma (local, offline embeddings).

Uses Chroma's built-in ONNX all-MiniLM embedding function — downloaded once on
first seed, then fully local and key-free. Retrieval is exposed to the agent as
a tool it *chooses* to call, not a mandatory pre-step.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import chromadb

from agent_ops.config import get_settings

_COLLECTION = "aurora_kb"


@lru_cache
def _client() -> chromadb.ClientAPI:
    s = get_settings()
    s.chroma_dir.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(s.chroma_dir))


def get_collection() -> chromadb.Collection:
    return _client().get_or_create_collection(_COLLECTION)


def reset_collection() -> chromadb.Collection:
    client = _client()
    try:
        client.delete_collection(_COLLECTION)
    except Exception:
        pass
    return client.get_or_create_collection(_COLLECTION)


def index_articles(articles: list[dict[str, Any]]) -> int:
    """(Re)index KB articles. Each article: id, title, category, body, tags."""
    col = reset_collection()
    col.add(
        ids=[a["id"] for a in articles],
        documents=[f"{a['title']}\n\n{a['body']}" for a in articles],
        metadatas=[
            {"title": a["title"], "category": a["category"], "tags": ",".join(a.get("tags", []))}
            for a in articles
        ],
    )
    return len(articles)


def search(query: str, k: int = 3) -> list[dict[str, Any]]:
    col = get_collection()
    if col.count() == 0:
        return []
    k = min(k, col.count())
    res = col.query(query_texts=[query], n_results=k)
    hits: list[dict[str, Any]] = []
    ids = res.get("ids", [[]])[0]
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]
    for i, doc_id in enumerate(ids):
        meta = metas[i] or {}
        hits.append(
            {
                "id": doc_id,
                "title": meta.get("title", ""),
                "category": meta.get("category", ""),
                "excerpt": (docs[i] or "")[:600],
                "score": round(1.0 - float(dists[i]), 4) if dists else None,
            }
        )
    return hits
