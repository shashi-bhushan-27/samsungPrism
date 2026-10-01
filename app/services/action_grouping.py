"""ONE ACTION = ONE PHYSICAL SCREEN OR FEATURE (PDF §4.1, §7.2).

* merge_fragments: "Open Settings" / "Tap Display" / "Tap Navigation bar" / "Select Swipe gestures" split
  into separate actions are merged into the action that actually uses the screen.
* split_bundled: an action that starts from a root screen twice (two screens) is split.
* merge_same_target: two actions resolved to the same catalog screen become one action.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Optional

from app.core.constants import CATEGORY_AUTO, is_dummy_uri
from app.models.internal import ExtractedAction
from app.retrieval.ui_path import parse_ui_path
from app.validation import text_rules as T
from app.validation.repair import description_from_action_name

_ROOT_NAV = re.compile(
    r"(?i)^(?:open|launch|go\s+to|navigate\s+to(?:\s+and\s+open)?|head\s+to|start)\s+(?:the\s+|your\s+)?"
    r"(?:settings|[\w\- ]{1,30}\s+app)\b"
)
_NAV_ONLY = re.compile(r"(?i)^(?:open|launch|go\s+to|navigate\s+to(?:\s+and\s+open)?|tap(?:\s+on)?|touch|select|head\s+to)\s+")


def _is_root(step: str) -> bool:
    return bool(_ROOT_NAV.match(T.core_clause(step)))


def _is_nav_stub(action: ExtractedAction) -> bool:
    return 0 < len(action.steps) <= 2 and all(_NAV_ONLY.match(T.core_clause(s)) for s in action.steps)


def _is_nav_only(action: ExtractedAction) -> bool:
    """Only reaches a screen ("Open Settings. Tap Display. Tap Navigation bar.") without operating anything."""
    p = parse_ui_path(action.steps)
    return bool(action.steps) and bool(p.root or p.screens) and not p.interactions


def _is_continuation(action: ExtractedAction) -> bool:
    """Only operates on-screen controls ("Select Swipe gestures."), so it happens on the screen the previous
    action ended on. Anything physical or unparsed ("Press and hold the Side key.") disqualifies it."""
    p = parse_ui_path(action.steps)
    return bool(action.steps) and not p.root and bool(p.elements) and all(
        e.kind in ("control", "slider", "button", "value") for e in p.elements
    )


def merge_fragments(actions: list[ExtractedAction]) -> tuple[list[ExtractedAction], list[str]]:
    notes: list[str] = []
    out = list(actions)
    i = 0
    while i < len(out) - 1:
        cur, nxt = out[i], out[i + 1]
        stub = _is_nav_stub(cur) and bool(nxt.steps) and not _is_root(nxt.steps[0])
        # Settings fragments only: critical and manual actions are self-contained operations.
        continuation = cur.category == CATEGORY_AUTO and _is_nav_only(cur) and _is_continuation(nxt)
        if (stub or continuation) and cur.category == nxt.category:
            merged_steps = T.dedupe_consecutive(cur.steps + nxt.steps)
            prov = {**nxt.provenance, "merged_from": cur.action_name}
            prov["has_grounded_content"] = bool(
                nxt.provenance.get("has_grounded_content", True) or cur.provenance.get("has_grounded_content", True)
            )
            out[i + 1] = replace(nxt, steps=merged_steps, provenance=prov)
            notes.append(f"merged_fragment:{cur.action_name}->{nxt.action_name}")
            del out[i]
            i = max(i - 1, 0)
            continue
        i += 1
    return out, notes


def _derived_name(steps: list[str]) -> str:
    path = parse_ui_path(steps)
    target = path.deepest_screen.label if path.deepest_screen else None
    ctrl = next((e for e in path.interactions if e.kind in ("control", "slider", "button")), None)
    if ctrl is not None and ctrl.kind == "control":
        return T.to_title_case(f"Turn {ctrl.state or 'on'} {ctrl.label}")
    if ctrl is not None:
        return T.to_title_case(f"Use {ctrl.label}")
    if target:
        return T.to_title_case(f"Open {target}")
    return T.to_title_case(" ".join(steps[0].rstrip(".").split()[:5]))


def split_bundled(action: ExtractedAction) -> list[ExtractedAction]:
    roots = [i for i, s in enumerate(action.steps) if _is_root(s)]
    if len(roots) <= 1:
        return [action]
    cuts = roots if roots[0] == 0 else [0] + roots
    parts: list[ExtractedAction] = []
    for n, start in enumerate(cuts):
        end = cuts[n + 1] if n + 1 < len(cuts) else len(action.steps)
        steps = action.steps[start:end]
        if not steps:
            continue
        if n == 0:
            parts.append(replace(action, steps=steps))
        else:
            name = _derived_name(steps)
            parts.append(
                replace(
                    action,
                    action_name=name,
                    description=description_from_action_name(name),
                    steps=steps,
                    target_screen=None,
                    navigation_path=[],
                    provenance={**action.provenance, "split_from": action.action_name},
                )
            )
    return parts


def split_all(actions: list[ExtractedAction]) -> tuple[list[ExtractedAction], list[str]]:
    out: list[ExtractedAction] = []
    notes: list[str] = []
    for a in actions:
        parts = split_bundled(a)
        if len(parts) > 1:
            notes.append(f"split_bundled:{a.action_name}->{len(parts)}")
        out.extend(parts)
    return out, notes


def merge_same_target(
    actions: list[ExtractedAction], uris: list[Optional[str]]
) -> tuple[list[ExtractedAction], list[Optional[str]], list[str]]:
    """Merge actions of the same category that resolved to the same catalog URI.

    Same screen but different category (e.g. clear cache vs. clear data) stays two actions; the
    later one keeps its steps but not the duplicated deeplink.
    """
    notes: list[str] = []
    out_a: list[ExtractedAction] = []
    out_u: list[Optional[str]] = []
    first_idx: dict[str, int] = {}
    for a, u in zip(actions, uris):
        if u is None or is_dummy_uri(u):
            out_a.append(a)
            out_u.append(u)
            continue
        if u in first_idx:
            j = first_idx[u]
            if out_a[j].category == a.category:
                out_a[j] = replace(out_a[j], steps=T.dedupe_consecutive(out_a[j].steps + a.steps))
                notes.append(f"merged_same_target:{a.action_name}->{out_a[j].action_name}")
                continue
            notes.append(f"duplicate_target_dropped:{a.action_name}")
            out_a.append(a)
            out_u.append(None)
            continue
        first_idx[u] = len(out_a)
        out_a.append(a)
        out_u.append(u)
    return out_a, out_u, notes
