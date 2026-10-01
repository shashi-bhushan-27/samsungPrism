"""Catalog and SIIS indexes: built once (batch-embedded), persisted, reused across requests."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from app.catalog.registry import CatalogRegistry
from app.core.constants import PIPELINE_VERSION
from app.models.catalog import CatalogEntry
from app.models.internal import SiisDoc
from app.retrieval.bm25 import BM25Index
from app.retrieval.dense import DenseIndex
from app.retrieval.embeddings import EmbeddingProvider
from app.retrieval.text import fold, token_set, tokens

log = logging.getLogger(__name__)

INDEX_SCHEMA_VERSION = "2"
GENERIC_LABEL_TOKENS = frozenset({"set", "option", "menu", "screen", "page", "detail", "list", "app info"})
# One UI top-level Settings menus (general domain knowledge, not dataset specific).
TOP_LEVEL_MENUS = frozenset(
    token_set(x)
    for x in (
        "Settings", "Connections", "Connected devices", "Modes and Routines", "Sounds and vibration", "Notifications",
        "Display", "Wallpaper and style", "Themes", "Home screen", "Lock screen", "Lock screen and AOD",
        "Security and privacy", "Location", "Safety and emergency", "Accounts and backup", "Google",
        "Advanced features", "Digital Wellbeing and parental controls", "Battery", "Battery and device care",
        "Device care", "Apps", "General management", "Accessibility", "Software update", "About phone",
        "Developer options",
    )
)
ENTITY_TOKENS = frozenset(
    token_set("camera gallery messages contacts chrome youtube keyboard bluetooth wifi hotspot nfc calendar clock email")
)
_LABEL_PATTERNS = (
    re.compile(r"(?i)^(?:open|go\s+to|launch|view|see|check)\s+(?:the\s+)?(?P<l>.+)$"),
    re.compile(r"(?i)^(?:turn|toggle|switch)\s+(?:on|off)\s+(?:the\s+)?(?P<l>.+)$"),
    re.compile(r"(?i)^(?:enable|disable|show|hide|change|adjust|set|choose|manage|use)\s+(?:the\s+)?(?P<l>.+)$"),
)


# Generated-catalog phrasing: "Opens the X settings page in device Settings on the device.",
# "Enables X via device Settings on the device.", "Adjusts X to a specified value via TV Settings".
_GENERATED_DESC = re.compile(
    r"(?i)^(?:opens|enables|disables|updates|configures|adjusts|sets)\s+(?:the\s+)?(?P<l>.+?)"
    r"(?:\s+settings\s+page)?(?:\s+to\s+a\s+specified\s+value)?\s+(?:via|in|on)\s+(?:the\s+)?"
    r"(?:(?:device|tv|tablet|phone)\s+settings|device)\b"
)


def _readable_key(entry: CatalogEntry) -> Optional[str]:
    """The validation key names the on-screen control ("Use 24-hour format") when it is human readable."""
    key = entry.validation.key.strip() if entry.validation and entry.validation.key else ""
    return key if key and "_" not in key else None


def candidate_label(entry: CatalogEntry) -> str:
    desc = (entry.description or "").strip().rstrip(".")
    m = _GENERATED_DESC.match(desc)
    if m:
        return _readable_key(entry) or m.group("l").strip()
    for rx in _LABEL_PATTERNS:
        m = rx.match(desc)
        if m:
            return m.group("l").strip()
    return desc


def label_tokens(label: str) -> frozenset[str]:
    return frozenset(t for t in tokens(label) if t not in GENERIC_LABEL_TOKENS)


@dataclass(frozen=True)
class CatalogDoc:
    entry: CatalogEntry
    label: str
    label_tokens: frozenset[str]
    ext_tokens: frozenset[str]
    control: str
    is_root: bool
    is_top_level: bool
    domain: Optional[str]
    entities: frozenset[str]

    @property
    def uri(self) -> str:
        return self.entry.uri


def _control_of(entry: CatalogEntry) -> str:
    raw = (entry.original_type or entry.control_type or "").lower()
    for kind in ("toggle", "switch", "slider", "button", "list", "screen"):
        if kind in raw:
            return "toggle" if kind == "switch" else kind
    desc = (entry.description or "").lower()
    if desc.startswith(("turn on", "turn off", "enable", "disable", "show ")):
        return "toggle"
    return "screen" if desc.startswith(("open", "go to")) else "unknown"


def make_doc(entry: CatalogEntry) -> CatalogDoc:
    label = candidate_label(entry)
    ltoks = label_tokens(label)
    ext = token_set(" ".join(filter(None, [entry.description, entry.message, entry.qna_description])))
    domain = None
    if entry.classes:
        for key in ("category", "domain", "group"):
            if entry.classes.get(key):
                domain = str(entry.classes[key])
                break
    raw_tokens = token_set(label)
    is_root = raw_tokens == frozenset({"set"}) or not raw_tokens
    return CatalogDoc(
        entry=entry,
        label=label,
        label_tokens=ltoks,
        ext_tokens=ext | ltoks,
        control=_control_of(entry),
        is_root=is_root,
        is_top_level=is_root or ltoks in TOP_LEVEL_MENUS or raw_tokens in TOP_LEVEL_MENUS,
        domain=domain,
        entities=frozenset(ltoks & ENTITY_TOKENS),
    )


def bm25_doc_tokens(entry: CatalogEntry) -> list[str]:
    parts = [entry.description, entry.description, entry.message or "", entry.qna_description or ""]
    if entry.classes:
        parts.extend(str(v) for v in entry.classes.values())
    parts.append(entry.original_type or entry.control_type or "")
    return tokens(" ".join(parts))


def _fingerprint(texts: Sequence[str]) -> str:
    h = hashlib.sha256()
    for t in texts:
        h.update(t.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class CatalogIndex:
    """BM25 + dense index over catalog *metadata* (never the opaque URI)."""

    def __init__(
        self,
        registry: CatalogRegistry,
        provider: EmbeddingProvider,
        *,
        index_dir: Optional[Path],
        k1: float = 1.2,
        b: float = 0.75,
    ):
        t0 = time.perf_counter()
        self.registry = registry
        self.provider = provider
        self.docs: list[CatalogDoc] = [make_doc(e) for e in registry.entries if registry.servable(e)]
        self.bm25 = BM25Index([bm25_doc_tokens(d.entry) for d in self.docs], k1=k1, b=b)
        texts = [d.entry.semantic_text() for d in self.docs]
        labels = [d.label or d.entry.description for d in self.docs]
        expected = {
            "kind": "catalog",
            "schema": INDEX_SCHEMA_VERSION,
            "pipeline": PIPELINE_VERSION,
            "embedding": provider.signature,
            "content": _fingerprint(texts + labels),
            "rows": len(texts),
        }
        path = (index_dir / f"catalog_{expected['content'][:16]}_{provider.provider}") if index_dir else None
        loaded = DenseIndex.load(path, expected) if path else None
        self.loaded_from_disk = loaded is not None
        if loaded is None:
            passages = provider.embed_passage(texts)
            self.dense = DenseIndex(passages, dict(expected, part="passages"))
            self.label_dense = DenseIndex(provider.embed(labels), dict(expected, part="labels"))
            if path:
                self.dense.manifest = dict(expected)
                self.dense.save(path)
                self.label_dense.manifest = dict(expected)
                self.label_dense.save(path.with_name(path.name + "_labels"))
        else:
            self.dense = loaded
            lbl = DenseIndex.load(path.with_name(path.name + "_labels"), expected) if path else None
            self.label_dense = lbl or DenseIndex(provider.embed(labels), dict(expected))
        self.build_ms = (time.perf_counter() - t0) * 1000.0
        log.info("catalog index ready docs=%d from_disk=%s ms=%.1f", len(self.docs), self.loaded_from_disk, self.build_ms)

    def __len__(self) -> int:
        return len(self.docs)


class SiisIndex:
    """Searchable view of SIIS knowledge (source text preserved verbatim)."""

    def __init__(
        self,
        docs: Sequence[SiisDoc],
        query_text_by_doc: dict[str, list[str]],
        provider: EmbeddingProvider,
        *,
        index_dir: Optional[Path],
        k1: float = 1.2,
        b: float = 0.75,
    ):
        t0 = time.perf_counter()
        self.docs = list(docs)
        self.query_text_by_doc = query_text_by_doc
        reps = [self._representation(d) for d in self.docs]
        self.bm25 = BM25Index([tokens(r) for r in reps], k1=k1, b=b)
        expected = {
            "kind": "siis",
            "schema": INDEX_SCHEMA_VERSION,
            "pipeline": PIPELINE_VERSION,
            "embedding": provider.signature,
            "content": _fingerprint(reps),
            "rows": len(reps),
        }
        path = (index_dir / f"siis_{expected['content'][:16]}_{provider.provider}") if index_dir else None
        loaded = DenseIndex.load(path, expected) if path else None
        self.loaded_from_disk = loaded is not None
        if loaded is None:
            # Symmetric embedding: incoming complaints are compared with the title + canonical query.
            heads = [self._head(d) for d in self.docs]
            self.dense = DenseIndex(provider.embed(heads), dict(expected))
            if path:
                self.dense.save(path)
        else:
            self.dense = loaded
        self.build_ms = (time.perf_counter() - t0) * 1000.0

    def _head(self, d: SiisDoc) -> str:
        qs = self.query_text_by_doc.get(d.id, [])
        return " . ".join([*(qs[:1]), d.title or "", d.text.split("\n", 1)[0]])

    def _representation(self, d: SiisDoc) -> str:
        qs = self.query_text_by_doc.get(d.id, [])
        return "\n".join([*qs, d.title or "", d.domain or "", d.text])

    def __len__(self) -> int:
        return len(self.docs)


def fold_for_query(text: str) -> str:
    return fold(text)


def as_matrix(vecs: np.ndarray) -> np.ndarray:
    return np.asarray(vecs, dtype=np.float32)
