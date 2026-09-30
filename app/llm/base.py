"""Provider-neutral LLM interface. Application logic never depends on one vendor."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from app.models.internal import TokenUsage


class LLMError(Exception):
    def __init__(self, message: str, *, code: str = "llm_error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: TokenUsage
    latency_ms: float
    finish_reason: Optional[str] = None
    attempts: list[dict[str, Any]] = field(default_factory=list)


class LLMProvider(ABC):
    name = "base"

    @abstractmethod
    def model_name(self) -> str: ...

    @abstractmethod
    async def generate_structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: Optional[dict[str, Any]] = None,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_output_tokens: Optional[int] = None,
    ) -> LLMResponse: ...

    @abstractmethod
    def estimate_cost(self, usage: TokenUsage, model: Optional[str] = None) -> Optional[float]:
        """USD cost, or None when pricing for the model is not known (never guessed)."""

    async def probe(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class NullLLMProvider(LLMProvider):
    """LLM disabled: every call fails fast so the pipeline uses its deterministic fallbacks."""

    name = "none"

    def model_name(self) -> str:
        return "none"

    async def generate_structured(self, **kwargs) -> LLMResponse:
        raise LLMError("LLM provider disabled", code="llm_disabled")

    def estimate_cost(self, usage: TokenUsage, model: Optional[str] = None) -> Optional[float]:
        return 0.0 if usage.calls == 0 else None

    async def probe(self) -> bool:
        return False
