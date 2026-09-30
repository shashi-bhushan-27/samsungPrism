from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import schema
from app.cache.models import ORIGIN_KB, ORIGIN_REQUEST, CacheKey, CacheRecord, make_plan_id
from app.cache.semantic import PlanCache, compatibility, intent_summary
from app.cache.store import MemoryCacheStore, SqliteCacheStore
from app.catalog.loaders import load_catalog
from app.catalog.registry import CatalogRegistry
from app.retrieval.embeddings import HashingEmbeddingProvider
from app.services.query_enrichment import QueryEnricher, fingerprint_text
from app.validation.business_rules import validate_plan

DEV = Path(__file__).resolve().parents[2] / "data" / "dev_fixtures"
VERSIONS = {"catalog": "cat-v1", "pipeline": "p-v1"}


@pytest.fixture(scope="module")
def registry():
    entries, issues, fp = load_catalog(DEV / "deeplinks.json")
    return CatalogRegistry(entries, fingerprint=fp, issues=issues)


@pytest.fixture(scope="module")
def enricher():
    return QueryEnricher()


def validator_for(registry):
    def _v(payload):
        try:
            resp = schema.ContextDeeplinkResponse.model_validate(payload["response"])
        except Exception:
            return False
        return bool(resp.contexts) and validate_plan(resp, registry).ok

    return _v


def sample(name: str) -> dict:
    return json.loads(next((DEV / "samples").glob(f"*_{name}.json")).read_text())


def record_from_sample(enricher, name: str, *, origin=ORIGIN_KB, siis=None) -> CacheRecord:
    s = sample(name)
    out = s["output"]
    query = out["query"]
    intent = enricher.analyze(query)
    siis_text = siis if siis is not None else s["input"]["siis_response"]
    fp = fingerprint_text(siis_text)
    keys = [CacheKey(query, intent.normalized_query, "query")]
    keys += [CacheKey(v, enricher.normalize_query(v), "variation") for v in out["query_variations"]]
    return CacheRecord(
        plan_id=make_plan_id(origin, fp, intent.normalized_query),
        origin=origin,
        source_id=name,
        source_fp=fp,
        query=query,
        normalized_query=intent.normalized_query,
        intent=intent_summary(intent),
        payload={"query_variations": out["query_variations"], "response": out["response"]},
        model="test",
        versions=dict(VERSIONS),
        keys=keys,
    )


def make_cache(registry, store=None, threshold=0.55, versions=VERSIONS):
    return PlanCache(
        store or MemoryCacheStore(),
        HashingEmbeddingProvider(),
        versions,
        threshold=threshold,
        validator=validator_for(registry),
    )


def test_exact_and_semantic_hits(registry, enricher):
    cache = make_cache(registry)
    cache.warm_load()
    assert cache.put(record_from_sample(enricher, "B01"))
    q = enricher.analyze("My phone battery dies very fast")
    hit = cache.lookup_exact(q, None)
    assert hit is not None and hit.kind == "exact"
    para = enricher.analyze("my phone battery dies way too fast")
    assert cache.lookup_exact(para, None) is None
    res = cache.lookup_semantic(para, cache.embedder.embed([para.normalized_query])[0], None)
    assert res.hit is not None and res.hit.kind == "semantic" and res.hit.record.source_id == "B01"


def test_request_scoped_plan_never_served_to_other_users(registry, enricher):
    cache = make_cache(registry)
    cache.warm_load()
    custom_siis = "Battery drains quickly\n\nOpen Settings, tap Battery, and then turn on Power saving."
    rec = record_from_sample(enricher, "B01", origin=ORIGIN_REQUEST, siis=custom_siis)
    assert cache.put(rec)
    q = enricher.analyze("My phone battery dies very fast")
    assert cache.lookup_exact(q, None) is None  # no siis_response → only KB plans
    assert cache.lookup_exact(q, fingerprint_text("different text")) is None
    assert cache.lookup_exact(q, fingerprint_text(custom_siis)) is not None


def test_cache_poisoning_distinct_complaints_do_not_collide(registry, enricher):
    cache = make_cache(registry, threshold=0.0)  # even with no similarity floor
    cache.warm_load()
    rec = record_from_sample(enricher, "B01")
    rec.intent = intent_summary(enricher.analyze("battery drains after update"))
    cache.put(rec)
    hot = enricher.analyze("phone becomes hot during charging")
    res = cache.lookup_semantic(hot, cache.embedder.embed([hot.normalized_query])[0], None)
    assert res.hit is None
    assert any(r["reason"] in ("concept_conflict", "domain_conflict", "qualifier_conflict") for r in res.rejected)


def test_compatibility_rules(enricher):
    drain = intent_summary(enricher.analyze("battery drains fast"))
    drain_upd = intent_summary(enricher.analyze("battery drains fast after the update"))
    hot = intent_summary(enricher.analyze("phone gets hot while charging"))
    dim = intent_summary(enricher.analyze("screen is too dim"))
    flicker = intent_summary(enricher.analyze("screen flickers"))
    assert compatibility(drain, drain_upd)[0]
    assert not compatibility(drain, hot)[0]
    assert not compatibility(dim, flicker)[0]
    after_app = intent_summary(enricher.analyze("battery drains fast after installing an app"))
    assert compatibility(after_app, drain_upd) == (False, "qualifier_conflict")


def test_invalid_plan_is_never_cached(registry, enricher):
    cache = make_cache(registry)
    cache.warm_load()
    rec = record_from_sample(enricher, "B01")
    bad = copy.deepcopy(rec.payload)
    bad["response"]["contexts"][0]["actions"][0]["stepGroups"][0]["actionableDeeplink"]["deeplink"] = "bixby://fake"
    rec.payload = bad
    assert cache.put(rec) is False
    assert len(cache) == 0


def test_hit_that_fails_revalidation_is_evicted(registry, enricher):
    cache = make_cache(registry)
    cache.warm_load()
    rec = record_from_sample(enricher, "C02")
    assert cache.put(rec)
    # Corrupt the stored payload behind the cache's back (e.g. catalog changed in place).
    cache._records[rec.plan_id].payload["response"]["contexts"][0]["title"] = "Not A Valid Title At All"
    q = enricher.analyze(rec.query)
    assert cache.lookup_exact(q, None) is None
    assert len(cache) == 0 and cache.stats["rejected_hits"] == 1


def test_sqlite_persistence_and_version_invalidation(tmp_path, registry, enricher):
    path = tmp_path / "c.sqlite"
    c1 = make_cache(registry, SqliteCacheStore(path))
    c1.warm_load()
    assert c1.put(record_from_sample(enricher, "P01"))
    c2 = make_cache(registry, SqliteCacheStore(path))
    assert c2.warm_load()["loaded"] == 1
    assert c2.lookup_exact(enricher.analyze("My phone got slow after the update"), None) is not None
    c3 = make_cache(registry, SqliteCacheStore(path), versions={"catalog": "cat-v2", "pipeline": "p-v1"})
    stats = c3.warm_load()
    assert stats == {"loaded": 0, "dropped": 1, "reembedded": 0}


def test_export_import_round_trip(tmp_path, registry, enricher):
    c1 = make_cache(registry)
    c1.warm_load()
    for n in ("B01", "C02", "P01", "D02", "D01"):
        assert c1.put(record_from_sample(enricher, n))
    out = tmp_path / "prewarm.jsonl"
    assert c1.export_jsonl(out) == 5
    c2 = make_cache(registry)
    c2.warm_load()
    assert c2.import_jsonl(out) == {"imported": 5, "skipped": 0}
    assert c2.key_count == c1.key_count


def test_multi_intent_query_does_not_hit_single_intent_plan(registry, enricher):
    cache = make_cache(registry, threshold=0.0)
    cache.warm_load()
    cache.put(record_from_sample(enricher, "D02"))
    q = enricher.analyze("screen flickers and the battery dies fast")
    assert len(q.sub_intents) == 2
    sub = q.sub_intents[0]
    res = cache.lookup_semantic(sub, cache.embedder.embed([sub.normalized_query])[0], None)
    assert res.hit is not None and res.hit.record.source_id == "D02"
