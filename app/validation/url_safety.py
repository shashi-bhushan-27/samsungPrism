"""Programmatic zero-URL-leak gate (PDF §4.2.1, §7.3).

The LLM is never trusted to omit links. Every human-readable string in a response is
scanned here after generation and again before serialisation. Catalog deeplink values
(keys named ``deeplink``) are opaque identifiers and are checked for catalog membership
elsewhere — they are never rewritten by this module.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Iterable

from app.validation.text_rules import core_clause, split_interactions

_TLDS = (
    "com|net|org|io|co|in|uk|us|de|kr|jp|cn|fr|app|dev|info|biz|me|tv|ly|gl|ai|xyz|edu|gov"
    "|site|online|store|tech|page|link|shop|mobi|ru|br|au|ca|es|it|nl|eu|asia|cc|sg|hk|tw|vn|id"
)

# Any scheme URL, including bixby:// which may only appear inside deeplink fields.
# URL bodies stop at whitespace and closing brackets; trailing sentence punctuation is
# trimmed in find_urls so "(www.x.com)." keeps its ")" and ".".
_SCHEME_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]{1,24}\s*:\s*//[^\s)\]>\"']*")
_PSEUDO_SCHEME_RE = re.compile(r"(?i)\b(?:mailto|tel|sms|javascript|data|intent|market):[^\s,;)\]>\"']+")
_WWW_RE = re.compile(r"(?i)\bwww\d{0,3}\s*\.\s*[^\s,;)\]>\"']+")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]{0,300})\]\(\s*([^)\n]*)\)")
_BARE_DOMAIN_RE = re.compile(
    r"(?i)(?<![\w@.\-])(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+(?:" + _TLDS + r")\b(?:[/:?#][^\s,;)\]]*)?"
)
_DEFANGED_RE = re.compile(
    r"(?i)\bh(?:xx|tt)ps?\s*(?:\[\s*:\s*\]|:)\s*/\s*/\S*|\b[\w\-]+\s*\[\s*\.\s*\]\s*(?:" + _TLDS + r")\b\S*"
)
_SPOKEN_DOMAIN_RE = re.compile(r"(?i)\b[\w\-]+\s+dot\s+(?:com|net|org|co|in|io)\b(?:\s*(?:slash|/)\s*[\w\-/]+)?")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{2,5})?(?:/\S*)?")

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("markdown_link", _MARKDOWN_LINK_RE),
    ("scheme_url", _SCHEME_RE),
    ("defanged_url", _DEFANGED_RE),
    ("pseudo_scheme", _PSEUDO_SCHEME_RE),
    ("www", _WWW_RE),
    ("bare_domain", _BARE_DOMAIN_RE),
    ("spoken_domain", _SPOKEN_DOMAIN_RE),
    ("ip_address", _IP_RE),
)

# A step whose purpose is to send the user to a website is dropped entirely.
_LINK_INTRO_RE = re.compile(
    r"(?i)^\s*(?:please\s+)?(?:visit|browse(?:\s+to)?|go\s+to|navigate\s+to|open|see|check\s+out|refer\s+to|"
    r"read|click|follow|download(?:\s+\w+)?\s+from|find\s+more\s+at|learn\s+more\s+at|log\s+on\s+to|"
    r"sign\s+in\s+(?:at|to)|contact\s+us\s+at)\b"
)
_DANGLING_TAIL_RE = re.compile(
    r"(?i)(?:\b(?:visit|see|at|on|via|from|to|here|website|web\s+page|page|link|site|url|online|and|or)\b\s*|:\s*)+"
    r"([.!?]?)\s*$"
)
_EMPTY_PARENS_RE = re.compile(r"\(\s*[,;:]?\s*\)|\[\s*\]")


@dataclass(frozen=True)
class UrlFinding:
    kind: str
    match: str
    start: int
    end: int


@dataclass(frozen=True)
class UrlLeak:
    path: str
    kind: str
    match: str


def _valid_ip(candidate: str) -> bool:
    host = candidate.split("/")[0].split(":")[0]
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return False
    return True


def find_urls(text: str) -> list[UrlFinding]:
    """Return non-overlapping URL-like spans, earliest first."""
    if not text:
        return []
    found: list[UrlFinding] = []
    for kind, rx in _RULES:
        for m in rx.finditer(text):
            if kind == "markdown_link" and not m.group(2).strip():
                continue
            if kind == "ip_address" and not _valid_ip(m.group(0)):
                continue
            span = m.group(0)
            end = m.end()
            if kind != "markdown_link":
                trimmed = span.rstrip(".,!?:;")
                end -= len(span) - len(trimmed)
                span = trimmed
            found.append(UrlFinding(kind, span, m.start(), end))
    found.sort(key=lambda f: (f.start, -(f.end - f.start)))
    merged: list[UrlFinding] = []
    for f in found:
        if merged and f.start < merged[-1].end:
            continue
        merged.append(f)
    return merged


def contains_url(text: str) -> bool:
    return bool(find_urls(text))


def _cleanup(text: str) -> str:
    text = _EMPTY_PARENS_RE.sub("", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,;:])\s*([.!?])", r"\2", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" ,;:-")


def sanitize_text(text: str) -> tuple[str, list[UrlFinding]]:
    """Remove URL spans. Markdown links keep their anchor text unless it is a URL itself."""
    findings = find_urls(text)
    if not findings:
        return text, []
    out: list[str] = []
    cursor = 0
    for f in findings:
        out.append(text[cursor : f.start])
        if f.kind == "markdown_link":
            m = _MARKDOWN_LINK_RE.fullmatch(f.match)
            anchor = m.group(1).strip() if m else ""
            if anchor and not find_urls(anchor):
                out.append(anchor)
        cursor = f.end
    out.append(text[cursor:])
    cleaned = _cleanup("".join(out))
    # Repeat until stable (anchor text or leftovers may expose another pattern).
    if find_urls(cleaned):
        cleaned, more = sanitize_text(cleaned)
        findings = findings + more
    return cleaned, findings


def _sanitize_clause(clause: str) -> str | None:
    findings = find_urls(clause)
    if not findings:
        return clause
    body = core_clause(clause)
    offset = len(clause) - len(body) if clause.endswith(body) else 0
    intro = _LINK_INTRO_RE.match(body)
    if intro and findings[0].start - (offset + intro.end()) <= 16:
        # "Visit samsung.com/support ..." — the URL is the object of the instruction,
        # so the clause exists only to send the user to a website.
        return None
    cleaned, _ = sanitize_text(clause)
    cleaned = _DANGLING_TAIL_RE.sub(r"\1", cleaned).strip(" ,;")
    if len(re.findall(r"[A-Za-z]{2,}", cleaned)) < 2:
        return None
    return cleaned


def sanitize_step(step: str) -> list[str]:
    """Sanitize one UI step clause by clause.

    Returns the surviving pieces (possibly empty when the step only pointed to a website).
    A step without URLs is returned unchanged as a single-element list.
    """
    if not find_urls(step):
        return [step]
    kept: list[str] = []
    for clause in split_interactions(step):
        cleaned = _sanitize_clause(clause)
        if cleaned:
            if cleaned[-1] not in ".!?":
                cleaned += "."
            kept.append(cleaned[0].upper() + cleaned[1:])
    return kept


def scan_payload(obj: Any, *, path: str = "$", deeplink_keys: Iterable[str] = ("deeplink",)) -> list[UrlLeak]:
    """Walk a JSON-like payload and report every URL outside deeplink identifier fields."""
    keys = frozenset(deeplink_keys)
    leaks: list[UrlLeak] = []

    def walk(node: Any, p: str, key: str | None) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and find_urls(k):
                    leaks.extend(UrlLeak(f"{p}.<key>", f.kind, f.match) for f in find_urls(k))
                walk(v, f"{p}.{k}", str(k))
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, f"{p}[{i}]", key)
        elif isinstance(node, str):
            if key in keys:
                # Deeplink identifiers are opaque catalog values (membership is checked by the
                # deeplink validator); anything that is not a bixby:// URI is a leak.
                if not node.startswith("bixby://"):
                    leaks.append(UrlLeak(p, "non_bixby_deeplink", node))
                return
            leaks.extend(UrlLeak(p, f.kind, f.match) for f in find_urls(node))

    walk(obj, path, None)
    return leaks
