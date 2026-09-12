"""BM25 candidate retrieval followed by embedding cosine reranking.

Dependencies: numpy, rank-bm25.
"""

from __future__ import annotations

import re

import numpy as np
from rank_bm25 import BM25Okapi


def tokenize_chinese(text: str) -> list[str]:
    """Keep Latin words/numbers together and use single Han characters as tokens."""
    return re.findall(r"[\u4e00-\u9fff]|[A-Za-z]+|\d+(?:\.\d+)?", str(text).lower())


class HybridRetriever:
    def __init__(self, bm25_top_k: int = 20, final_top_k: int = 3, similarity_threshold: float = 0.5):
        self.bm25_top_k = int(bm25_top_k)
        self.final_top_k = int(final_top_k)
        self.similarity_threshold = float(similarity_threshold)
        self.chunks: list[dict] = []
        self.bm25: BM25Okapi | None = None

    def build_index(self, chunks: list[dict]) -> None:
        if not chunks:
            raise ValueError("Cannot build retrieval index without textbook chunks")
        self.chunks = chunks
        self.bm25 = BM25Okapi([tokenize_chinese(item["chunk_text"]) for item in chunks])

    def bm25_search(self, query: str) -> list[dict]:
        if self.bm25 is None:
            raise RuntimeError("BM25 index has not been built")
        scores = self.bm25.get_scores(tokenize_chinese(query))
        indices = np.argsort(scores)[::-1][: self.bm25_top_k]
        return [self.chunks[int(index)] for index in indices]

    def retrieve(self, query: str, query_embedding: list[float]) -> dict:
        candidates = self.bm25_search(query)
        available = [item for item in candidates if item.get("embedding") is not None]
        if not available:
            return {"matched_chunks": [], "top_score": 0.0, "is_matched": False}
        query_vector = np.asarray(query_embedding, dtype=np.float32)
        candidate_matrix = np.asarray([item["embedding"] for item in available], dtype=np.float32)
        denominator = np.linalg.norm(candidate_matrix, axis=1) * np.linalg.norm(query_vector)
        similarities = np.divide(candidate_matrix @ query_vector, denominator, out=np.zeros(len(available)), where=denominator != 0)
        indices = np.argsort(similarities)[::-1][: self.final_top_k]
        top_score = float(similarities[indices[0]]) if len(indices) else 0.0
        matched = []
        for index in indices:
            item = available[int(index)]
            matched.append(
                {
                    "chunk_id": item["id"],
                    "text": item["chunk_text"],
                    "source_file": item["source_file"],
                    "source_page": item["source_page"],
                    "score": float(similarities[int(index)]),
                }
            )
        return {"matched_chunks": matched, "top_score": top_score, "is_matched": top_score >= self.similarity_threshold}

