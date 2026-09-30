"""Defensive parsing of model output: valid JSON, fenced JSON, prose wrappers, trailing commas."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


class ParseError(ValueError):
    pass


def _balanced_object(text: str) -> str | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def parse_json_loose(text: str) -> dict[str, Any]:
    """Return the first JSON object found in `text`, or raise ParseError."""
    if not isinstance(text, str) or not text.strip():
        raise ParseError("empty model output")
    raw = text.strip().lstrip("﻿")
    candidates = [raw]
    fence = _FENCE.search(raw)
    if fence:
        candidates.insert(0, fence.group(1).strip())
    obj = _balanced_object(raw)
    if obj:
        candidates.append(obj)
    for cand in candidates:
        for attempt in (cand, _TRAILING_COMMA.sub(r"\1", cand)):
            try:
                value = json.loads(attempt)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(value, dict):
                return value
            if isinstance(value, list):
                return {"goals": value}
    raise ParseError("no JSON object could be parsed from model output")
