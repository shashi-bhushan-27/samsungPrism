"""Internal (non-public) data structures passed between pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class QueryRecord:
    id: str
    text: str
    domain: Optional[str] = None
    siis_id: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class SiisDoc:
    id: str
    text: str
    title: Optional[str] = None
    domain: Optional[str] = None
    query_id: Optional[str] = None
    query_text: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class Sample:
    id: str
    query: str
    siis_response: Optional[str]
    expected: Mapping[str, Any]  # decoded expected envelope or {"contexts": [...]}
    source_files: tuple[str, ...] = ()

    @property
    def expected_contexts(self) -> list[Any]:
        exp = self.expected
        if "response" in exp and isinstance(exp["response"], Mapping):
            return list(exp["response"].get("contexts", []))
        return list(exp.get("contexts", []))


@dataclass
class LoadIssue:
    source: str
    code: str
    detail: str


@dataclass
class CanonicalIntent:
    """Deterministic interpretation of a complaint (no LLM involved)."""

    raw_query: str
    normalized_query: str
    canonical_query: str
    domain: Optional[str]
    domain_scores: dict[str, float]
    symptoms: tuple[str, ...]
    features: tuple[str, ...]
    qualifiers: tuple[str, ...]
    goal_kind: str
    topic: str
    title_hint: str
    signature: str
    sub_intents: tuple["CanonicalIntent", ...] = ()

    @property
    def has_concepts(self) -> bool:
        return bool(self.symptoms or self.features)


@dataclass
class ExtractedAction:
    action_name: str
    description: str
    category: str
    steps: list[str]
    target_screen: Optional[str] = None
    navigation_path: list[str] = field(default_factory=list)
    controls: list[str] = field(default_factory=list)
    target_description: Optional[str] = None
    source_quote: Optional[str] = None
    proposed_category: Optional[str] = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractedGoal:
    topic: str
    goal_kind: str
    title: str
    actions: list[ExtractedAction]
    source_id: Optional[str] = None


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.thinking_tokens

    def add(self, other: "TokenUsage") -> "TokenUsage":
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.thinking_tokens += other.thinking_tokens
        self.calls += other.calls
        return self
