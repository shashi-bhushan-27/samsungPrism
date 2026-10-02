"""HTTP API tests (FastAPI TestClient, fake LLM, hashing embeddings, in-memory cache)."""

from __future__ import annotations

import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.cache.store import MemoryCacheStore
from app.core.config import Settings
from app.core.constants import DUMMY_POSITIVE_URI
from app.core.container import build_components
from app.main import create_app
from app.retrieval.embeddings import HashingEmbeddingProvider
from app.validation.business_rules import validate_envelope
from app.validation.url_safety import scan_payload
from tests.integration.test_pipeline import DEV, GOLD, SIIS, GoldLLM

META = json.loads((DEV / "eval" / "catalog_meta.json").read_text())["entries"]


def settings(tmp_path, **kw):
    params = dict(data_dir=DEV, artifacts_dir=tmp_path, llm_provider="fake", embedding_provider="hashing",
                  cache_backend="memory", semantic_cache_threshold=0.6, siis_retrieval_min_score=0.55,
                  log_level="WARNING")
    params.update(kw)
    return Settings(**params)


def client_for(tmp_path, llm=None, **kw):
    s = settings(tmp_path, **kw)
    comps = build_components(s, llm=llm or GoldLLM(), embedder=HashingEmbeddingProvider(),
                             cache_store=MemoryCacheStore(), import_prewarm=False)
    return TestClient(create_app(s, comps)), comps


@pytest.fixture()
def api(tmp_path):
    client, comps = client_for(tmp_path)
    with client:
        yield client, comps


def ok_contract(comps, body):
    rep = validate_envelope(body, comps.registry)
    assert rep.ok, rep.summary()
    assert scan_payload(body) == []


def post(client, query, siis=None, **extra):
    payload = {"query": query, **extra}
    if siis is not None:
        payload["siis_response"] = siis
    return client.post("/v1/troubleshoot", json=payload)


# 1 / 2 / 3 ----------------------------------------------------------------- basics
def test_valid_complaint_without_siis(api):
    client, comps = api
    r = post(client, GOLD["B01"]["query"])
    assert r.status_code == 200 and r.headers["X-Cache"] == "MISS"
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    ok_contract(comps, body)
    assert set(body) == {"query", "query_variations", "response", "meta"}
    assert set(body["meta"]) == {"latency_ms", "cache_hit", "model", "cost_usd"}


def test_valid_complaint_with_siis(api):
    client, comps = api
    r = post(client, GOLD["C02"]["query"], SIIS["C02"])
    assert r.status_code == 200
    ok_contract(comps, r.json())
    assert not r.text.lstrip().startswith("```")


def test_missing_siis_unknown_issue_returns_no_siis_context(api):
    client, comps = api
    r = post(client, "How do I change my ringtone?")
    body = r.json()
    assert r.status_code == 200 and body["response"]["contexts"] == []
    assert body["meta"]["fallback"] == "no_siis_context"
    ok_contract(comps, body)


# 4 ------------------------------------------------------------------- no match
def test_no_match(api):
    client, comps = api
    r = post(client, "Can I use my fingerprint sensor underwater?",
             "Fingerprint underwater\n\nThis is a hardware limitation and cannot be changed in Settings.")
    body = r.json()
    assert body["response"]["contexts"] == [] and body["meta"]["fallback"] == "no_match"
    ok_contract(comps, body)


# 5 / 6 ------------------------------------------------------------ bad requests
def test_malformed_json(api):
    client, _ = api
    r = client.post("/v1/troubleshoot", content=b'{"query": "battery', headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "malformed_json"
    assert "Traceback" not in r.text


@pytest.mark.parametrize("payload,code", [
    ({"query": ""}, "empty_query"),
    ({"query": "   "}, "empty_query"),
    ({}, "invalid_request"),
    ({"query": 42}, "invalid_request"),
    ({"query": "battery", "siis_response": ["x"]}, "invalid_request"),
    ({"query": "b" * 5000}, "query_too_long"),
])
def test_invalid_requests(api, payload, code):
    client, _ = api
    r = client.post("/v1/troubleshoot", json=payload)
    assert r.status_code == 422 and r.json()["error"]["code"] == code
    assert r.json()["error"]["request_id"] == r.headers["X-Request-ID"]


def test_extra_request_fields_are_ignored(api):
    client, comps = api
    r = post(client, GOLD["C07"]["query"], SIIS["C07"], debug=True, user_id="x")
    assert r.status_code == 200


def test_unknown_route_is_structured(api):
    client, _ = api
    r = client.get("/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


# 7 / 8 ---------------------------------------------------------------- cache hits
def test_exact_and_semantic_cache_hits_skip_the_llm(tmp_path):
    llm = GoldLLM()
    client, comps = client_for(tmp_path, llm)
    with client:
        post(client, GOLD["B01"]["query"])
        n = len(llm.calls)
        r = post(client, GOLD["B01"]["query"])
        assert r.headers["X-Cache"] == "HIT-EXACT" and r.json()["meta"]["cache_hit"] is True
        r = post(client, "my phone battery dies way too fast")
        assert r.headers["X-Cache"] == "HIT-SEMANTIC" and r.headers["X-Model-Calls"] == "0"
        assert len(llm.calls) == n
        assert r.json()["meta"]["cost_usd"] == 0.0
        ok_contract(comps, r.json())


# 9 / 11 ------------------------------------------------------- model failure / bad output
def test_model_failure_degrades_gracefully(tmp_path):
    client, comps = client_for(tmp_path, GoldLLM(fail=True))
    with client:
        r = post(client, GOLD["P01"]["query"], SIIS["P01"])
        assert r.status_code == 200 and r.json()["meta"]["model"] == "rules-extractor"
        ok_contract(comps, r.json())
        r = post(client, "battery drains", "Battery information\n\nBattery life varies with usage.")
        assert r.status_code == 503 and r.json()["error"]["code"] == "extraction_unavailable"


def test_invalid_model_output_is_repaired_or_rejected(tmp_path):
    client, comps = client_for(tmp_path, GoldLLM(lambda plan, prompt: "not json at all" if "SOURCE" in prompt else plan))
    with client:
        r = post(client, GOLD["C08"]["query"], SIIS["C08"])
        assert r.status_code == 200
        ok_contract(comps, r.json())


# 10 ------------------------------------------------------------- retrieval failure
def test_retrieval_failure_is_a_structured_503(tmp_path):
    client, comps = client_for(tmp_path)

    def boom(texts):
        raise RuntimeError("embedding backend down")

    with client:
        comps.embedder.embed = boom  # type: ignore[assignment]
        comps.embedder.embed_query = boom  # type: ignore[assignment]
        r = post(client, GOLD["B01"]["query"])
        assert r.status_code == 503 and r.json()["error"]["code"] == "retrieval_unavailable"
        assert "RuntimeError" not in r.text and "Traceback" not in r.text


# 12 / 13 ------------------------------------------------ URL leak / fabricated deeplink
def test_url_leak_and_fabricated_uri_never_reach_the_client(tmp_path):
    def mutate(plan, prompt):
        p = json.loads(json.dumps(plan))
        a = p["goals"][0]["actions"][0]
        a["steps"].append("Visit https://www.samsung.com/support/ for details.")
        a["steps"].append("Open bixby://masked/act/ffffffffffff now.")
        a["description"] = "It will help [docs](https://evil.example)"
        return p

    client, comps = client_for(tmp_path, GoldLLM(mutate))
    with client:
        r = post(client, GOLD["B01"]["query"] + " see https://evil.example.com", SIIS["B01"])
        body = r.json()
        ok_contract(comps, body)
        assert "http" not in r.text and "ffffffffffff" not in r.text
        uris = [g["actionableDeeplink"]["deeplink"] for c in body["response"]["contexts"] for a in c["actions"]
                for g in a["stepGroups"] if g["actionableDeeplink"]]
        assert all(u == DUMMY_POSITIVE_URI or comps.registry.get_entry(u) for u in uris)


# 14 / 15 --------------------------------------------------------------------- health
def test_health_after_initialisation(api):
    client, _ = api
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_health_when_llm_unreachable(tmp_path):
    from app.llm.fake import FakeLLMProvider

    client, _ = client_for(tmp_path, FakeLLMProvider(healthy=False))
    with client:
        r = client.get("/health")
        assert r.status_code == 503 and r.json()["components"]["checks"]["llm"] is False


def test_health_before_initialisation(tmp_path):
    s = settings(tmp_path, data_dir=tmp_path / "missing")  # startup fails: no catalog
    app = create_app(s)
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 503 and r.json()["status"] == "unavailable"
        r = post(client, "battery")
        assert r.status_code == 503 and r.json()["error"]["code"] == "service_unavailable"


# 16 / 17 / 18 / 19 ---------------------------------------------- plan semantics via API
def test_manual_critical_exact_target_and_parent_menu(api):
    client, comps = api
    b04 = post(client, GOLD["B04"]["query"], SIIS["B04"]).json()
    acts = b04["response"]["contexts"][0]["actions"]
    for a in acts:
        if a["category"] == "manual":
            assert a["stepGroups"][0]["actionableDeeplink"] is None  # 16
    cats = [a["category"] for a in acts]
    assert cats.index("critical") == len(cats) - 1  # 17
    b01 = post(client, GOLD["B01"]["query"], SIIS["B01"]).json()
    sleep = next(a for a in b01["response"]["contexts"][0]["actions"] if "Sleep" in a["actionName"])
    assert sleep["stepGroups"][0]["actionableDeeplink"]["deeplink"] in (META["put_unused_to_sleep"]["uri"],
                                                                        META["background_limits"]["uri"])  # 18
    d01 = post(client, GOLD["D01"]["query"], SIIS["D01"]).json()
    dl = d01["response"]["contexts"][0]["actions"][0]["stepGroups"][0]["actionableDeeplink"]["deeplink"]
    assert dl == DUMMY_POSITIVE_URI and dl not in (META["display"]["uri"], META["settings"]["uri"])  # 19


def test_request_id_is_propagated(api):
    client, _ = api
    r = client.post("/v1/troubleshoot", json={"query": GOLD["C05"]["query"]}, headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Request-ID"] == "abc-123"


def test_demo_page_is_served_and_only_calls_the_api(api):
    client, _ = api
    r = client.get("/demo")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert 'fetch("/v1/troubleshoot"' in r.text and "://" not in r.text  # same-origin API only, no external links


def test_health_without_a_configured_model_reports_no_model_mode(tmp_path):
    from app.llm.base import NullLLMProvider

    client, _ = client_for(tmp_path, NullLLMProvider())
    with client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["mode"] == "no_model" and r.json()["llm"] == "not_configured"
        d = client.get("/health/details").json()
        assert d["mode"] == "no_model" and "llm" not in d["required"]
