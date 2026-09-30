"""Validation report shared by every gate."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Violation:
    code: str
    path: str
    message: str
    severity: str = ERROR


@dataclass
class ValidationReport:
    violations: list[Violation] = field(default_factory=list)
    # rule -> [checks performed, checks passed]; feeds the rule-compliance metric.
    checks: dict[str, list[int]] = field(default_factory=dict)
    schema_valid: bool = True

    @property
    def ok(self) -> bool:
        return self.schema_valid and not any(v.severity == ERROR for v in self.violations)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == ERROR]

    def add(self, code: str, path: str, message: str, severity: str = ERROR) -> None:
        self.violations.append(Violation(code, path, message, severity))

    def check(self, rule: str, passed: bool, path: str = "", message: str = "", severity: str = ERROR) -> bool:
        bucket = self.checks.setdefault(rule, [0, 0])
        bucket[0] += 1
        if passed:
            bucket[1] += 1
        else:
            self.add(rule, path, message or rule, severity)
        return passed

    def merge(self, other: "ValidationReport") -> "ValidationReport":
        self.violations.extend(other.violations)
        for rule, (n, p) in other.checks.items():
            bucket = self.checks.setdefault(rule, [0, 0])
            bucket[0] += n
            bucket[1] += p
        self.schema_valid = self.schema_valid and other.schema_valid
        return self

    def codes(self) -> Counter:
        return Counter(v.code for v in self.errors)

    def has(self, prefix: str) -> bool:
        return any(v.code.startswith(prefix) for v in self.errors)

    def summary(self, limit: int = 8) -> str:
        errs = self.errors
        head = "; ".join(f"{v.code}@{v.path}: {v.message}" for v in errs[:limit])
        more = f" (+{len(errs) - limit} more)" if len(errs) > limit else ""
        return head + more if errs else "ok"


def merge_reports(reports: Iterable[ValidationReport]) -> ValidationReport:
    out = ValidationReport()
    for r in reports:
        out.merge(r)
    return out
