"""Internal typed view of a deeplink catalog record. URIs are opaque and kept verbatim."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional

import schema


@dataclass(frozen=True)
class ValidationRule:
    """A toggle-validation rule carried by a catalog record (→ schema.ValidationDeepLink)."""

    deeplink: str
    key: str
    result_type: Optional[str] = None
    condition: Optional[str] = None
    value: Optional[str] = None

    def to_schema(self) -> schema.ValidationDeepLink:
        return schema.ValidationDeepLink(
            deeplink=self.deeplink,
            key=self.key,
            resultType=self.result_type,
            condition=self.condition,
            value=self.value,
        )


@dataclass(frozen=True)
class CatalogEntry:
    uri: str
    description: str
    message: Optional[str]
    qna_description: Optional[str]
    classes: Optional[Mapping[str, str]]
    original_type: Optional[str]
    control_type: Optional[str]
    validation: Optional[ValidationRule]
    index: int
    raw: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}), compare=False, repr=False)

    def to_deeplink(self) -> schema.Deeplink:
        """Copy the catalog record into the contract model without altering any value."""
        kwargs: dict[str, Any] = {"deeplink": self.uri, "description": self.description}
        if self.message is not None:
            kwargs["message"] = self.message
        if self.classes is not None:
            kwargs["classes"] = dict(self.classes)
        if self.original_type is not None:
            kwargs["originalType"] = self.original_type
        return schema.Deeplink(**kwargs)

    def to_validation_deeplink(self) -> Optional[schema.ValidationDeepLink]:
        return self.validation.to_schema() if self.validation else None

    def semantic_text(self) -> str:
        """Descriptive metadata used for retrieval. The URI itself is never included."""
        parts: list[str] = [self.description or "", self.message or "", self.qna_description or ""]
        if self.classes:
            parts.extend(str(v) for v in self.classes.values() if v)
        if self.original_type:
            parts.append(self.original_type)
        if self.control_type:
            parts.append(self.control_type)
        if self.validation and self.validation.key:
            parts.append(self.validation.key.replace("_", " "))
        return " . ".join(p for p in parts if p).strip()
