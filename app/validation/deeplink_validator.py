"""Catalog-integrity gate (PDF §4.2.2): every URI must be an exact, unaltered catalog value."""

from __future__ import annotations

from typing import Optional, Protocol

import schema
from app.core.constants import CATEGORY_AUTO, CATEGORY_MANUAL, DUMMY_POSITIVE_URI
from app.models.catalog import CatalogEntry, ValidationRule
from app.validation.report import ValidationReport


class CatalogLookup(Protocol):
    def get_entry(self, uri: str) -> Optional[CatalogEntry]: ...

    def get_validation_rule(self, uri: str) -> Optional[ValidationRule]: ...


def _enum_value(v: object) -> Optional[str]:
    if v is None:
        return None
    return getattr(v, "value", v)  # type: ignore[return-value]


def validate_actionable(
    dl: schema.Deeplink,
    category: Optional[str],
    catalog: CatalogLookup,
    path: str,
    report: ValidationReport,
) -> None:
    uri = dl.deeplink
    report.check(
        "deeplink.manual_actionable",
        category != CATEGORY_MANUAL,
        path,
        "manual actions must not carry an actionable deeplink",
    )
    if uri == DUMMY_POSITIVE_URI:
        report.check(
            "deeplink.dummy_not_allowed",
            category == CATEGORY_AUTO,
            path,
            "dummy_positive is reserved for auto actions opening an un-indexed Settings screen",
        )
        report.check(
            "deeplink.dummy_description",
            bool((dl.description or "").strip()),
            path,
            "dummy_positive deeplink needs a description of the target screen",
        )
        return
    entry = catalog.get_entry(uri) if isinstance(uri, str) else None
    if not report.check("deeplink.catalog_membership", entry is not None, path, f"URI not in catalog: {uri!r}"):
        return
    assert entry is not None
    expected = entry.to_deeplink()
    same = (
        dl.description == expected.description
        and (dl.message or "") == (expected.message or "")
        and (dl.classes or None) == (expected.classes or None)
        and dl.originalType == expected.originalType
    )
    report.check("deeplink.field_integrity", same, path, "deeplink metadata differs from the catalog record")


def validate_validation(
    vdl: schema.ValidationDeepLink,
    catalog: CatalogLookup,
    path: str,
    report: ValidationReport,
) -> None:
    rule = catalog.get_validation_rule(vdl.deeplink) if isinstance(vdl.deeplink, str) else None
    if not report.check(
        "validation.catalog_membership",
        rule is not None,
        path,
        f"validation URI not in catalog: {vdl.deeplink!r}",
    ):
        return
    assert rule is not None
    same = (
        vdl.key == rule.key
        and _enum_value(vdl.resultType) == rule.result_type
        and _enum_value(vdl.condition) == rule.condition
        and vdl.value == rule.value
    )
    report.check("validation.field_integrity", same, path, "validation rule differs from the catalog record")


def is_valid_catalog_deeplink(uri: object, catalog: CatalogLookup, *, allow_dummy: bool = True) -> bool:
    if not isinstance(uri, str):
        return False
    if uri == DUMMY_POSITIVE_URI:
        return allow_dummy
    return catalog.get_entry(uri) is not None
