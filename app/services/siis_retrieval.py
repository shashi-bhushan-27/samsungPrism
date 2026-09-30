"""SIIS knowledge retrieval: used when siis_response is omitted and the cache misses.

Only a confident, concept-compatible match is used as grounding; otherwise the engine returns the
documented `no_siis_context` fallback instead of guessing (no external web content, ever).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from app.cache.semantic import compatibility, intent_summary
from app.models.internal import CanonicalIntent, SiisDoc
from app.retrieval.indexes import SiisIndex
from app.retrieval.text import tokens
from app.services.query_enrichment import QueryEnricher


@dataclass
class SiisMatch:
    doc: SiisDoc
    score: float
    bm25_norm: float
    dense: float
    margin: float


class SiisRetriever:
    def __init__(
        self,
        index: SiisIndex,
        enricher: QueryEnricher,
        *,
        min_score: float,
        min_margin: float,
        w_bm25: float = 0.4,
        w_dense: float = 0.6,
    ):
        self.index = index
        self.min_score = min_score
        self.min_margin = min_margin
        self.w_bm25 = w_bm25
        self.w_dense = w_dense
        self._doc_intent = {}
        for d in index.docs:
            anchor = (index.query_text_by_doc.get(d.id) or [d.title or d.text.split("\n", 1)[0]])[0]
            self._doc_intent[d.id] = intent_summary(enricher.analyze(anchor))

    def retrieve(self, intent: CanonicalIntent, qvec: Optional[np.ndarray]) -> tuple[Optional[SiisMatch], list[dict]]:
        idx = self.index
        if not len(idx):
            return None, []
        q = f"{intent.normalized_query} {intent.canonical_query}"
        bm = {h.index: h.score for h in idx.bm25.top_k(tokens(q), 10)}
        sims = idx.dense.similarities(qvec) if qvec is not None else np.zeros(len(idx))
        dense_top = sorted(range(len(sims)), key=lambda i: (-float(sims[i]), i))[:10]
        pool = sorted(set(bm) | set(dense_top))
        best_bm = max(bm.values(), default=0.0) or 1.0
        qsum = intent_summary(intent)
        scored: list[tuple[float, int, float, float]] = []
        rejected: list[dict] = []
        for i in pool:
            d = idx.docs[i]
            ok, why = compatibility(qsum, self._doc_intent[d.id])
            if not ok:
                rejected.append({"doc": d.id, "reason": why})
                continue
            b = bm.get(i, 0.0) / best_bm
            s = self.w_bm25 * b + self.w_dense * float(sims[i])
            scored.append((s, i, b, float(sims[i])))
        scored.sort(key=lambda t: (-t[0], t[1]))
        if not scored:
            return None, rejected
        top = scored[0]
        margin = top[0] - (scored[1][0] if len(scored) > 1 else 0.0)
        match = SiisMatch(idx.docs[top[1]], top[0], top[2], top[3], margin)
        if top[0] < self.min_score or margin < self.min_margin:
            rejected.append({"doc": match.doc.id, "reason": "below_threshold", "score": round(top[0], 4),
                             "margin": round(margin, 4)})
            return None, rejected
        return match, rejected
