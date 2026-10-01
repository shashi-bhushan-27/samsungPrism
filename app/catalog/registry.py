"""Canonical deeplink catalog registry: exact `uri -> entry` lookups, never normalised."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Iterable, Optional

from app.core.constants import DUMMY_POSITIVE_URI, is_dummy_uri
from app.models.catalog import CatalogEntry, ValidationRule
from app.models.internal import LoadIssue


def _meta_key(e: CatalogEntry) -> str:
    return re.sub(r"\s+", " ", f"{e.description}|{e.message or ''}".lower()).strip()


class CatalogRegistry:
    """Immutable registry built once at start-up.

    * Duplicate URIs: first occurrence wins (file order), the rest are reported.
    * Ambiguous metadata: different URIs with identical description+message are grouped so
      the resolver can treat them as interchangeable-or-ambiguous instead of guessing.
    """

    def __init__(self, entries: Iterable[CatalogEntry], *, fingerprint: str = "", issues: Optional[list[LoadIssue]] = None):
        self.issues: list[LoadIssue] = list(issues or [])
        self._by_uri: dict[str, CatalogEntry] = {}
        self.duplicate_uris: dict[str, list[int]] = {}
        ordered: list[CatalogEntry] = []
        self.dummy_uri: str = DUMMY_POSITIVE_URI
        self.dummy_entry: Optional[CatalogEntry] = None
        for e in entries:
            if is_dummy_uri(e.uri):  # the reserved placeholder is never a retrievable target
                self.dummy_uri, self.dummy_entry = e.uri, e
                continue
            if e.uri in self._by_uri:
                first = self._by_uri[e.uri]
                self.duplicate_uris.setdefault(e.uri, [first.index]).append(e.index)
                conflict = _meta_key(first) != _meta_key(e)
                self.issues.append(
                    LoadIssue("deeplinks.json", "duplicate_uri_conflict" if conflict else "duplicate_uri", f"{e.uri} (record {e.index})")
                )
                continue
            self._by_uri[e.uri] = e
            ordered.append(e)
        self.entries: tuple[CatalogEntry, ...] = tuple(ordered)
        self._validation: dict[str, ValidationRule] = {}
        for e in self.entries:
            if e.validation is not None:
                prev = self._validation.get(e.validation.deeplink)
                if prev is not None and prev != e.validation:
                    self.issues.append(LoadIssue("deeplinks.json", "validation_uri_conflict", e.validation.deeplink))
                    continue
                self._validation.setdefault(e.validation.deeplink, e.validation)
        groups: dict[str, list[str]] = defaultdict(list)
        for e in self.entries:
            groups[_meta_key(e)].append(e.uri)
        self.ambiguous_groups: list[list[str]] = [uris for uris in groups.values() if len(uris) > 1]
        self._ambiguous_of: dict[str, tuple[str, ...]] = {u: tuple(g) for g in self.ambiguous_groups for u in g}
        self.fingerprint = fingerprint or hashlib.sha256(
            "\n".join(f"{e.uri}\t{e.description}" for e in self.entries).encode("utf-8")
        ).hexdigest()

    # --- exact lookups -------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.entries)

    def get_entry(self, uri: str) -> Optional[CatalogEntry]:
        return self._by_uri.get(uri) if isinstance(uri, str) else None

    def resolve_catalog_deeplink(self, uri: str) -> CatalogEntry:
        entry = self.get_entry(uri)
        if entry is None:
            raise KeyError(f"URI not in catalog: {uri!r}")
        return entry

    def is_valid_catalog_deeplink(self, uri: object, *, allow_dummy: bool = False) -> bool:
        if not isinstance(uri, str):
            return False
        if is_dummy_uri(uri):
            return allow_dummy and uri == self.dummy_uri
        return uri in self._by_uri

    def get_validation_rule(self, uri: str) -> Optional[ValidationRule]:
        return self._validation.get(uri) if isinstance(uri, str) else None

    def ambiguous_with(self, uri: str) -> tuple[str, ...]:
        return self._ambiguous_of.get(uri, ())

    def servable(self, entry: CatalogEntry) -> bool:
        """An entry can be emitted only if it yields a schema-valid Deeplink."""
        return bool(entry.description.strip())

    def stats(self) -> dict[str, object]:
        return {
            "entries": len(self.entries),
            "with_validation": sum(1 for e in self.entries if e.validation),
            "with_qna": sum(1 for e in self.entries if e.qna_description),
            "with_classes": sum(1 for e in self.entries if e.classes),
            "duplicate_uris": len(self.duplicate_uris),
            "ambiguous_groups": len(self.ambiguous_groups),
            "issues": len(self.issues),
            "fingerprint": self.fingerprint[:16],
        }
