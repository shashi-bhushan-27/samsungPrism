"""Schema gate: strict JSON parsing + Pydantic validation against the supplied schema.py."""

from __future__ import annotations

import json
from typing import Any, Optional

import schema
from app.validation.report import ValidationReport


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def strict_json_loads(text: str) -> Any:
    """json.loads that rejects NaN/Infinity and markdown-wrapped payloads."""
    if not isinstance(text, str):
        raise ValueError("payload must be text")
    stripped = text.strip()
    if stripped.startswith("```"):
        raise ValueError("markdown-fenced JSON is not pure JSON")
    return json.loads(stripped, parse_constant=_reject_constant)


def validate_contract_model(obj: Any) -> tuple[Optional[schema.ContextDeeplinkResponse], ValidationReport]:
    """Validate a decoded `response` object against schema.ContextDeeplinkResponse."""
    report = ValidationReport()
    try:
        model = schema.ContextDeeplinkResponse.model_validate(obj)
    except Exception as exc:
        report.schema_valid = False
        report.add("schema.contract", "$.response", str(exc)[:500])
        return None, report
    report.check("schema.contract", True, "$.response")
    return model, report


def parse_jsonl_line(line: str) -> tuple[Optional[Any], Optional[str]]:
    try:
        return strict_json_loads(line), None
    except Exception as exc:
        return None, f"invalid JSON: {exc}"
