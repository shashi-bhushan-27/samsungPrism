"""Deterministic action-category and disruption rules.

The LLM proposes a category; these rules decide it. Keyword evidence is taken from the
actionName and the steps (the operations the user will actually perform).

Ordering tiers (PDF §4.1 "critical ... must be ordered last", §6 "Settings toggles ->
system optimizations -> device reboots"; see AUDIT.md D2):

    0 auto: settings / configuration
    1 auto: less-disruptive corrective actions (clear cache, force stop, ...)
    2 auto: system optimisation (Device care, memory, storage)
    3 manual: physical interventions (cleaning → accessories/hardware → service centre)
    4 critical: restart < safe mode < software update < wipe cache partition < reset settings < clear app data < factory reset
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from app.core.constants import CATEGORY_AUTO, CATEGORY_CRITICAL, CATEGORY_MANUAL

# (rank, pattern) — higher rank = more disruptive. Checked against name + steps.
_CRITICAL_RULES: tuple[tuple[int, re.Pattern[str]], ...] = (
    (7, re.compile(r"(?i)\bfactory\s+(?:data\s+)?reset\b|\berase\s+all\b|\bwipe\s+(?:all\s+)?data\b|\breset\s+(?:your\s+)?(?:phone|device)\s+to\s+factory")),
    (6, re.compile(r"(?i)\bclear\s+(?:app\s+)?(?:data|storage)\b|\bdelete\s+(?:app\s+)?data\b")),
    (5, re.compile(r"(?i)\breset\s+(?:all\s+)?settings\b|\breset\s+network\s+settings\b|\breset\s+accessibility\s+settings\b|\breset\s+app\s+preferences\b")),
    (4, re.compile(r"(?i)\bwipe\s+(?:the\s+)?cache\s+partition\b|\brecovery\s+mode\b")),
    (3, re.compile(r"(?i)\b(?:software|firmware|system)\s+update\b|\bupdate\s+(?:the\s+|your\s+)?(?:software|firmware|phone|device)\b|\bdownload\s+and\s+install\b")),
    (2, re.compile(r"(?i)\bsafe\s+mode\b")),
    (1, re.compile(r"(?i)\brestart\b|\breboot\b|\bpower\s+off\b|\bturn\s+(?:the\s+|your\s+)?(?:phone|device)\s+off\b|\bswitch\s+off\s+(?:the\s+|your\s+)?(?:phone|device)\b")),
)
# Things that look critical but are not device-level operations.
_CRITICAL_EXCEPTIONS = re.compile(
    r"(?i)\bauto(?:matic)?\s+restart\b|\brestart\s+(?:the\s+)?(?:[\w-]+\s+){0,2}app\b|\bforce\s+stop\b"
    r"|\breset\s+(?:camera\s+)?settings\s+in\s+the\s+camera\b"
    r"|\breset\s+(?:the\s+)?(?:brightness|zoom|timer|counter)\b|\bupdate\s+(?:the\s+)?(?:[\w-]+\s+){0,2}apps?\b|\bapp\s+updates?\b"
    r"|\bgalaxy\s+store\b.*\bupdate\b|\bupdate\s+.*\bin\s+(?:galaxy|play)\s+store\b"
)
# Used only to confirm an LLM-proposed "critical" that no specific rule above recognised.
_GENERIC_DESTRUCTIVE = re.compile(r"(?i)\b(?:reset|erase|wipe|format|delete\s+all|restore\s+factory)\b")
# Camera-app "Reset settings" only restores camera preferences — corrective, not critical.
_CAMERA_SETTINGS_RESET = re.compile(r"(?i)\bcamera\b.*\breset\s+settings\b|\breset\s+settings\b.*\bcamera\b")

_MANUAL_RULES: tuple[tuple[int, re.Pattern[str]], ...] = (
    (2, re.compile(r"(?i)\bservice\s+(?:centre|center)\b|\bauthori[sz]ed\s+(?:repair|service)\b|\btake\s+(?:the\s+|your\s+)?(?:phone|device)\s+to\b|\bcontact\s+(?:samsung\s+)?(?:support|customer\s+care)\b|\brepair\s+(?:centre|center|shop)\b")),
    (1, re.compile(r"(?i)\breplace\s+(?:the\s+)?(?:battery|cable|charger|adapter|screen)\b|\b(?:use|try)\s+(?:a\s+|an\s+|the\s+)?(?:different|another|original|genuine|certified|samsung)\s+(?:\w+\s+)?(?:charger|cable|adapter|charging\s+cable|wall\s+outlet|outlet|power\s+source)\b")),
    (0, re.compile(r"(?i)\bclean\s+(?:the\s+)?(?:\w+\s+){0,2}(?:port|lens|lenses|screen|sensor|speaker|microphone|contacts?)\b|\bwipe\s+(?:the\s+)?(?:\w+\s+){0,2}(?:lens|screen|port)\b|\bremove\s+(?:the\s+|any\s+|your\s+)?(?:\w+\s+){0,2}(?:case|cover|screen\s+protector|protector|film|sticker|sim\s+card|sd\s+card|memory\s+card)\b|\blet\s+(?:the\s+|your\s+)?(?:phone|device)\s+cool\b|\bmove\s+(?:the\s+|your\s+)?(?:phone|device)\s+(?:to|out|away)\b|\bkeep\s+(?:the\s+|your\s+)?(?:phone|device)\s+(?:out\s+of|away\s+from)\b|\bunplug\s+(?:the\s+)?(?:charger|cable)\b|\binspect\s+(?:the\s+)?(?:\w+\s+)?(?:port|cable|charger)\b|\bdry\s+(?:the\s+|your\s+)?(?:phone|device|port)\b|\bwith\s+both\s+hands\b|\btripod\b|\bgimbal\b|\b(?:re)?insert\s+(?:the\s+|it\s+)?(?:sim|sd|memory)\b|\bcools?\s+down\b")),
)

_CORRECTIVE_RE = re.compile(
    r"(?i)\bclear\s+(?:the\s+)?cache\b|\bforce\s+stop\b|\buninstall\b|\bdisable\s+(?:the\s+)?app\b|\bremove\s+(?:the\s+)?app\b"
    r"|\bupdate\s+(?:the\s+|your\s+|all\s+)?(?:[\w-]+\s+){0,2}apps?\b|\bapp\s+updates?\b|\bupdate\s+all\b|\breset\s+settings\b"
)
_OPTIMISE_RE = re.compile(
    r"(?i)\boptimi[sz]e\s+now\b|\bdevice\s+care\b|\bclean\s+now\b|\bfree\s+up\b|\bauto\s+optimi[sz]ation\b|\bmemory\b.*\bclean\b"
    r"|\bstorage\b.*\b(?:clean|delete|remove)\b|\bclose\s+all\b|\bclose\s+(?:background|unused|running)\s+apps\b"
)

MAX_TIER = 4


@dataclass(frozen=True)
class CategoryDecision:
    category: str
    rank: int
    tier: int
    reason: str


def _text(action_name: str, steps: Sequence[str]) -> str:
    return " | ".join([action_name or "", *[s or "" for s in steps]])


def critical_rank(action_name: str, steps: Sequence[str]) -> Optional[int]:
    text = _text(action_name, steps)
    best: Optional[int] = None
    for rank, rx in _CRITICAL_RULES:
        for m in rx.finditer(text):
            window = text[max(0, m.start() - 40) : m.end() + 40]
            if _CRITICAL_EXCEPTIONS.search(window):
                continue
            if rank in (5,) and _CAMERA_SETTINGS_RESET.search(text) and "reset settings" in m.group(0).lower():
                continue
            best = rank if best is None else max(best, rank)
    return best


def manual_rank(action_name: str, steps: Sequence[str]) -> Optional[int]:
    text = _text(action_name, steps)
    best: Optional[int] = None
    for rank, rx in _MANUAL_RULES:
        if rx.search(text):
            best = rank if best is None else max(best, rank)
    return best


def classify(action_name: str, steps: Sequence[str], proposed: Optional[str] = None) -> CategoryDecision:
    """Deterministic category. Keyword evidence overrides the proposed (LLM) category."""
    crit = critical_rank(action_name, steps)
    if crit is not None:
        return CategoryDecision(CATEGORY_CRITICAL, crit, 4, "critical_keyword")
    if proposed == CATEGORY_CRITICAL and _GENERIC_DESTRUCTIVE.search(_text(action_name, steps)):
        # Safety first: keep an LLM "critical" when the wording is destructive.
        return CategoryDecision(CATEGORY_CRITICAL, 5, 4, "proposed_critical_destructive")
    man = manual_rank(action_name, steps)
    # A physical intervention only makes the action manual when no Settings navigation is
    # involved (e.g. "Clean the charging port" vs "Open Settings > ... > clean now").
    if man is not None and not _mentions_settings_navigation(steps):
        return CategoryDecision(CATEGORY_MANUAL, man, 3, "manual_keyword")
    text = _text(action_name, steps)
    if _OPTIMISE_RE.search(text):
        return CategoryDecision(CATEGORY_AUTO, 0, 2, "optimisation_keyword")
    if _CORRECTIVE_RE.search(text):
        return CategoryDecision(CATEGORY_AUTO, 0, 1, "corrective_keyword")
    if proposed == CATEGORY_MANUAL and not _mentions_settings_navigation(steps):
        return CategoryDecision(CATEGORY_MANUAL, 0, 3, "proposed_manual_no_settings")
    return CategoryDecision(CATEGORY_AUTO, 0, 0, "settings_default")


_SETTINGS_NAV = re.compile(r"(?i)\b(?:open|go\s+to|navigate\s+to|launch)\b.*\bsettings\b|\bsettings\s*(?:>|→|->)")


def _mentions_settings_navigation(steps: Iterable[str]) -> bool:
    return any(_SETTINGS_NAV.search(s or "") for s in steps)


def order_key(category: str, action_name: str, steps: Sequence[str]) -> tuple[int, int]:
    """(tier, rank) used by the sequencer; a stable sort keeps source order within a key."""
    if category == CATEGORY_CRITICAL:
        rank = critical_rank(action_name, steps)
        if rank is None and _GENERIC_DESTRUCTIVE.search(_text(action_name, steps)):
            rank = 5
        return (4, rank or 0)
    if category == CATEGORY_MANUAL:
        return (3, manual_rank(action_name, steps) or 0)
    text = _text(action_name, steps)
    if _OPTIMISE_RE.search(text):
        return (2, 0)
    if _CORRECTIVE_RE.search(text):
        return (1, 0)
    return (0, 0)
