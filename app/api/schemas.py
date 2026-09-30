"""HTTP request/response envelope (PDF §5 and Appendix B). The plan itself is schema.ContextDeeplinkResponse."""

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

import schema
from app.core.constants import FALLBACK_CODES


class TroubleshootRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)

    query: str = Field(..., description="Customer complaint in natural language")
    siis_response: Optional[str] = Field(None, description="Optional raw SIIS reference text")

    @field_validator("query", mode="before")
    @classmethod
    def _query_is_string(cls, v: Any) -> Any:
        if not isinstance(v, str):
            raise ValueError("query must be a string")
        return v

    @field_validator("siis_response", mode="before")
    @classmethod
    def _siis_is_string(cls, v: Any) -> Any:
        if v is not None and not isinstance(v, str):
            raise ValueError("siis_response must be a string when provided")
        return v


class Meta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latency_ms: int = Field(..., ge=0)
    cache_hit: bool
    model: str
    cost_usd: Optional[float] = Field(None, ge=0)
    fallback: Optional[str] = None

    @field_validator("fallback")
    @classmethod
    def _fallback_code(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in FALLBACK_CODES:
            raise ValueError(f"fallback must be one of {sorted(FALLBACK_CODES)}")
        return v


class TroubleshootResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    query_variations: List[str]
    response: schema.ContextDeeplinkResponse
    meta: Meta

    def to_public_dict(self) -> dict[str, Any]:
        """Serialise exactly as the worked example: meta.fallback only present when set."""
        meta = self.meta.model_dump(mode="json")
        if meta.get("fallback") is None:
            meta.pop("fallback", None)
        response = self.response.model_dump(mode="json")
        # Match the worked example: optional Deeplink metadata that is unset is omitted, while
        # StepGroup.validationDeeplink stays explicit (null) as in Appendix B.
        for goal in response.get("contexts", []):
            for action in goal.get("actions", []):
                for group in action.get("stepGroups", []):
                    dl = group.get("actionableDeeplink")
                    if isinstance(dl, dict):
                        for k in ("classes", "originalType"):
                            if dl.get(k) is None:
                                dl.pop(k, None)
        return {
            "query": self.query,
            "query_variations": list(self.query_variations),
            "response": response,
            "meta": meta,
        }


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: Optional[str] = None


class ErrorResponse(BaseModel):
    error: ErrorBody
