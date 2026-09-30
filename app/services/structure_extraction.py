"""Grounded structure extraction (pipeline stage 1) with a bounded validate/repair loop.

LLM output is treated as untrusted: it is parsed defensively, every step is URL-sanitised,
split into single interactions, checked against the SOURCE (grounding), categories are decided
by deterministic rules, and text fields are repaired to the contract. The model is re-asked
at most `max_repairs` times; if it stays unusable the deterministic rules extractor is used.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.core.constants import CATEGORIES
from app.llm.base import LLMError, LLMProvider
from app.llm.parsing import ParseError, parse_json_loose
from app.llm.prompts import EXTRACTION_SCHEMA, EXTRACTION_SYSTEM, extraction_prompt, repair_prompt
from app.models.internal import CanonicalIntent, ExtractedAction, ExtractedGoal, TokenUsage
from app.services import category_rules
from app.services.action_grouping import merge_fragments, split_all
from app.services.grounding import SourceIndex, ground_steps, quote_supported
from app.services.rules_extractor import extract_rules
from app.validation import text_rules as T
from app.validation.repair import (
    clean_topic,
    repair_action_name,
    repair_description,
    repair_steps,
    repair_title,
)
from app.validation.url_safety import sanitize_text

log = logging.getLogger(__name__)


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple)):
        return [x for x in v if isinstance(x, (str, int, float))]
    return []


class LlmAction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action_name: str = ""
    description: str = ""
    category: str = "auto"
    steps: list[str] = []
    target_screen: Optional[str] = None
    navigation_path: list[str] = []
    source_quote: Optional[str] = None

    @field_validator("steps", "navigation_path", mode="before")
    @classmethod
    def _lists(cls, v):
        return [str(x) for x in _as_list(v)]

    @field_validator("action_name", "description", mode="before")
    @classmethod
    def _strs(cls, v):
        return "" if v is None else str(v)

    @field_validator("target_screen", "source_quote", mode="before")
    @classmethod
    def _opt(cls, v):
        return None if v is None else str(v)

    @field_validator("category", mode="before")
    @classmethod
    def _cat(cls, v):
        v = str(v or "").strip().lower()
        return v if v in CATEGORIES else "auto"


class LlmGoal(BaseModel):
    model_config = ConfigDict(extra="ignore")
    topic: str = ""
    goal_type: str = "Troubleshooting"
    title: str = ""
    actions: list[LlmAction] = []

    @field_validator("topic", "title", "goal_type", mode="before")
    @classmethod
    def _strs(cls, v):
        return "" if v is None else str(v)


class LlmPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")
    has_solution: bool = True
    no_solution_reason: Optional[str] = None
    goals: list[LlmGoal] = []

    @field_validator("has_solution", mode="before")
    @classmethod
    def _bool(cls, v):
        if isinstance(v, str):
            return v.strip().lower() not in ("false", "no", "0", "")
        return bool(v) if v is not None else True


def coerce_plan(raw: dict[str, Any]) -> LlmPlan:
    """Accept common shape drift: actions at top level, a single goal object, etc."""
    if "goals" not in raw and "actions" in raw:
        raw = {"has_solution": raw.get("has_solution", True), "goals": [raw]}
    if isinstance(raw.get("goals"), dict):
        raw = dict(raw, goals=[raw["goals"]])
    return LlmPlan.model_validate(raw)


@dataclass
class ExtractionResult:
    goals: list[ExtractedGoal]
    has_solution: bool
    usage: TokenUsage
    model: str
    mode: str  # llm | llm_repaired | rules
    llm_ms: float
    attempts: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    grounding: float = 0.0
    source_id: Optional[str] = None


class StructureExtractor:
    def __init__(self, llm: LLMProvider, *, model: Optional[str] = None, max_repairs: int = 1, allow_rules_fallback: bool = True):
        self.llm = llm
        self.model = model
        self.max_repairs = max(0, max_repairs)
        self.allow_rules_fallback = allow_rules_fallback

    async def extract(
        self, query: str, intent: CanonicalIntent, siis_text: str, *, source_id: Optional[str] = None
    ) -> ExtractionResult:
        src = SourceIndex.build(siis_text)
        usage = TokenUsage()
        notes: list[str] = []
        attempts: list[dict[str, Any]] = []
        base_prompt = extraction_prompt(query, siis_text, intent.canonical_query if intent.symptoms else None)
        prompt = base_prompt
        llm_ms = 0.0
        model_used = self.model or self.llm.model_name()
        for attempt in range(1 + self.max_repairs):
            t0 = time.perf_counter()
            try:
                resp = await self.llm.generate_structured(
                    system=EXTRACTION_SYSTEM, prompt=prompt, schema=EXTRACTION_SCHEMA, model=self.model
                )
            except LLMError as exc:
                llm_ms += (time.perf_counter() - t0) * 1000
                notes.append(f"llm_error:{exc.code}")
                attempts.extend(getattr(exc, "attempts", []))
                break
            llm_ms += (time.perf_counter() - t0) * 1000
            usage.add(resp.usage)
            attempts.extend(resp.attempts)
            model_used = resp.model
            try:
                plan = coerce_plan(parse_json_loose(resp.text))
            except (ParseError, ValidationError) as exc:
                notes.append(f"parse_error:{type(exc).__name__}")
                prompt = repair_prompt(base_prompt, [f"the output was not valid JSON for the schema ({str(exc)[:160]})"])
                continue
            goals, problems, grounding = self._postprocess(plan, intent, src, source_id, notes)
            if not plan.has_solution and not goals:
                return ExtractionResult([], False, usage, model_used, "llm" if attempt == 0 else "llm_repaired",
                                        llm_ms, attempts, notes, 0.0, source_id)
            if goals:
                mode = "llm" if attempt == 0 else "llm_repaired"
                return ExtractionResult(goals, True, usage, model_used, mode, llm_ms, attempts, notes, grounding, source_id)
            notes.extend(problems)
            prompt = repair_prompt(base_prompt, problems or ["no grounded action remained after validation"])
        if not self.allow_rules_fallback:
            raise LLMError("extraction failed and rules fallback is disabled", code="extraction_failed")
        actions = extract_rules(siis_text)
        goal, grounding = self._goal_from_actions(actions, None, intent, src, source_id, notes)
        notes.append("rules_fallback")
        goals = [goal] if goal.actions else []
        return ExtractionResult(goals, bool(goals), usage, "rules-extractor", "rules", llm_ms, attempts, notes, grounding, source_id)

    # ------------------------------------------------------------ post-processing
    def _postprocess(
        self, plan: LlmPlan, intent: CanonicalIntent, src: SourceIndex, source_id: Optional[str], notes: list[str]
    ) -> tuple[list[ExtractedGoal], list[str], float]:
        goals: list[ExtractedGoal] = []
        problems: list[str] = []
        supports: list[float] = []
        for g in plan.goals[:3]:
            actions: list[ExtractedAction] = []
            for a in g.actions:
                actions.append(
                    ExtractedAction(
                        action_name=a.action_name,
                        description=a.description,
                        category=a.category,
                        steps=list(a.steps),
                        target_screen=a.target_screen,
                        navigation_path=list(a.navigation_path),
                        source_quote=a.source_quote,
                        proposed_category=a.category,
                    )
                )
            goal, grounding = self._goal_from_actions(actions, g, intent, src, source_id, notes)
            if goal.actions:
                goals.append(goal)
                supports.append(grounding)
            elif g.actions:
                problems.append("every action was dropped: steps must be copied from the SOURCE, imperative, one interaction each")
        return goals, problems, (sum(supports) / len(supports) if supports else 0.0)

    def _goal_from_actions(
        self,
        actions: list[ExtractedAction],
        g: Optional[LlmGoal],
        intent: CanonicalIntent,
        src: SourceIndex,
        source_id: Optional[str],
        notes: list[str],
    ) -> tuple[ExtractedGoal, float]:
        cleaned: list[ExtractedAction] = []
        supports: list[float] = []
        for a in actions:
            steps = repair_steps(a.steps)
            imperative = [s for s in steps if T.is_imperative(s)[0] and T.count_interactions(s) == 1]
            if len(imperative) < len(steps):
                notes.append(f"dropped_non_imperative:{len(steps) - len(imperative)}")
            kept, rep = ground_steps(imperative, src)
            if len(kept) < len(imperative):
                notes.append(f"dropped_ungrounded:{len(imperative) - len(kept)}")
            if not kept:
                notes.append(f"action_dropped_ungrounded:{a.action_name[:40]}")
                continue
            # Generic steps ("Open Settings.") alone do not ground an action; decided after fragment merging.
            has_content = any(sup.supported and sup.sentence_index is not None for sup in rep.supports)
            supports.extend(s.score for s in rep.supports if s.supported)
            name_source = a.action_name or kept[-1]
            name = repair_action_name(name_source)
            decision = category_rules.classify(name, kept, a.proposed_category)
            desc = repair_description(a.description, name)
            cleaned.append(
                ExtractedAction(
                    action_name=name,
                    description=desc,
                    category=decision.category,
                    steps=kept,
                    target_screen=_clean_opt(a.target_screen),
                    navigation_path=[p for p in (_clean_opt(x) for x in a.navigation_path) if p],
                    controls=list(a.controls),
                    source_quote=a.source_quote,
                    proposed_category=a.proposed_category,
                    provenance={
                        "has_grounded_content": has_content,
                        "source_id": source_id,
                        "category_rule": decision.reason,
                        "llm_category": a.proposed_category,
                        "quote_supported": quote_supported(a.source_quote, src),
                        "step_support": [
                            {"step": s.step, "score": s.score, "sentence": s.sentence_index} for s in rep.supports
                        ],
                    },
                )
            )
        merged, n1 = merge_fragments(cleaned)
        merged_content = []
        for a in merged:
            if a.provenance.get("has_grounded_content", True):
                merged_content.append(a)
            else:
                notes.append(f"action_dropped_no_grounded_content:{a.action_name[:40]}")
        split, n2 = split_all(merged_content)
        notes.extend(n1 + n2)
        # Unique, repaired action names (duplicates break the one-action-per-screen rule).
        seen: dict[str, int] = {}
        final: list[ExtractedAction] = []
        for a in split:
            key = a.action_name.lower()
            if key in seen:
                j = seen[key]
                final[j] = ExtractedAction(**{**final[j].__dict__, "steps": T.dedupe_consecutive(final[j].steps + a.steps)})
                notes.append(f"merged_duplicate_name:{a.action_name}")
                continue
            seen[key] = len(final)
            final.append(a)
        topic_raw = g.topic if g is not None else ""
        title_raw = g.title if g is not None else ""
        topic = clean_topic(topic_raw, intent.topic) if topic_raw else clean_topic(intent.topic, intent.topic)
        title = repair_title(title_raw or intent.title_hint, intent.title_hint)
        grounding = sum(supports) / len(supports) if supports else 0.0
        return ExtractedGoal(topic=topic, goal_kind=intent.goal_kind, title=title, actions=final, source_id=source_id), grounding


def _clean_opt(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    cleaned, _ = sanitize_text(v)
    cleaned = cleaned.strip()
    return cleaned or None
