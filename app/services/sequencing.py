"""Deterministic safe sequencing: settings → corrective → optimisation → manual → critical (last).

Critical actions are ordered restart < safe mode < software update < wipe cache < reset
settings < clear data < factory reset. The sort is stable, so the SOURCE order is kept within
a tier; the LLM's ordering never overrides these rules.
"""

from __future__ import annotations

from typing import Sequence, TypeVar

from app.models.internal import ExtractedAction
from app.services.category_rules import order_key

T = TypeVar("T")


def sequence(actions: Sequence[ExtractedAction], payload: Sequence[T]) -> tuple[list[ExtractedAction], list[T]]:
    keyed = [(order_key(a.category, a.action_name, a.steps), i) for i, a in enumerate(actions)]
    order = [i for _, i in sorted(keyed)]
    return [actions[i] for i in order], [payload[i] for i in order]
