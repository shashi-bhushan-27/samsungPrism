"""Grounding guard (PDF §4.2.3 "No Hallucinated Steps"): every step must be supported by the source.

A step is supported when its content tokens (UI verbs and filler removed) are covered by one
source sentence, or — for short steps — by the source as a whole. Unsupported steps are dropped
before anything reaches the response; actions left without support are removed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from app.retrieval.text import token_set, tokens

_UI_VERBS = frozenset(
    tokens(
        "open tap touch select choose pick turn toggle switch press hold swipe drag slide go navigate launch enable "
        "disable activate deactivate make sure then optionally again also first next finally please on off to and "
        "into the your a an it them this that settings setting app menu option button screen icon"
    )
)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+|(?<=:)\s+|,\s+(?:and\s+)?then\s+|\s+and\s+then\s+|;\s+")


@dataclass
class SourceIndex:
    text: str
    sentences: list[str]
    sentence_tokens: list[frozenset[str]]
    all_tokens: frozenset[str]

    @classmethod
    def build(cls, text: str) -> "SourceIndex":
        sents = [s.strip() for s in _SENT_SPLIT.split(text or "") if s and s.strip()]
        toks = [token_set(s) for s in sents]
        allt = frozenset().union(*toks) if toks else frozenset()
        return cls(text or "", sents, toks, allt)


@dataclass
class StepSupport:
    step: str
    score: float
    sentence_index: Optional[int]
    supported: bool


@dataclass
class GroundingReport:
    supports: list[StepSupport] = field(default_factory=list)

    @property
    def mean(self) -> float:
        return sum(s.score for s in self.supports) / len(self.supports) if self.supports else 0.0


def step_support(step: str, src: SourceIndex, *, threshold: float = 0.6) -> StepSupport:
    ct = frozenset(t for t in token_set(step) if t not in _UI_VERBS)
    if not ct:
        # Pure navigation verb with a generic object ("Tap OK.", "Open Settings.") — supported if the
        # source mentions the object at all.
        raw = token_set(step)
        ok = bool(raw & src.all_tokens) or not raw
        return StepSupport(step, 1.0 if ok else 0.0, None, ok)
    best, best_i = 0.0, None
    for i, st in enumerate(src.sentence_tokens):
        cov = len(ct & st) / len(ct)
        if cov > best:
            best, best_i = cov, i
    if best < threshold and len(ct) <= 3:
        whole = len(ct & src.all_tokens) / len(ct)
        if whole >= 0.99:
            best = max(best, threshold)
    return StepSupport(step, round(best, 4), best_i, best >= threshold)


def quote_supported(quote: Optional[str], src: SourceIndex) -> bool:
    if not quote:
        return False
    qt = token_set(quote)
    if not qt:
        return False
    return len(qt & src.all_tokens) / len(qt) >= 0.8


def ground_steps(steps: list[str], src: SourceIndex, *, threshold: float = 0.6) -> tuple[list[str], GroundingReport]:
    report = GroundingReport()
    kept: list[str] = []
    for s in steps:
        sup = step_support(s, src, threshold=threshold)
        report.supports.append(sup)
        if sup.supported:
            kept.append(s)
    return kept, report
