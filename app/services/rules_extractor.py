"""Deterministic (no-LLM) extraction used when the model is unavailable.

Parses SIIS text into sections (numbered items, "Solution N:", bullets or paragraphs) and turns
imperative sentences into atomic steps. Lower quality than the LLM path but fully grounded.
"""

from __future__ import annotations

import re
from typing import Optional

from app.models.internal import ExtractedAction
from app.validation import text_rules as T
from app.validation.repair import repair_steps

_SECTION = re.compile(
    r"^\s*(?:(?:\d+[.)])|(?:solution|step|option|method|tip)\s*\d*\s*[:.)-]|[-*•])\s*(?P<head>[^\n]*)$",
    re.I | re.M,
)
_HEAD_BODY = re.compile(r"^(?P<head>[^:]{3,80}):\s*(?P<body>.+)$", re.S)
_SENT = re.compile(r"(?<=[.!?])\s+")


def _sections(text: str) -> list[tuple[str, str]]:
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    sections: list[tuple[str, list[str]]] = []
    current: Optional[tuple[str, list[str]]] = None
    loose: list[str] = []
    for ln in lines:
        if not ln.strip():
            continue
        m = _SECTION.match(ln)
        if m:
            head = m.group("head").strip()
            hb = _HEAD_BODY.match(head)
            if hb:
                current = (hb.group("head").strip(), [hb.group("body").strip()])
            else:
                current = (head, [])
            sections.append(current)
        elif current is not None:
            current[1].append(ln.strip())
        else:
            loose.append(ln.strip())
    out = [(h, " ".join(b)) for h, b in sections]
    if not out:
        # Unstructured prose: every imperative paragraph becomes a candidate section.
        out = [("", p) for p in loose[1:]] if len(loose) > 1 else [("", " ".join(loose))]
    return out


def _name_from(head: str, steps: list[str]) -> str:
    base = head.strip().rstrip(".:")
    if not base and steps:
        base = steps[-1].rstrip(".")
    words = base.split()[:6]
    return T.to_title_case(" ".join(words)) if words else "Review Settings"


def extract_rules(siis_text: str) -> list[ExtractedAction]:
    actions: list[ExtractedAction] = []
    for head, body in _sections(siis_text):
        sentences = [s.strip() for s in _SENT.split(body) if s.strip()]
        imperative = [s for s in sentences if T.is_imperative(s)[0]]
        steps = repair_steps(imperative)
        steps = [s for s in steps if T.is_imperative(s)[0]]
        if not steps:
            continue
        name = _name_from(head, steps)
        actions.append(ExtractedAction(action_name=name, description="", category="auto", steps=steps,
                                       source_quote=body[:400], proposed_category=None))
    return actions
