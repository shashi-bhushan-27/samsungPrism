"""Builds every component once (data, indexes, models, cache, services). No per-request rebuilding."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import schema
from app.cache.semantic import PlanCache
from app.cache.store import CacheStore, MemoryCacheStore, SqliteCacheStore
from app.catalog.loaders import link_queries_to_siis, load_catalog, parse_queries, parse_siis, read_json
from app.catalog.registry import CatalogRegistry
from app.core.config import MODEL_PRICING_USD_PER_MTOK, Settings
from app.core.constants import PIPELINE_VERSION
from app.llm.base import LLMProvider, NullLLMProvider
from app.llm.fake import FakeLLMProvider
from app.models.internal import LoadIssue, QueryRecord, SiisDoc
from app.retrieval.embeddings import EmbeddingProvider, build_embedding_provider
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.indexes import CatalogIndex, SiisIndex
from app.retrieval.reranker import TargetResolver
from app.services.deeplink_mapping import DeeplinkMapper
from app.services.query_enrichment import QueryEnricher, fingerprint_text
from app.services.siis_retrieval import SiisRetriever
from app.services.structure_extraction import StructureExtractor
from app.services.troubleshooting import TroubleshootingService
from app.validation.business_rules import check_variations, validate_plan
from app.validation.report import ValidationReport

log = logging.getLogger(__name__)


@dataclass
class Components:
    settings: Settings
    registry: CatalogRegistry
    queries: list[QueryRecord]
    siis_docs: list[SiisDoc]
    links: dict[str, str]
    enricher: QueryEnricher
    embedder: EmbeddingProvider
    catalog_index: CatalogIndex
    siis_index: SiisIndex
    resolver: TargetResolver
    mapper: DeeplinkMapper
    llm: LLMProvider
    extractor: StructureExtractor
    cache: PlanCache
    siis_retriever: SiisRetriever
    service: TroubleshootingService
    issues: list[LoadIssue] = field(default_factory=list)
    health: dict[str, Any] = field(default_factory=dict)
    timings_ms: dict[str, float] = field(default_factory=dict)


def price_lookup(settings: Settings):
    def _lookup(model: str):
        p = settings.price_for(model)
        if p is not None:
            return p
        for key in sorted(MODEL_PRICING_USD_PER_MTOK, key=len, reverse=True):
            if model and model.startswith(key):
                return settings.price_for(key)
        return None

    return _lookup


def build_llm(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "none":
        return NullLLMProvider()
    if settings.llm_provider == "fake":
        return FakeLLMProvider()
    from app.llm.gemini import GeminiProvider

    key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else ""
    if not key:
        # Serve cached plans and deterministic fallbacks; /health reports llm=false (not ready).
        log.warning("GEMINI_API_KEY is not set: LLM disabled, cold requests use the rules extractor")
        return NullLLMProvider()
    return GeminiProvider(
        api_key=key,
        model=settings.llm_model,
        fallback_models=settings.fallback_models,
        base_url=settings.gemini_base_url,
        timeout_s=settings.llm_timeout_s,
        attempts_per_model=settings.llm_attempts_per_model,
        thinking_level=settings.llm_thinking_level,
        seed=settings.llm_seed,
        max_output_tokens=settings.llm_max_output_tokens,
        price_lookup=price_lookup(settings),
        hedge_after_s=settings.llm_hedge_after_s,
        max_concurrency=settings.llm_max_concurrency,
    )


def make_plan_validator(registry: CatalogRegistry):
    def _validate(payload: dict[str, Any]) -> bool:
        try:
            resp = schema.ContextDeeplinkResponse.model_validate(payload["response"])
        except Exception:
            return False
        if not resp.contexts:
            return False
        if not validate_plan(resp, registry).ok:
            return False
        r = ValidationReport()
        check_variations("", payload.get("query_variations"), r)
        return r.ok

    return _validate


def build_components(
    settings: Settings,
    *,
    llm: Optional[LLMProvider] = None,
    embedder: Optional[EmbeddingProvider] = None,
    cache_store: Optional[CacheStore] = None,
    import_prewarm: bool = True,
) -> Components:
    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    data_dir = settings.resolved_data_dir
    issues: list[LoadIssue] = []

    entries, cat_issues, fp = load_catalog(data_dir / "deeplinks.json")
    registry = CatalogRegistry(entries, fingerprint=fp, issues=cat_issues)
    issues.extend(registry.issues)
    queries: list[QueryRecord] = []
    docs: list[SiisDoc] = []
    if (data_dir / "queries.json").exists():
        queries, qi = parse_queries(read_json(data_dir / "queries.json"))
        issues.extend(qi)
    if (data_dir / "siis_responses.json").exists():
        docs, si = parse_siis(read_json(data_dir / "siis_responses.json"))
        issues.extend(si)
    links, li = link_queries_to_siis(queries, docs) if queries and docs else ({}, [])
    issues.extend(li)
    q_by_doc: dict[str, list[str]] = {}
    for q in queries:
        if q.id in links:
            q_by_doc.setdefault(links[q.id], []).append(q.text)
    timings["data_load"] = (time.perf_counter() - t0) * 1000

    t1 = time.perf_counter()
    embedder = embedder or build_embedding_provider(settings)
    embedder.warmup()
    timings["embedding_model_load"] = (time.perf_counter() - t1) * 1000

    t1 = time.perf_counter()
    index_dir = settings.index_dir
    catalog_index = CatalogIndex(registry, embedder, index_dir=index_dir, k1=settings.bm25_k1, b=settings.bm25_b)
    siis_index = SiisIndex(docs, q_by_doc, embedder, index_dir=index_dir, k1=settings.bm25_k1, b=settings.bm25_b)
    timings["index_build_or_load"] = (time.perf_counter() - t1) * 1000

    vocab = [d.text for d in docs] + [q.text for q in queries] + [
        " ".join(filter(None, [e.description, e.message, e.qna_description])) for e in registry.entries
    ]
    enricher = QueryEnricher(vocab)
    retriever = HybridRetriever(
        catalog_index,
        top_k_bm25=settings.retrieval_top_k_bm25,
        top_k_dense=settings.retrieval_top_k_dense,
        w_bm25=settings.hybrid_w_bm25,
        w_dense=settings.hybrid_w_dense,
    )
    resolver = TargetResolver(
        catalog_index,
        retriever,
        embedder,
        top_k_rerank=settings.retrieval_top_k_rerank,
        min_score=settings.deeplink_min_score,
        min_margin=settings.deeplink_min_margin,
    )
    llm = llm or build_llm(settings)
    mapper = DeeplinkMapper(resolver, registry, mode=settings.deeplink_mapper, llm=llm, llm_model=settings.llm_model)
    extractor = StructureExtractor(llm, model=settings.llm_model, max_repairs=settings.llm_repair_retries)

    t1 = time.perf_counter()
    if cache_store is None:
        cache_store = (
            SqliteCacheStore(settings.resolved_cache_path) if settings.cache_backend == "sqlite" else MemoryCacheStore()
        )
    cache = PlanCache(
        cache_store,
        embedder,
        {"catalog": registry.fingerprint, "pipeline": PIPELINE_VERSION},
        threshold=settings.semantic_cache_threshold,
        no_concept_threshold=settings.semantic_cache_threshold_no_concept,
        margin=settings.semantic_cache_margin,
        top_k=settings.semantic_cache_top_k,
        key_min_similarity=settings.cache_key_min_similarity,
        validator=make_plan_validator(registry),
        kb_fingerprints={d.id: fingerprint_text(d.text) for d in docs},
    )
    load_stats = cache.warm_load()
    prewarm_stats = {"imported": 0, "skipped": 0}
    if import_prewarm and settings.resolved_prewarm_path.exists():
        prewarm_stats = cache.import_jsonl(settings.resolved_prewarm_path)
    timings["cache_load"] = (time.perf_counter() - t1) * 1000

    siis_retriever = SiisRetriever(
        siis_index, enricher, min_score=settings.siis_retrieval_min_score, min_margin=settings.siis_retrieval_min_margin,
        no_concept_min_dense=settings.siis_no_concept_min_dense,
    )
    service = TroubleshootingService(
        settings=settings,
        registry=registry,
        enricher=enricher,
        embedder=embedder,
        cache=cache,
        siis_retriever=siis_retriever,
        extractor=extractor,
        mapper=mapper,
        llm=llm,
    )
    timings["total"] = (time.perf_counter() - t0) * 1000
    comps = Components(
        settings=settings, registry=registry, queries=queries, siis_docs=docs, links=links, enricher=enricher,
        embedder=embedder, catalog_index=catalog_index, siis_index=siis_index, resolver=resolver, mapper=mapper,
        llm=llm, extractor=extractor, cache=cache, siis_retriever=siis_retriever, service=service, issues=issues,
        timings_ms=timings,
    )
    comps.health = {
        "dataset": settings.dataset_label,
        "catalog_entries": len(registry),
        "catalog_issues": len(registry.issues),
        "siis_docs": len(docs),
        "catalog_index_rows": len(catalog_index),
        "siis_index_rows": len(siis_index),
        "embedding": embedder.signature,
        "cache_ready": cache.ready,
        "cache_plans": len(cache),
        "cache_load": load_stats,
        "cache_prewarm": prewarm_stats,
    }
    log.info("components ready %s", {k: round(v, 1) for k, v in timings.items()})
    return comps
