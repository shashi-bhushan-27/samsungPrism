"""Deeplink mapping (pipeline stage 2): action → exact catalog entry → verbatim Deeplink objects.

The URI placed in a response is always `entry.uri` of a registry entry (or the reserved
dummy_positive), never text produced by a model. In `llm` mode (ablation baseline) the model's
answer is only accepted if it is byte-identical to a catalog URI.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import schema
from app.catalog.registry import CatalogRegistry
from app.core.constants import CATEGORY_AUTO, CATEGORY_MANUAL, DUMMY_POSITIVE_URI
from app.llm.base import LLMError, LLMProvider
from app.llm.parsing import ParseError, parse_json_loose
from app.llm.prompts import MAPPING_SCHEMA, MAPPING_SYSTEM, mapping_prompt
from app.models.internal import ExtractedAction, TokenUsage
from app.retrieval.hybrid import Candidate, HybridRetriever
from app.retrieval.reranker import MappingDecision, TargetResolver
from app.validation.url_safety import sanitize_text

_OFF = re.compile(r"(?i)\bturn\s+off\b|\bdisable\b|\bswitch\s+off\b|\bdeactivate\b|\btoggle\s+off\b")


@dataclass
class MappingOutcome:
    decision: MappingDecision
    actionable: Optional[schema.Deeplink]
    validation: Optional[schema.ValidationDeepLink]
    retrieval_ms: float = 0.0
    usage: TokenUsage = field(default_factory=TokenUsage)
    fabricated: Optional[str] = None
    cost_usd: Optional[float] = 0.0

    @property
    def uri(self) -> Optional[str]:
        return self.actionable.deeplink if self.actionable is not None else None


def _intended_off(action: ExtractedAction, decision: MappingDecision) -> bool:
    prim = decision.primary
    if prim is not None and prim.kind == "control" and prim.state:
        return prim.state == "off"
    return bool(_OFF.search(" ".join(action.steps)))


def validation_for(action: ExtractedAction, decision: MappingDecision) -> Optional[schema.ValidationDeepLink]:
    """Attach the catalog validation rule unless it contradicts the action's intended state."""
    if decision.kind != "catalog" or decision.doc is None:
        return None
    rule = decision.doc.entry.validation
    if rule is None:
        return None
    if rule.result_type == "boolean" and (rule.condition in (None, "equal")) and rule.value in ("true", "false"):
        want = "false" if _intended_off(action, decision) else "true"
        if rule.value != want:
            return None
    return rule.to_schema()


def _clean(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    cleaned, _ = sanitize_text(text)
    return cleaned.strip() or None


class DeeplinkMapper:
    def __init__(
        self,
        resolver: TargetResolver,
        registry: CatalogRegistry,
        *,
        mode: str = "hybrid",
        llm: Optional[LLMProvider] = None,
        llm_model: Optional[str] = None,
    ):
        self.registry = registry
        self.mode = mode
        self.llm = llm
        self.llm_model = llm_model
        self.resolver = resolver
        if mode == "bm25":
            self.resolver = TargetResolver(resolver.index, resolver.retriever, resolver.provider,
                                           top_k_rerank=resolver.top_k_rerank, min_score=resolver.min_score,
                                           min_margin=resolver.min_margin, use_dense=False)
        elif mode == "dense":
            self.resolver = TargetResolver(resolver.index, resolver.retriever, resolver.provider,
                                           top_k_rerank=resolver.top_k_rerank, min_score=resolver.min_score,
                                           min_margin=resolver.min_margin, use_bm25=False)

    async def map(self, action: ExtractedAction, *, domain: Optional[str] = None) -> MappingOutcome:
        t0 = time.perf_counter()
        usage = TokenUsage()
        fabricated: Optional[str] = None
        cost: Optional[float] = 0.0
        if self.mode == "rules":
            decision = self._rules(action, domain)
        elif self.mode == "llm":
            decision, usage, fabricated, cost = await self._llm(action, domain)
        else:
            decision = self.resolver.resolve(action, domain=domain)
        ms = (time.perf_counter() - t0) * 1000
        return self._outcome(action, decision, ms, usage, fabricated, cost)

    def _outcome(self, action, decision, ms, usage, fabricated, cost) -> MappingOutcome:
        actionable: Optional[schema.Deeplink] = None
        if action.category != CATEGORY_MANUAL:
            if decision.kind == "catalog" and decision.doc is not None:
                entry = self.registry.get_entry(decision.doc.uri)
                if entry is not None:  # defensive: only registry objects are serialised
                    actionable = entry.to_deeplink()
            elif decision.kind == "dummy" and action.category == CATEGORY_AUTO:
                actionable = schema.Deeplink(
                    deeplink=DUMMY_POSITIVE_URI,
                    description=_clean(decision.dummy_description) or "Open the Settings screen for this step",
                    message=_clean(decision.dummy_message) or "",
                )
        validation = validation_for(action, decision) if actionable is not None else None
        return MappingOutcome(decision, actionable, validation, ms, usage, fabricated, cost)

    # ----------------------------------------------------------- ablation modes
    def _rules(self, action: ExtractedAction, domain: Optional[str]) -> MappingDecision:
        """Pure rules: label/keyword matching over the whole catalog, no BM25 or embeddings."""
        r = self.resolver
        path = r._path_with_hints(action)
        from app.retrieval.ui_path import primary_interaction

        primary = primary_interaction(path, action.action_name)
        if action.category == CATEGORY_MANUAL:
            return MappingDecision("none", None, 0.0, "manual_action", path, primary)
        cands = [Candidate(doc) for doc in r.index.docs]
        return r._decide(action, path, primary, cands, domain)

    async def _llm(self, action: ExtractedAction, domain: Optional[str]):
        r = self.resolver
        path = r._path_with_hints(action)
        from app.retrieval.ui_path import primary_interaction

        primary = primary_interaction(path, action.action_name)
        if action.category == CATEGORY_MANUAL:
            return MappingDecision("none", None, 0.0, "manual_action", path, primary), TokenUsage(), None, 0.0
        if self.llm is None:
            return MappingDecision("none", None, 0.0, "llm_unavailable", path, primary), TokenUsage(), None, None
        lines = [
            f"{d.uri} | {d.entry.description} | {d.entry.message or ''} | {(d.entry.qna_description or '')[:90]}"
            for d in r.index.docs
        ]
        try:
            resp = await self.llm.generate_structured(
                system=MAPPING_SYSTEM, prompt=mapping_prompt(action.action_name, action.steps, lines),
                schema=MAPPING_SCHEMA, model=self.llm_model, max_output_tokens=256,
            )
        except LLMError:
            return MappingDecision("none", None, 0.0, "llm_error", path, primary), TokenUsage(), None, None
        cost = self.llm.estimate_cost(resp.usage, resp.model)
        try:
            answer = str(parse_json_loose(resp.text).get("deeplink", "")).strip()
        except ParseError:
            answer = resp.text.strip()
        doc = next((d for d in r.index.docs if d.uri == answer), None)
        if doc is not None:
            return MappingDecision("catalog", doc, 1.0, "llm_choice", path, primary), resp.usage, None, cost
        fabricated = answer if answer and answer.upper() != "NONE" else None
        decision = r._fallback(action, path, primary, [], "llm_none" if fabricated is None else "llm_fabricated_uri")
        return decision, resp.usage, fabricated, cost
