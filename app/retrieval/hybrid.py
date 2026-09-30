"""Hybrid BM25 + dense retrieval with score normalisation and weighted fusion."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from app.retrieval.indexes import CatalogDoc, CatalogIndex
from app.retrieval.text import tokens


@dataclass
class Candidate:
    doc: CatalogDoc
    bm25: float = 0.0
    bm25_rank: Optional[int] = None
    dense: float = 0.0
    dense_rank: Optional[int] = None
    fused: float = 0.0
    signals: dict[str, float] = field(default_factory=dict)
    final: float = 0.0
    exact: bool = False
    parent: bool = False
    flags: list[str] = field(default_factory=list)

    @property
    def uri(self) -> str:
        return self.doc.uri

    def trace(self) -> dict:
        return {
            "uri": self.uri,
            "description": self.doc.entry.description,
            "bm25": round(self.bm25, 4),
            "bm25_rank": self.bm25_rank,
            "dense": round(self.dense, 4),
            "dense_rank": self.dense_rank,
            "fused": round(self.fused, 4),
            "final": round(self.final, 4),
            "exact": self.exact,
            "parent": self.parent,
            "flags": list(self.flags),
            "signals": {k: round(v, 3) for k, v in self.signals.items()},
        }


# Dense cosine is mapped with a fixed affine transform (not per-query min-max) so scores stay
# comparable across queries; BM25 is normalised by the best BM25 score of the query.
DENSE_FLOOR = 0.30
DENSE_SPAN = 0.55


class HybridRetriever:
    def __init__(self, index: CatalogIndex, *, top_k_bm25: int, top_k_dense: int, w_bm25: float, w_dense: float):
        self.index = index
        self.top_k_bm25 = top_k_bm25
        self.top_k_dense = top_k_dense
        total = (w_bm25 + w_dense) or 1.0
        self.w_bm25 = w_bm25 / total
        self.w_dense = w_dense / total

    def retrieve(
        self,
        query_text: str,
        query_vec: Optional[np.ndarray],
        *,
        use_bm25: bool = True,
        use_dense: bool = True,
    ) -> list[Candidate]:
        cands: dict[int, Candidate] = {}
        if use_bm25:
            for hit in self.index.bm25.top_k(tokens(query_text), self.top_k_bm25):
                c = cands.setdefault(hit.index, Candidate(self.index.docs[hit.index]))
                c.bm25, c.bm25_rank = hit.score, hit.rank
        if use_dense and query_vec is not None:
            for hit in self.index.dense.top_k(query_vec, self.top_k_dense):
                c = cands.setdefault(hit.index, Candidate(self.index.docs[hit.index]))
                c.dense, c.dense_rank = hit.score, hit.rank
            # Dense scores for BM25-only candidates (cheap: one dot product each).
            sims = self.index.dense.similarities(query_vec)
            for i, c in cands.items():
                if c.dense_rank is None:
                    c.dense = float(sims[i])
        best_bm25 = max((c.bm25 for c in cands.values()), default=0.0) or 1.0
        for c in cands.values():
            bm = c.bm25 / best_bm25 if use_bm25 else 0.0
            dn = float(np.clip((c.dense - DENSE_FLOOR) / DENSE_SPAN, 0.0, 1.0)) if use_dense else 0.0
            if use_bm25 and use_dense:
                c.fused = self.w_bm25 * bm + self.w_dense * dn
            else:
                c.fused = bm if use_bm25 else dn
            c.signals["bm25_norm"] = bm
            c.signals["dense_norm"] = dn
        order = sorted(cands.items(), key=lambda kv: (-kv[1].fused, kv[1].doc.entry.index))
        return [c for _, c in order]
