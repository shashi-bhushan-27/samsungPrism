"""Cache record model. Only fully validated plans are ever stored."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

ORIGIN_KB = "kb"  # grounded on the SIIS knowledge base (pre-warm or KB retrieval)
ORIGIN_REQUEST = "request"  # grounded on a caller-supplied siis_response (scoped to that text)


@dataclass
class CacheKey:
    text: str
    normalized: str
    kind: str  # query | canonical | variation


@dataclass
class CacheRecord:
    plan_id: str
    origin: str
    source_id: Optional[str]
    source_fp: str
    query: str
    normalized_query: str
    intent: dict[str, Any]
    payload: dict[str, Any]  # {"query_variations": [...], "response": {"contexts": [...]}}
    model: str
    versions: dict[str, str]
    keys: list[CacheKey] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "CacheRecord":
        keys = [CacheKey(**k) for k in d.get("keys", [])]
        return cls(
            plan_id=d["plan_id"],
            origin=d["origin"],
            source_id=d.get("source_id"),
            source_fp=d["source_fp"],
            query=d["query"],
            normalized_query=d["normalized_query"],
            intent=d.get("intent", {}),
            payload=d["payload"],
            model=d.get("model", ""),
            versions=d.get("versions", {}),
            keys=keys,
            created_at=d.get("created_at", time.time()),
            provenance=d.get("provenance", {}),
        )


def make_plan_id(origin: str, source_fp: str, normalized_query: str) -> str:
    raw = json.dumps([origin, source_fp, normalized_query], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass
class CacheHit:
    record: CacheRecord
    kind: str  # exact | semantic
    similarity: float
    matched_key: str
    diagnostics: dict[str, Any] = field(default_factory=dict)
