"""Deterministic repairs applied before re-validation (bounded; never invents content).

Repairs only reshape text that already exists (casing, trimming, splitting, URL removal).
When a field cannot be repaired from its own text, a caller-supplied grounded fallback
(e.g. derived from the actionName or the canonical intent) is used.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from app.core import constants as C
from app.validation import text_rules as T
from app.validation.url_safety import sanitize_step, sanitize_text

_TITLE_DROP = frozenset(
    {"the", "a", "an", "your", "my", "of", "on", "in", "for", "to", "with", "and", "how", "fix", "fixing", "fixes",
     "troubleshoot", "troubleshooting", "issue", "issues", "problem", "problems", "guide", "help", "solution",
     "solutions", "phone", "device", "galaxy", "samsung", "is", "are", "keeps", "when"}
)
_DESC_FILLER = ("really", "very", "just", "easily", "simply", "also", "quickly", "the", "a", "an", "your", "that", "which", "any")
_DESC_BAD_ENDINGS = frozenset({"the", "a", "an", "to", "and", "or", "of", "your", "for", "with", "on", "in", "by", "from", "that"})
_DESC_TAILS = ("on your phone", "for you", "on your device")
_THIRD_PERSON = re.compile(r"^(?P<stem>[a-z]+?)(?:ies|es|s)$")


def _strip_urls(text: str) -> str:
    cleaned, _ = sanitize_text(text or "")
    return re.sub(r"\s+", " ", cleaned).strip()


# ----------------------------------------------------------------------------- title
def repair_title(title: str, fallback: str) -> str:
    """Sentence case, 2–3 words. `fallback` must itself be a grounded short phrase."""
    raw = _strip_urls(title)
    raw = re.sub(r"[\"“”'‘’`.!?:;,()\[\]]", " ", raw)
    toks = [t for t in raw.split() if t]
    if not (C.TITLE_MIN_WORDS <= len(toks) <= C.TITLE_MAX_WORDS):
        toks = [t for t in toks if t.lower() not in _TITLE_DROP] or toks
    if len(toks) > C.TITLE_MAX_WORDS:
        toks = toks[: C.TITLE_MAX_WORDS]
    candidate = T.to_sentence_case(" ".join(toks))
    if C.TITLE_MIN_WORDS <= T.count_words(candidate) <= C.TITLE_MAX_WORDS and T.is_sentence_case(candidate):
        return candidate
    fb = T.to_sentence_case(re.sub(r"[.!?:;,]", "", fallback or "").strip())
    fb_toks = fb.split()[: C.TITLE_MAX_WORDS]
    if len(fb_toks) < C.TITLE_MIN_WORDS:
        fb_toks = (fb_toks + ["troubleshooting"])[: C.TITLE_MAX_WORDS]
    return T.to_sentence_case(" ".join(fb_toks))


# ------------------------------------------------------------------------------ goal
_TOPIC_STRIP_TAIL = re.compile(r"(?i)\s+(?:troubleshooting|configuration|issues?|problems?|fix)$")


def clean_topic(topic: str, fallback: str) -> str:
    t = _strip_urls(topic)
    t = re.sub(r"[^A-Za-z0-9 \-&'/+]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    prev = None
    while prev != t:
        prev = t
        t = _TOPIC_STRIP_TAIL.sub("", t).strip()
    toks = t.split()[: C.TOPIC_MAX_WORDS]
    if not toks:
        toks = re.sub(r"[^A-Za-z0-9 \-]", " ", fallback or "General").split()[: C.TOPIC_MAX_WORDS] or ["General"]
    return T.to_title_case(" ".join(toks)).replace(" To ", " to ")


def build_goal(topic: str, kind: str, fallback_topic: str = "General") -> str:
    kind = kind if kind in C.GOAL_KINDS else C.GOAL_KIND_TROUBLESHOOTING
    return f"{C.GOAL_PREFIX}{clean_topic(topic, fallback_topic)} {kind}"


# ------------------------------------------------------------------------ actionName
def repair_action_name(name: str, fallback: str = "Review Settings") -> str:
    n = _strip_urls(name)
    n = re.sub(r"[\"“”`.!?:;()\[\]]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    toks = n.split()[: C.ACTION_NAME_MAX_WORDS]
    if not toks:
        toks = fallback.split()
    cleaned = " ".join(toks)
    # Minimal intervention: a name that already is Title Case keeps its casing ("Turn On Power Saving").
    return cleaned if T.is_title_case(cleaned) else T.to_title_case(cleaned)


# ----------------------------------------------------------------------- description
def _base_form(word: str) -> str:
    low = word.lower()
    special = {"lets": "let", "helps": "help", "makes": "make", "gives": "give", "reduces": "reduce",
               "improves": "improve", "saves": "save", "stops": "stop", "fixes": "fix", "allows": "allow",
               "prevents": "prevent", "restores": "restore", "clears": "clear", "turns": "turn", "keeps": "keep",
               "limits": "limit", "extends": "extend", "frees": "free", "resets": "reset", "updates": "update",
               "removes": "remove", "shows": "show", "opens": "open", "changes": "change", "lowers": "lower",
               "boosts": "boost", "applies": "apply", "enables": "enable", "disables": "disable"}
    if low in special:
        return special[low]
    return low


def description_from_action_name(action_name: str) -> str:
    phrase = re.sub(r"[^A-Za-z0-9 \-]", " ", action_name or "").strip().lower()
    phrase = re.sub(r"\s+", " ", phrase)
    return fit_description(f"It will {phrase}" if phrase else "It will help resolve this issue")


def fit_description(desc: str) -> str:
    """Force 'It will …' with 5–7 words by trimming filler / padding with a neutral tail."""
    toks = desc.split()
    if len(toks) > C.DESCRIPTION_MAX_WORDS:
        head, rest = toks[:2], toks[2:]
        for filler in _DESC_FILLER:
            while len(head) + len(rest) > C.DESCRIPTION_MAX_WORDS and filler in [r.lower() for r in rest]:
                idx = [r.lower() for r in rest].index(filler)
                rest.pop(idx)
        toks = head + rest
    if len(toks) > C.DESCRIPTION_MAX_WORDS:
        toks = toks[: C.DESCRIPTION_MAX_WORDS]
    while len(toks) > C.DESCRIPTION_MIN_WORDS and toks[-1].lower().strip(",") in _DESC_BAD_ENDINGS:
        toks.pop()
    if len(toks) < C.DESCRIPTION_MIN_WORDS:
        for tail in _DESC_TAILS:
            if C.DESCRIPTION_MIN_WORDS <= len(toks) + len(tail.split()) <= C.DESCRIPTION_MAX_WORDS:
                toks = toks + tail.split()
                break
    while len(toks) < C.DESCRIPTION_MIN_WORDS:
        toks.append("quickly" if "quickly" not in toks else "again")
    return " ".join(toks).rstrip(",;:")


def repair_description(desc: str, action_name: str) -> str:
    d = _strip_urls(desc)
    d = re.sub(r"[\"“”`]", "", d).strip().rstrip(".!;:,")
    if not d:
        return description_from_action_name(action_name)
    low = d.lower()
    if low.startswith("it will "):
        body = d[len("it will ") :]
    elif re.match(r"(?i)^(?:this|that)(?:\s+(?:action|step|setting|option|fix))?\s+will\s+", d):
        body = re.sub(r"(?i)^(?:this|that)(?:\s+(?:action|step|setting|option|fix))?\s+will\s+", "", d)
    elif low.startswith("will "):
        body = d[5:]
    else:
        first, _, rest = d.partition(" ")
        body = (_base_form(first) + (" " + rest if rest else "")).strip()
    body = body[:1].lower() + body[1:] if body and not T.is_acronym(body.split()[0]) else body
    candidate = fit_description(f"{C.DESCRIPTION_PREFIX} {body}".strip())
    if candidate.split()[-1].lower() in _DESC_BAD_ENDINGS:
        return description_from_action_name(action_name)
    return candidate


# ----------------------------------------------------------------------------- steps
def repair_steps(steps: Iterable[str]) -> list[str]:
    """URL-sanitise, normalise, split compound steps and drop duplicates."""
    out: list[str] = []
    for step in steps:
        if not isinstance(step, str) or not step.strip():
            continue
        for piece in sanitize_step(step):
            for atom in T.expand_step(piece):
                atom = atom.strip()
                if atom and T.count_words(atom) >= 1:
                    out.append(atom)
    return T.dedupe_consecutive(out)


def repair_variation(text: str) -> Optional[str]:
    cleaned = _strip_urls(text)
    cleaned = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", cleaned).strip().strip('"').strip()
    return cleaned or None
