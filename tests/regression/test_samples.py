"""Sample regression (PDF §8 Phase 1: "validate baseline parsing against reference samples").

For every reference sample in samples/:
1. the expected output itself passes every gate (schema, business rules, URL scan, catalog integrity);
2. offline replay: a scripted model returns exactly the extraction the sample implies (topic, title, actions,
   categories, steps). Everything after the model (grounding, grouping, deeplink mapping, validation deeplinks,
   sequencing, repair, final gate) must reproduce the sample's goal, title, actions, steps and deeplinks.
The live model is exercised on the same samples by scripts/run_benchmarks.py (metrics.md §2).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.cache.store import MemoryCacheStore
from app.catalog.loaders import load_samples
from app.core.config import Settings
from app.core.constants import GOAL_PREFIX
from app.core.container import build_components
from app.llm.fake import FakeLLMProvider
from app.llm.prompts import VARIATION_REGISTERS
from app.retrieval.embeddings import HashingEmbeddingProvider
from app.validation.business_rules import validate_envelope
from app.validation.url_safety import scan_payload

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "data" / "dev_fixtures"
SAMPLES, ISSUES = load_samples(DEV / "samples")


def plan_from(contexts: list[dict]) -> dict:
    goals = []
    for g in contexts:
        topic, kind = g["goal"][len(GOAL_PREFIX):].rsplit(" ", 1)
        goals.append({
            "topic": topic, "goal_type": kind, "title": g["title"],
            "actions": [{"action_name": a["actionName"], "description": a["description"], "category": a["category"],
                         "steps": [s for sg in a["stepGroups"] for s in sg["steps"]]} for a in g["actions"]],
        })
    return {"has_solution": bool(goals), "goals": goals}


def skeleton(contexts: list[dict]) -> list:
    """Everything except the confidence score (derived deterministically, but not part of the sample contract)."""
    return [{k: v for k, v in g.items() if k != "score"} for g in contexts]


@pytest.fixture(scope="module")
def components(tmp_path_factory):
    s = Settings(data_dir=DEV, artifacts_dir=tmp_path_factory.mktemp("art"), llm_provider="fake",
                 embedding_provider="hashing", cache_backend="memory")
    return build_components(s, llm=FakeLLMProvider(), embedder=HashingEmbeddingProvider(),
                            cache_store=MemoryCacheStore(), import_prewarm=False)


def test_samples_are_loaded():
    assert len(SAMPLES) == 5 and not ISSUES


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: s.id)
def test_expected_sample_output_passes_every_gate(components, sample):
    rep = validate_envelope(dict(sample.expected), components.registry)
    assert rep.ok, rep.summary()
    assert scan_payload(dict(sample.expected)) == []


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: s.id)
def test_pipeline_reproduces_sample_after_extraction(components, sample):
    expected = sample.expected_contexts

    def respond(system, prompt, schema):
        if "paraphrases" in system:
            return {r: f"{sample.query} ({r.replace('_', ' ')})" for r in VARIATION_REGISTERS}
        return plan_from(expected)

    components.service.llm = components.extractor.llm = FakeLLMProvider(respond)
    r = asyncio.run(components.service.troubleshoot(sample.query, sample.siis_response,
                                                    read_cache=False, write_cache=False))
    assert r.status_code == 200
    assert validate_envelope(r.body, components.registry).ok
    assert skeleton(r.body["response"]["contexts"]) == skeleton(expected)
