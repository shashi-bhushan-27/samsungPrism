"""Per-request stage timings and token/cost accounting."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator, Optional

from app.models.internal import TokenUsage


class StageTimer:
    STAGES = (
        "query_enrichment", "exact_cache", "semantic_cache", "cache_validation", "siis_retrieval",
        "llm_inference", "deeplink_retrieval", "deeplink_reranking", "sequencing", "validation", "cache_write",
    )

    def __init__(self) -> None:
        self.start = time.perf_counter()
        self.stages: dict[str, float] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = self.stages.get(name, 0.0) + (time.perf_counter() - t0) * 1000.0

    def add(self, name: str, ms: float) -> None:
        self.stages[name] = self.stages.get(name, 0.0) + ms

    @property
    def total_ms(self) -> float:
        return (time.perf_counter() - self.start) * 1000.0

    def snapshot(self) -> dict[str, float]:
        out = {k: round(v, 3) for k, v in self.stages.items()}
        out["total"] = round(self.total_ms, 3)
        return out


class CostMeter:
    """Accumulates tokens and cost across LLM calls; cost becomes None if any call is unpriced."""

    def __init__(self) -> None:
        self.usage = TokenUsage()
        self.cost_usd: Optional[float] = 0.0
        self.models: list[str] = []

    def add(self, usage: TokenUsage, cost: Optional[float], model: Optional[str]) -> None:
        self.usage.add(usage)
        if model and model not in self.models:
            self.models.append(model)
        if cost is None and usage.calls:
            self.cost_usd = None
        elif self.cost_usd is not None and cost is not None:
            self.cost_usd += cost
