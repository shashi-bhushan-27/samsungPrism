"""Okapi BM25 over pre-tokenised documents (deterministic, no external dependency)."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class ScoredDoc:
    index: int
    score: float
    rank: int


class BM25Index:
    def __init__(self, docs: Sequence[Sequence[str]], *, k1: float = 1.2, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.n_docs = len(docs)
        self.doc_len = np.array([len(d) for d in docs], dtype=np.float64)
        self.avgdl = float(self.doc_len.mean()) if self.n_docs else 0.0
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, doc in enumerate(docs):
            for term, tf in sorted(Counter(doc).items()):
                postings[term].append((i, tf))
        self.postings = dict(postings)
        self.idf = {
            term: math.log(1.0 + (self.n_docs - len(p) + 0.5) / (len(p) + 0.5)) for term, p in self.postings.items()
        }

    def scores(self, query: Sequence[str]) -> np.ndarray:
        out = np.zeros(self.n_docs, dtype=np.float64)
        if not self.n_docs:
            return out
        norm = self.k1 * (1.0 - self.b + self.b * self.doc_len / (self.avgdl or 1.0))
        for term, qtf in Counter(query).items():
            plist = self.postings.get(term)
            if not plist:
                continue
            idf = self.idf[term]
            for i, tf in plist:
                out[i] += qtf * idf * (tf * (self.k1 + 1.0)) / (tf + norm[i])
        return out

    def top_k(self, query: Sequence[str], k: int) -> list[ScoredDoc]:
        s = self.scores(query)
        if not self.n_docs:
            return []
        # Stable ordering: score desc, then document index asc.
        order = sorted(range(self.n_docs), key=lambda i: (-s[i], i))
        return [ScoredDoc(i, float(s[i]), r + 1) for r, i in enumerate(order[:k]) if s[i] > 0.0]
