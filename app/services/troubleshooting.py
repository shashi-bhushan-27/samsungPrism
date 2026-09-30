"""End-to-end orchestration of the Smart Guided Troubleshooting Engine.

request → enrichment → exact cache → semantic cache ──hit──→ re-validated cached plan (no LLM)
                                        │miss
                                        ▼
       SIIS context (supplied text, or confident KB retrieval, else `no_siis_context`)
                                        ▼
       [LLM] variations ∥ [LLM] grounded extraction → repair/grounding → grouping
                                        ▼
       hybrid deeplink retrieval → exact target resolution → safe sequencing
                                        ▼
       final gate (schema + rules + URL + catalog) → cache write (validated only) → JSON
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import schema
from app.api.schemas import Meta, TroubleshootResponse
from app.cache.models import ORIGIN_KB, ORIGIN_REQUEST, CacheHit, CacheKey, CacheRecord, make_plan_id
from app.cache.semantic import PlanCache, compatibility, intent_summary
from app.catalog.registry import CatalogRegistry
from app.core.config import Settings
from app.core.constants import (
    CATEGORY_CRITICAL,
    CATEGORY_MANUAL,
    FALLBACK_NO_MATCH,
    FALLBACK_NO_SIIS_CONTEXT,
)
from app.llm.base import LLMError, LLMProvider
from app.llm.parsing import ParseError, parse_json_loose
from app.llm.prompts import VARIATION_REGISTERS, VARIATIONS_SCHEMA, VARIATIONS_SYSTEM, variations_prompt
from app.models.internal import CanonicalIntent, ExtractedAction, ExtractedGoal, SiisDoc
from app.observability.timing import CostMeter, StageTimer
from app.retrieval.embeddings import EmbeddingProvider
from app.services.action_grouping import merge_same_target
from app.services.deeplink_mapping import DeeplinkMapper, MappingOutcome
from app.services.query_enrichment import QueryEnricher, finalize_variations, fingerprint_text
from app.services.sequencing import sequence
from app.services.siis_retrieval import SiisRetriever
from app.services.structure_extraction import ExtractionResult, StructureExtractor
from app.validation import business_rules as BR
from app.validation.report import ValidationReport
from app.validation.repair import build_goal, repair_variation
from app.validation.url_safety import sanitize_text

log = logging.getLogger(__name__)
MODE_WEIGHT = {"llm": 1.0, "llm_repaired": 0.9, "rules": 0.7}


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 503):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class Source:
    origin: str
    text: str
    doc_id: Optional[str]
    confidence: float
    intent: CanonicalIntent


@dataclass
class PipelineResult:
    status_code: int
    body: dict[str, Any]
    telemetry: dict[str, Any] = field(default_factory=dict)


class TroubleshootingService:
    def __init__(
        self,
        *,
        settings: Settings,
        registry: CatalogRegistry,
        enricher: QueryEnricher,
        embedder: EmbeddingProvider,
        cache: PlanCache,
        siis_retriever: Optional[SiisRetriever],
        extractor: StructureExtractor,
        mapper: DeeplinkMapper,
        llm: LLMProvider,
    ):
        self.settings = settings
        self.registry = registry
        self.enricher = enricher
        self.embedder = embedder
        self.cache = cache
        self.siis_retriever = siis_retriever
        self.extractor = extractor
        self.mapper = mapper
        self.llm = llm

    # ================================================================== public
    async def troubleshoot(
        self,
        query: str,
        siis_response: Optional[str] = None,
        *,
        request_id: str = "",
        read_cache: Optional[bool] = None,
        write_cache: Optional[bool] = None,
    ) -> PipelineResult:
        s = self.settings
        read_cache = s.cache_read_enabled if read_cache is None else read_cache
        write_cache = s.cache_write_enabled if write_cache is None else write_cache
        timer = StageTimer()
        meter = CostMeter()
        tel: dict[str, Any] = {"request_id": request_id, "cache_hit": False, "cache_kind": None, "fallback": None,
                               "notes": [], "sources": [], "actions": []}
        siis = siis_response if (siis_response is not None and siis_response.strip()) else None
        echo = self._echo(query)

        with timer.stage("query_enrichment"):
            intent = self.enricher.analyze(query)
        tel["intent"] = intent_summary(intent)
        scope_fp = fingerprint_text(siis) if siis else None
        qvec = None

        # ---------------------------------------------------------- fast path
        if read_cache:
            try:
                hit_result = self._lookup(intent, scope_fp, timer, tel)
            except Exception as exc:  # cache failure never fails the request
                log.exception("cache lookup failed")
                tel["notes"].append(f"cache_error:{type(exc).__name__}")
                hit_result = None
            if hit_result is not None:
                hits, qvec = hit_result
                if hits:
                    with timer.stage("cache_validation"):
                        body = self._from_cache(echo, intent, hits, timer)
                    if body is not None:
                        tel.update(cache_hit=True, cache_kind=hits[0].kind,
                                   cache_similarity=round(min(h.similarity for h in hits), 4),
                                   plan_ids=[h.record.plan_id for h in hits])
                        return self._finish(200, body, timer, meter, tel, model=hits[0].record.model, cache_hit=True)
                    tel["notes"].append("cached_plan_failed_final_gate")

        # ---------------------------------------------------------- SIIS source
        sources: list[Source] = []
        if siis:
            sources.append(Source(ORIGIN_REQUEST, siis, None, 1.0, intent))
        elif s.siis_retrieval_enabled and self.siis_retriever is not None:
            with timer.stage("siis_retrieval"):
                seen: set[str] = set()
                for sub in (intent.sub_intents or (intent,)):
                    vec = qvec if (sub is intent and qvec is not None) else self.embedder.embed([sub.normalized_query])[0]
                    match, rejected = self.siis_retriever.retrieve(sub, vec)
                    tel["notes"].extend(f"siis_reject:{r['doc']}:{r['reason']}" for r in rejected[:3])
                    if match is not None and match.doc.id not in seen:
                        seen.add(match.doc.id)
                        sources.append(Source(ORIGIN_KB, match.doc.text, match.doc.id, min(1.0, match.score), sub))
        tel["sources"] = [{"origin": x.origin, "doc_id": x.doc_id, "confidence": round(x.confidence, 4)} for x in sources]
        if not sources:
            return self._fallback(FALLBACK_NO_SIIS_CONTEXT, echo, query, intent, timer, meter, tel)

        # ------------------------------------------------------------- LLM calls
        with timer.stage("llm_inference"):
            var_task = asyncio.create_task(self._llm_variations(query))
            ext_tasks = [
                asyncio.create_task(self.extractor.extract(query, src.intent, src.text, source_id=src.doc_id))
                for src in sources
            ]
            extractions = await asyncio.gather(*ext_tasks, return_exceptions=True)
            # Paraphrases never extend the cold path: wait a short grace period, else use templates.
            done, _ = await asyncio.wait({var_task}, timeout=s.variations_grace_s)
            if done:
                llm_variations, var_usage, var_cost, var_model = var_task.result()
            else:
                var_task.cancel()
                tel["notes"].append("variations_timeout_templates_used")
                from app.models.internal import TokenUsage

                llm_variations, var_usage, var_cost, var_model = [], TokenUsage(), 0.0, None
        if var_usage.calls:
            meter.add(var_usage, var_cost, var_model)
        variations = finalize_variations(query, llm_variations, self.enricher.template_variations(query, intent))

        goals: list[tuple[ExtractedGoal, ExtractionResult, Source]] = []
        llm_failed = False
        for src, res in zip(sources, extractions):
            if isinstance(res, Exception):
                tel["notes"].append(f"extraction_error:{type(res).__name__}")
                llm_failed = True
                continue
            cost = self.llm.estimate_cost(res.usage, res.model) if res.usage.calls else 0.0
            if res.usage.calls:
                meter.add(res.usage, cost, res.model)
            tel["notes"].extend(res.notes)
            tel.setdefault("extraction_modes", []).append(res.mode)
            if res.mode == "rules" and any(n.startswith("llm_error") for n in res.notes):
                llm_failed = True
            for g in res.goals:
                goals.append((g, res, src))
        model_label = next((r.model for r in extractions if isinstance(r, ExtractionResult) and r.model), s.llm_model)

        if not goals:
            if llm_failed:
                raise ServiceError("extraction_unavailable", "The language model is unavailable and no grounded plan "
                                   "could be derived deterministically.", 503)
            return self._fallback(FALLBACK_NO_MATCH, echo, query, intent, timer, meter, tel, variations=variations)

        # ------------------------------------------------ mapping + sequencing
        contexts: list[schema.Goal] = []
        cacheable_parts: list[tuple[schema.Goal, Source, ExtractionResult]] = []
        for goal, res, src in goals:
            built = await self._build_goal(goal, res, src, intent, timer, tel)
            if built is not None:
                contexts.append(built)
                cacheable_parts.append((built, src, res))
        if not contexts:
            return self._fallback(FALLBACK_NO_MATCH, echo, query, intent, timer, meter, tel, variations=variations)

        # ---------------------------------------------------------- final gate
        with timer.stage("validation"):
            body = self._envelope(echo, variations, contexts, model_label, 0.0, False, None)
            report = BR.validate_envelope(body, self.registry)
        tel["validation"] = {"ok": report.ok, "violations": [f"{v.code}@{v.path}" for v in report.errors][:10]}
        if not report.ok:
            log.error("plan failed final gate: %s", report.summary())
            raise ServiceError("plan_validation_failed", "The generated plan did not pass validation.", 502)

        # ---------------------------------------------------------- cache write
        degraded = any(r.mode == "rules" for _, r, _ in goals)
        if write_cache and not degraded:
            with timer.stage("cache_write"):
                try:
                    self._write_cache(query, intent, variations, contexts, cacheable_parts, siis, model_label, tel)
                except Exception as exc:
                    log.exception("cache write failed")
                    tel["notes"].append(f"cache_write_error:{type(exc).__name__}")
        return self._finish(200, body, timer, meter, tel, model=model_label, cache_hit=False)

    # ================================================================ helpers
    @staticmethod
    def _echo(query: str) -> str:
        cleaned, _ = sanitize_text(query or "")
        cleaned = " ".join(cleaned.split())
        return cleaned or "[link removed]"

    def _lookup(self, intent: CanonicalIntent, scope_fp, timer: StageTimer, tel) -> Optional[tuple[list[CacheHit], Any]]:
        s = self.settings
        targets = list(intent.sub_intents) if intent.sub_intents else [intent]
        hits: list[CacheHit] = []
        qvec = None
        if not intent.sub_intents:
            with timer.stage("exact_cache"):
                hit = self.cache.lookup_exact(intent, scope_fp)
            if hit is not None:
                return [hit], None
        if not s.semantic_cache_enabled and not intent.sub_intents:
            return [], None
        with timer.stage("semantic_cache"):
            vecs = self.embedder.embed([t.normalized_query for t in [intent] + targets])
            qvec = vecs[0]
            if not intent.sub_intents:
                res = self.cache.lookup_semantic(intent, qvec, scope_fp)
                tel["cache_best_similarity"] = round(res.best_similarity, 4)
                if res.rejected:
                    tel["cache_rejections"] = res.rejected[:5]
                return ([res.hit] if res.hit else []), qvec
            # Multi-intent: every part must hit its own plan, otherwise it is a miss.
            for t, v in zip(targets, vecs[1:]):
                h = self.cache.lookup_exact(t, scope_fp)
                if h is None and s.semantic_cache_enabled:
                    h = self.cache.lookup_semantic(t, v, scope_fp, count_miss=False).hit
                if h is None:
                    self.cache.stats["misses"] += 1
                    return [], qvec
                if all(h.record.plan_id != x.record.plan_id for x in hits):
                    hits.append(h)
        return hits, qvec

    def _from_cache(self, echo: str, intent: CanonicalIntent, hits: list[CacheHit], timer) -> Optional[dict]:
        contexts = []
        for h in hits:
            contexts.extend(h.record.payload["response"]["contexts"])
        if len(hits) == 1:
            variations = list(hits[0].record.payload.get("query_variations") or [])
        else:
            variations = self.enricher.template_variations(echo, intent)
        variations = finalize_variations(echo, variations, self.enricher.template_variations(echo, intent))
        try:
            goals = [schema.Goal.model_validate(c) for c in contexts]
        except Exception:
            return None
        body = self._envelope(echo, variations, goals, hits[0].record.model, 0.0, True, None)
        report = BR.validate_envelope(body, self.registry)
        if not report.ok:
            for h in hits:
                self.cache.evict(h.record.plan_id, reason="final_gate")
            return None
        return body

    async def _llm_variations(self, query: str):
        from app.models.internal import TokenUsage

        try:
            resp = await self.llm.generate_structured(
                system=VARIATIONS_SYSTEM, prompt=variations_prompt(query), schema=VARIATIONS_SCHEMA,
                model=self.settings.enrichment_model, temperature=0.4, max_output_tokens=1024,
            )
        except LLMError:
            return [], TokenUsage(), 0.0, None
        cost = self.llm.estimate_cost(resp.usage, resp.model)
        try:
            data = parse_json_loose(resp.text)
        except ParseError:
            return [], resp.usage, cost, resp.model
        out = []
        for reg in VARIATION_REGISTERS:
            v = data.get(reg)
            if isinstance(v, str):
                rv = repair_variation(v)
                if rv:
                    out.append(rv)
        return out, resp.usage, cost, resp.model

    async def _build_goal(
        self, goal: ExtractedGoal, res: ExtractionResult, src: Source, intent: CanonicalIntent, timer: StageTimer, tel
    ) -> Optional[schema.Goal]:
        actions = list(goal.actions)
        t0 = time.perf_counter()
        outcomes: list[MappingOutcome] = [await self.mapper.map(a, domain=src.intent.domain or intent.domain) for a in actions]
        map_ms = (time.perf_counter() - t0) * 1000
        timer.add("deeplink_retrieval", map_ms * 0.5)
        timer.add("deeplink_reranking", map_ms * 0.5)
        for o in outcomes:
            if o.usage.calls:  # llm mapping mode (ablation)
                tel.setdefault("mapping_llm_calls", 0)
                tel["mapping_llm_calls"] += o.usage.calls
        uris = [o.uri if (o.actionable is not None and o.decision.kind == "catalog") else None for o in outcomes]
        actions, kept_uris, notes = merge_same_target(actions, uris)
        tel["notes"].extend(notes)
        # Re-attach outcomes to surviving actions (merged ones keep the first action's outcome).
        by_name = {a.action_name: o for a, o in zip(goal.actions, outcomes)}
        outcomes = []
        for a, u in zip(actions, kept_uris):
            o = by_name[a.action_name]
            if u is None and o.decision.kind == "catalog":
                o = MappingOutcome(o.decision, None, None, o.retrieval_ms, o.usage)  # duplicate target dropped
            outcomes.append(o)
        with timer.stage("sequencing"):
            actions, outcomes = sequence(actions, outcomes)
        schema_actions: list[schema.Action] = []
        kept_actions: list[ExtractedAction] = []
        kept_outcomes: list[MappingOutcome] = []
        for a, o in zip(actions, outcomes):
            sa = self._schema_action(a, o)
            sa = self._repair_action(sa, tel)
            if sa is None:
                continue
            schema_actions.append(sa)
            kept_actions.append(a)
            kept_outcomes.append(o)
            tel["actions"].append({
                "actionName": sa.actionName, "category": a.category, "mapping": o.decision.trace(3),
                "fabricated_uri": o.fabricated, "provenance": a.provenance,
            })
        schema_actions, kept_actions, kept_outcomes = self._repair_goal(schema_actions, kept_actions, kept_outcomes, tel)
        if not schema_actions:
            return None
        score = self._score(res.grounding, kept_actions, kept_outcomes, src.confidence, res.mode)
        return schema.Goal(
            goal=build_goal(goal.topic, goal.goal_kind, intent.topic),
            title=goal.title,
            actions=schema_actions,
            score=score,
        )

    @staticmethod
    def _schema_action(a: ExtractedAction, o: MappingOutcome) -> schema.Action:
        actionable = o.actionable if a.category != CATEGORY_MANUAL else None
        return schema.Action(
            actionName=a.action_name,
            description=a.description,
            stepGroups=[schema.StepGroup(steps=list(a.steps), validationDeeplink=o.validation if actionable else None,
                                         actionableDeeplink=actionable)],
            category=a.category,
        )

    def _repair_action(self, action: schema.Action, tel) -> Optional[schema.Action]:
        """Last-line guard: drop steps that still break a rule; drop the action if nothing survives."""
        report = ValidationReport()
        BR.check_action(action, "$", report, self.registry)
        if report.ok:
            return action
        good_steps = []
        for st in action.stepGroups[0].steps:
            r = ValidationReport()
            BR.check_steps([st], "$", r)
            if r.ok:
                good_steps.append(st)
        if not good_steps:
            tel["notes"].append(f"action_dropped_invalid:{action.actionName}")
            return None
        fixed = action.model_copy(deep=True)
        fixed.stepGroups[0].steps = good_steps
        report = ValidationReport()
        BR.check_action(fixed, "$", report, self.registry)
        if report.ok:
            tel["notes"].append(f"action_repaired:{action.actionName}")
            return fixed
        if report.has("deeplink") or report.has("validation"):
            fixed.stepGroups[0].actionableDeeplink = None
            fixed.stepGroups[0].validationDeeplink = None
            report = ValidationReport()
            BR.check_action(fixed, "$", report, self.registry)
            if report.ok:
                return fixed
        tel["notes"].append(f"action_dropped_invalid:{action.actionName}:{report.summary(3)}")
        return None

    def _repair_goal(self, actions, extracted, outcomes, tel, *, max_rounds: int = 3):
        """Goal-level safety net for rules spanning several actions (fragmentation, duplicate targets)."""
        import re as _re

        for _ in range(max_rounds):
            probe = schema.Goal(goal="Follow these steps to perform this Probe Troubleshooting", title="Probe plan",
                                actions=actions, score=0.5)
            report = ValidationReport()
            BR.check_goal_actions(probe, "$", report)
            if report.ok:
                break
            drop: set[int] = set()
            for v in report.errors:
                m = _re.search(r"actions\[(\d+)\]", v.path)
                if v.code == "action.not_fragmented" and m:
                    drop.add(int(m.group(1)))
                elif v.code == "action.unique_targets":
                    seen: set[str] = set()
                    for i, a in enumerate(actions):
                        dl = a.stepGroups[0].actionableDeeplink
                        if dl is not None and dl.deeplink in seen:
                            a.stepGroups[0].actionableDeeplink = None
                            a.stepGroups[0].validationDeeplink = None
                        elif dl is not None:
                            seen.add(dl.deeplink)
                elif v.code.startswith("ordering"):
                    extracted, pairs = sequence(extracted, list(zip(actions, outcomes)))
                    actions = [p[0] for p in pairs]
                    outcomes = [p[1] for p in pairs]
                elif v.code == "action.unique_names" and actions:
                    seen_names: set[str] = set()
                    for i, a in enumerate(actions):
                        if a.actionName.lower() in seen_names:
                            drop.add(i)
                        seen_names.add(a.actionName.lower())
            if drop:
                tel["notes"].extend(f"goal_repair_dropped:{actions[i].actionName}" for i in sorted(drop))
                actions = [a for i, a in enumerate(actions) if i not in drop]
                extracted = [a for i, a in enumerate(extracted) if i not in drop]
                outcomes = [o for i, o in enumerate(outcomes) if i not in drop]
        return actions, extracted, outcomes

    @staticmethod
    def _score(grounding: float, actions: list[ExtractedAction], outcomes: list[MappingOutcome], source_conf: float, mode: str) -> float:
        """Deterministic confidence: grounding, mapping quality, source confidence, extraction mode."""
        mq = []
        for a, o in zip(actions, outcomes):
            if a.category == CATEGORY_MANUAL:
                mq.append(1.0)
            elif o.decision.kind == "catalog":
                mq.append(1.0)
            elif o.decision.kind == "dummy":
                mq.append(0.8)
            elif a.category == CATEGORY_CRITICAL:
                mq.append(0.9)
            else:
                mq.append(0.6)
        m = sum(mq) / len(mq) if mq else 0.0
        score = 0.35 * grounding + 0.25 * m + 0.20 * source_conf + 0.20 * MODE_WEIGHT.get(mode, 0.7)
        return round(min(1.0, max(0.0, score)), 2)

    def _envelope(self, echo, variations, contexts, model, cost, cache_hit, fallback) -> dict[str, Any]:
        resp = TroubleshootResponse(
            query=echo,
            query_variations=list(variations),
            response=schema.ContextDeeplinkResponse(contexts=list(contexts)),
            meta=Meta(latency_ms=0, cache_hit=cache_hit, model=model or "none", cost_usd=cost, fallback=fallback),
        )
        return resp.to_public_dict()

    def _fallback(self, code, echo, query, intent, timer, meter, tel, variations=None) -> PipelineResult:
        if variations is None:
            variations = finalize_variations(query, [], self.enricher.template_variations(query, intent))
        tel["fallback"] = code
        body = self._envelope(echo, variations, [], self.settings.llm_model, 0.0, False, code)
        return self._finish(200, body, timer, meter, tel, model=self.settings.llm_model, cache_hit=False, fallback=code)

    def _finish(self, status, body, timer, meter, tel, *, model, cache_hit, fallback=None) -> PipelineResult:
        total = timer.total_ms
        body["meta"]["latency_ms"] = int(math.ceil(total))
        body["meta"]["cache_hit"] = cache_hit
        body["meta"]["model"] = model or "none"
        body["meta"]["cost_usd"] = 0.0 if cache_hit else (round(meter.cost_usd, 8) if meter.cost_usd is not None else None)
        if fallback:
            body["meta"]["fallback"] = fallback
        tel.update(
            latency_ms=round(total, 3),
            stages=timer.snapshot(),
            model=model,
            model_calls=meter.usage.calls,
            tokens={"input": meter.usage.input_tokens, "output": meter.usage.output_tokens,
                    "thinking": meter.usage.thinking_tokens, "total": meter.usage.total_tokens},
            cost_usd=body["meta"]["cost_usd"],
            cost_available=meter.cost_usd is not None,
            models_used=meter.models,
            deeplinks={
                "catalog": sum(1 for a in tel.get("actions", []) if a["mapping"]["kind"] == "catalog"),
                "dummy": sum(1 for a in tel.get("actions", []) if a["mapping"]["kind"] == "dummy"),
                "none": sum(1 for a in tel.get("actions", []) if a["mapping"]["kind"] == "none"),
            },
        )
        return PipelineResult(status, body, tel)

    def _write_cache(self, query, intent, variations, contexts, parts, siis, model, tel) -> None:
        plan_intent = intent_summary(intent)

        def admit(k: CacheKey) -> bool:
            return compatibility(intent_summary(self.enricher.analyze(k.text)), plan_intent)[0]

        written = []
        if len(parts) == 1 or siis:
            src = parts[0][1]
            origin = ORIGIN_REQUEST if siis else ORIGIN_KB
            source_fp = fingerprint_text(siis if siis else src.text)
            keys = [CacheKey(query, intent.normalized_query, "query")]
            if intent.symptoms:
                keys.append(CacheKey(intent.canonical_query, intent.canonical_query, "canonical"))
            keys += [CacheKey(v, self.enricher.normalize_query(v), "variation") for v in variations]
            rec = CacheRecord(
                plan_id=make_plan_id(origin, source_fp, intent.normalized_query),
                origin=origin,
                source_id=None if siis else src.doc_id,
                source_fp=source_fp,
                query=query,
                normalized_query=intent.normalized_query,
                intent=plan_intent,
                payload={
                    "query_variations": list(variations),
                    "response": schema.ContextDeeplinkResponse(contexts=list(contexts)).model_dump(mode="json"),
                },
                model=model,
                versions={},
                keys=keys,
                provenance={"sources": tel.get("sources"), "actions": tel.get("actions")},
            )
            if self.cache.put(rec, admit_keys=admit):
                written.append(rec.plan_id)
        else:
            # Multi-intent answer from several KB documents: cache each part under its own sub-intent.
            for goal, src, _res in parts:
                sub = src.intent
                source_fp = fingerprint_text(src.text)
                rec = CacheRecord(
                    plan_id=make_plan_id(ORIGIN_KB, source_fp, sub.normalized_query),
                    origin=ORIGIN_KB,
                    source_id=src.doc_id,
                    source_fp=source_fp,
                    query=sub.raw_query,
                    normalized_query=sub.normalized_query,
                    intent=intent_summary(sub),
                    payload={
                        "query_variations": finalize_variations(sub.raw_query, [], self.enricher.template_variations(sub.raw_query, sub)),
                        "response": schema.ContextDeeplinkResponse(contexts=[goal]).model_dump(mode="json"),
                    },
                    model=model,
                    versions={},
                    keys=[CacheKey(sub.raw_query, sub.normalized_query, "query")]
                    + ([CacheKey(sub.canonical_query, sub.canonical_query, "canonical")] if sub.symptoms else []),
                )
                if self.cache.put(rec):
                    written.append(rec.plan_id)
        tel["cache_written"] = written
