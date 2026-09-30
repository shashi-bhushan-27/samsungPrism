"""Scriptable in-process LLM for tests and offline benchmarks (never used in production)."""

from __future__ import annotations

import json
from typing import Any, Callable, Optional, Union

from app.llm.base import LLMError, LLMProvider, LLMResponse
from app.models.internal import TokenUsage

Responder = Callable[[str, str, Optional[dict]], Union[str, dict, Exception]]


class FakeLLMProvider(LLMProvider):
    """`responder(system, prompt, schema)` returns text, a dict (JSON-encoded) or an Exception."""

    name = "fake"

    def __init__(self, responder: Optional[Responder] = None, *, model: str = "fake-llm", healthy: bool = True):
        self.responder = responder or (lambda s, p, sc: {"has_solution": False, "goals": []})
        self._model = model
        self.healthy = healthy
        self.calls: list[dict[str, Any]] = []

    def model_name(self) -> str:
        return self._model

    async def generate_structured(self, *, system, prompt, schema=None, model=None, temperature=0.0, max_output_tokens=None):
        self.calls.append({"system": system, "prompt": prompt, "schema": schema, "model": model})
        out = self.responder(system, prompt, schema)
        if isinstance(out, Exception):
            if isinstance(out, LLMError):
                raise out
            raise LLMError(str(out), code="llm_fake_error")
        text = out if isinstance(out, str) else json.dumps(out)
        usage = TokenUsage(input_tokens=len(prompt) // 4, output_tokens=len(text) // 4, calls=1)
        return LLMResponse(text=text, model=model or self._model, usage=usage, latency_ms=1.0)

    def estimate_cost(self, usage: TokenUsage, model: Optional[str] = None) -> Optional[float]:
        return 0.0

    async def probe(self) -> bool:
        return self.healthy
