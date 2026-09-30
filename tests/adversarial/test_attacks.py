"""Hostile hardening suite (Phase 9): one section per attack of HARDENING_REPORT.md.

Offline and deterministic: the model is a scripted fake that returns hostile output. The live counterparts
(real model, real HTTP) are scripts/hostile_probe.py and scripts/run_benchmarks.py.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from app.cache.semantic import PlanCache
from app.cache.store import MemoryCacheStore
from app.catalog.loaders import load_catalog
from app.catalog.registry import CatalogRegistry
from app.core.constants import DUMMY_POSITIVE_URI
from app.evaluation.scoring import score_plan
from app.retrieval.embeddings import EmbeddingProvider
from app.services.query_enrichment import QueryEnricher
from app.services.troubleshooting import ServiceError
from app.validation.business_rules import validate_envelope
from app.validation.url_safety import scan_payload
from tests.conftest import catalog_deeplink, make_action, make_envelope
from tests.integration.test_pipeline import GOLD, SIIS, GoldLLM, assert_contract, make, run
from tests.unit.test_cache import record_from_sample, validator_for

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def dev_registry() -> CatalogRegistry:
    entries, issues, fp = load_catalog(ROOT / "data" / "dev_fixtures" / "deeplinks.json")
    return CatalogRegistry(entries, fingerprint=fp, issues=issues)


@pytest.fixture(scope="module")
def enricher() -> QueryEnricher:
    return QueryEnricher()


def uris_of(body: dict) -> list[str]:
    out = []
    for g in body["response"]["contexts"]:
        for a in g["actions"]:
            for sg in a["stepGroups"]:
                for key in ("actionableDeeplink", "validationDeeplink"):
                    d = sg.get(key)
                    if isinstance(d, dict) and d.get("deeplink"):
                        out.append(d["deeplink"])
    return out


# ============================================================ ATTACK 1 — URL leaks
URL_FORMS = [
    "https://www.samsung.com/us/support/",
    "http://example.com/fix",
    "www.samsung.com/support",
    "[the official guide](https://samsung.com/guide)",
    "samsung.com/support",
    "hxxps://evil[.]example[.]com",
    "visit samsung dot com slash support",
    "ftp://files.example.org/patch",
    "http://192.168.0.1/admin",
    "<https://t.co/abc>",
]


@pytest.mark.parametrize("url", URL_FORMS)
def test_attack1_url_in_every_generated_field_is_scrubbed(tmp_path, url):
    def mutate(plan, prompt):
        p = copy.deepcopy(plan)
        g = p["goals"][0]
        g["title"] = f"Battery drain {url}"
        g["topic"] = f"Battery Drain {url}"
        for a in g["actions"]:
            a["action_name"] = f"{a['action_name']} {url}"
            a["description"] = f"It will help, see {url}"
            a["steps"] = [f"{s[:-1]} or open {url}." for s in a["steps"]] + [f"For details go to {url}."]
        return p

    c = make(tmp_path, GoldLLM(mutate))
    r = run(c, GOLD["B01"]["query"] + " " + url, SIIS["B01"] + f"\nMore help: {url}\n")
    assert scan_payload(r.body) == []
    assert_contract(c, r.body)


def test_attack1_url_only_source_yields_no_plan(tmp_path):
    c = make(tmp_path, GoldLLM(lambda plan, prompt: {"has_solution": True, "goals": [{
        "topic": "Battery", "goal_type": "Troubleshooting", "title": "Battery help",
        "actions": [{"action_name": "Visit Support Site", "description": "It will open the support site",
                     "category": "manual", "steps": ["Go to https://www.samsung.com/support."]}]}]}))
    r = run(c, "battery drains", "Battery help\n\nFor help, visit https://www.samsung.com/support.")
    assert r.body["response"]["contexts"] == [] and r.body["meta"]["fallback"] == "no_match"
    assert scan_payload(r.body) == []


# ============================================================ ATTACK 2 — fabricated deeplinks
def test_attack2_model_supplied_uris_never_reach_the_response(tmp_path):
    fake = "bixby://masked/act/ffffffffffff"

    def mutate(plan, prompt):
        p = copy.deepcopy(plan)
        for a in p["goals"][0]["actions"]:
            a["deeplink"] = fake  # extra field the schema does not ask for
            a["actionableDeeplink"] = {"deeplink": fake, "description": "x"}
            a["steps"].append(f"Open {fake}.")
        return p

    c = make(tmp_path, GoldLLM(mutate))
    for qid in ("B01", "D02", "C02", "P01"):
        r = run(c, GOLD[qid]["query"], SIIS[qid], read_cache=False, write_cache=False)
        assert fake not in json.dumps(r.body)
        for u in uris_of(r.body):
            assert u == DUMMY_POSITIVE_URI or c.registry.get_entry(u) or c.registry.get_validation_rule(u)


def test_attack2_validator_rejects_a_uri_outside_the_catalog(test_registry):
    real = catalog_deeplink(test_registry, "bixby://masked/act/aaa004")
    env = make_envelope([make_action(deeplink={**real, "deeplink": "bixby://masked/act/aaa0040"})])  # one char added
    rep = validate_envelope(env, test_registry)
    assert not rep.ok and "deeplink.catalog_membership" in {v.code for v in rep.errors}
    env = make_envelope([make_action(deeplink={**real, "description": "Open Power saving settings now"})])  # edited
    rep = validate_envelope(env, test_registry)
    assert not rep.ok and "deeplink.field_integrity" in {v.code for v in rep.errors}


# ============================================================ ATTACK 3 — parent menus
def test_attack3_no_gold_action_is_mapped_to_its_parent_menu(tmp_path):
    """Every gold action goes through the real mapper: parent menus (Settings, Display, Battery...) exist in the
    catalog next to the exact child; the child (or dummy_positive when unindexed) must win every time."""
    c = make(tmp_path, GoldLLM())
    outcomes = []
    for qid in GOLD:
        r = run(c, GOLD[qid]["query"], SIIS[qid], read_cache=False, write_cache=False)
        outcomes += score_plan(qid, r.body["response"], GOLD[qid]).deeplink_outcomes
    assert "parent" not in outcomes and "wrong" not in outcomes
    assert outcomes.count("exact") == len(outcomes)


# ============================================================ ATTACK 4 — fragmentation
def test_attack4_one_action_per_tap_is_regrouped_into_one_screen_action(tmp_path):
    taps = [("Open Settings", "Open Settings."), ("Open Display", "Tap Display."),
            ("Open Navigation Bar", "Tap Navigation bar."),
            ("Choose Swipe Gestures", "Select your preferred navigation type between Buttons and Swipe gestures.")]

    def mutate(plan, prompt):
        return {"has_solution": True, "goals": [{"topic": "Swipe Navigation", "goal_type": "Troubleshooting",
                "title": "Swipe navigation settings", "actions": [
                    {"action_name": n, "description": "It will open the next screen", "category": "auto", "steps": [s]}
                    for n, s in taps]}]}

    c = make(tmp_path, GoldLLM(mutate))
    r = run(c, GOLD["D01"]["query"], SIIS["D01"])
    (action,) = r.body["response"]["contexts"][0]["actions"]
    assert action["stepGroups"][0]["steps"] == [s for _, s in taps]
    assert action["stepGroups"][0]["actionableDeeplink"]["deeplink"] == DUMMY_POSITIVE_URI  # never Display/Settings
    assert_contract(c, r.body)


# ============================================================ ATTACK 5 — over-bundling
def test_attack5_unrelated_screens_are_never_one_action(tmp_path):
    def mutate(plan, prompt):
        return {"has_solution": True, "goals": [{"topic": "Battery Drain", "goal_type": "Troubleshooting",
                "title": "Battery fast drain", "actions": [{
                    "action_name": "Fix Everything", "description": "It will fix the whole phone", "category": "auto",
                    "steps": ["Open Settings.", "Tap Battery.", "Turn on Power saving.", "Open Settings.",
                              "Tap Lock screen and AOD.", "Turn off Always On Display.", "Open Settings.",
                              "Tap Battery.", "Tap Battery usage."]}]}]}

    c = make(tmp_path, GoldLLM(mutate))
    r = run(c, GOLD["B01"]["query"], SIIS["B01"])
    acts = r.body["response"]["contexts"][0]["actions"]
    assert len(acts) == 3
    links = [a["stepGroups"][0]["actionableDeeplink"]["deeplink"] for a in acts]
    assert len(set(links)) == 3 and all(c.registry.get_entry(u) for u in links)
    assert_contract(c, r.body)


# ============================================================ ATTACK 6 — critical order
def test_attack6_critical_actions_last_and_least_disruptive_first(tmp_path):
    def mutate(plan, prompt):
        return {"has_solution": True, "goals": [{"topic": "Slow Performance", "goal_type": "Troubleshooting",
                "title": "Slow phone performance", "actions": [
                    {"action_name": "Factory Data Reset", "description": "It will erase all data and settings",
                     "category": "auto", "steps": ["Open Settings.", "Tap General management.", "Tap Reset.",
                                                   "Tap Factory data reset."]},
                    {"action_name": "Restart Your Phone", "description": "It will close all running apps",
                     "category": "auto", "steps": ["Press and hold the Side key.", "Tap Restart."]},
                    {"action_name": "Visit A Service Center", "description": "It will get your phone checked",
                     "category": "auto", "steps": ["Take your phone to a service center."]},
                    {"action_name": "Optimize With Device Care", "description": "It will close unused background apps",
                     "category": "critical", "steps": ["Open Settings.", "Tap Device care.", "Tap Optimize now."]},
                ]}]}

    c = make(tmp_path, GoldLLM(mutate))
    r = run(c, GOLD["P01"]["query"], SIIS["P01"])
    acts = r.body["response"]["contexts"][0]["actions"]
    names = [a["actionName"] for a in acts]
    cats = [a["category"] for a in acts]
    critical = [i for i, cat in enumerate(cats) if cat == "critical"]
    assert critical == list(range(len(cats) - len(critical), len(cats)))  # critical block is the suffix
    assert names.index("Restart Your Phone") < names.index("Factory Data Reset")  # least disruptive first
    assert cats[names.index("Optimize With Device Care")] == "auto"  # a non-destructive step is not critical
    assert_contract(c, r.body)


# ============================================================ ATTACK 7 — manual action deeplink
def test_attack7_manual_action_with_deeplink_is_rejected_by_the_gate(test_registry):
    env = make_envelope([make_action(category="manual", deeplink=catalog_deeplink(test_registry, "bixby://masked/act/aaa004"))])
    rep = validate_envelope(env, test_registry)
    assert not rep.ok and "deeplink.manual_actionable" in {v.code for v in rep.errors}


def test_attack7_pipeline_never_links_a_manual_action(tmp_path):
    def mutate(plan, prompt):
        p = copy.deepcopy(plan)
        for a in p["goals"][0]["actions"]:
            a["category"] = "manual"  # the model mislabels every Settings action as manual
        return p

    c = make(tmp_path, GoldLLM(mutate))
    r = run(c, GOLD["B04"]["query"], SIIS["B04"])
    for a in r.body["response"]["contexts"][0]["actions"]:
        if a["category"] == "manual":
            assert all(sg["actionableDeeplink"] is None for sg in a["stepGroups"])
    assert_contract(c, r.body)


# ============================================================ ATTACK 8 — cache poisoning
class ConstantEmbedder(EmbeddingProvider):
    """Worst case for a semantic cache: every text gets the same vector (cosine similarity 1.0)."""

    provider = "constant"

    def __init__(self):
        super().__init__("constant")

    @property
    def dim(self) -> int:
        return 8

    def embed(self, texts):
        v = np.ones((len(texts), self.dim), dtype=np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)


@pytest.mark.parametrize("cached,incoming", [
    ("battery drains after update", "phone becomes hot during charging"),
    ("phone becomes hot during charging", "battery drains after update"),
    ("screen is too dim", "screen keeps flickering"),
    ("camera app keeps crashing", "photos are blurry"),
    ("battery drains fast after the update", "battery drains fast after installing an app"),
])
def test_attack8_similar_embeddings_never_serve_another_intent(dev_registry, enricher, cached, incoming):
    from app.cache.semantic import intent_summary

    cache = PlanCache(MemoryCacheStore(), ConstantEmbedder(), {"catalog": "c", "pipeline": "p"}, threshold=0.0,
                      validator=validator_for(dev_registry))
    cache.warm_load()
    rec = record_from_sample(enricher, "B01")
    rec.intent = intent_summary(enricher.analyze(cached))
    rec.normalized_query = enricher.analyze(cached).normalized_query
    assert cache.put(rec)
    q = enricher.analyze(incoming)
    res = cache.lookup_semantic(q, cache.embedder.embed([q.normalized_query])[0], None)
    assert res.hit is None, res.rejected


# ============================================================ ATTACK 9 — cache fragmentation
@pytest.fixture(scope="module")
def heldout_lookup():
    """Production wiring (neural embedder, calibrated thresholds, committed pre-warmed plans) applied to the
    newest held-out round of LLM paraphrases. Returns (correct, wrong, n)."""
    from app.core.config import Settings
    from app.core.container import build_components

    try:
        comps = build_components(Settings(data_dir=ROOT / "data" / "dev_fixtures", llm_provider="none",
                                          embedding_provider="fastembed", cache_backend="memory"))
    except Exception as exc:  # embedding model not downloadable in this environment
        pytest.skip(f"embedding model unavailable: {type(exc).__name__}")
    assert len(comps.cache) == 32
    rounds = {json.loads(f.read_text()).get("round", 1): f
              for f in (ROOT / "data/dev_fixtures/eval").glob("paraphrases_llm_heldout*.json")}
    held = json.loads(rounds[max(rounds)].read_text())["items"]  # newest round: generated after the last fix
    plan_of = {r.source_id: r for r in comps.cache.records()}
    correct = wrong = 0
    for it in held:
        intent = comps.enricher.analyze(it["text"])
        hit = comps.cache.lookup_exact(intent, None)
        if hit is None:
            hit = comps.cache.lookup_semantic(intent, comps.embedder.embed([intent.normalized_query])[0], None).hit
        if hit is None:
            continue
        if hit.record is plan_of.get(comps.links[it["query_id"]]):
            correct += 1
        else:
            wrong += 1
    return correct, wrong, len(held)


def test_attack9_unseen_paraphrases_converge_on_the_right_plan(heldout_lookup):
    correct, _, n = heldout_lookup
    assert correct / n >= 0.8  # PDF target: >= 80% semantic hit rate on unseen paraphrases


@pytest.mark.xfail(strict=True, reason="Known limitation (FINAL_REVIEW.md, metrics.md §4): on held-out round 2 a few "
                   "hits serve a sibling plan (touch-vs-freeze subsumption when buttons are also unresponsive; "
                   "'phone is full' not recognised as storage; a symptom-less generic battery complaint). "
                   "strict: remove this marker once fixed and re-measured on a fresh round.")
def test_attack9_no_wrong_plan_is_ever_served(heldout_lookup):
    _, wrong, _ = heldout_lookup
    assert wrong == 0


# ============================================================ ATTACK 10 — malformed model output
MALFORMED = {
    "truncated_json": lambda plan: json.dumps(plan)[:120],
    "prose_only": lambda plan: "I am sorry, I can't help with that.",
    "extra_prose": lambda plan: "Here you go!\n" + json.dumps(plan) + "\nHope this helps :)",
    "missing_actions": lambda plan: {"has_solution": True, "goals": [{"topic": "Battery Drain", "title": "Battery drain"}]},
    "wrong_types": lambda plan: {"has_solution": "yes", "goals": {"actions": 5}},
    "steps_as_object": lambda plan: {**plan, "goals": [{**plan["goals"][0], "actions": [
        {**a, "steps": {"1": a["steps"][0]}} for a in plan["goals"][0]["actions"]]}]},
    "invalid_categories": lambda plan: {**plan, "goals": [{**plan["goals"][0], "actions": [
        {**a, "category": ["auto", "critical"]} for a in plan["goals"][0]["actions"]]}]},
    "description_lengths": lambda plan: {**plan, "goals": [{**plan["goals"][0], "actions": [
        {**a, "description": d} for a, d in zip(plan["goals"][0]["actions"],
                                                  ["Helps", "It will " + "really " * 20 + "help"] * 10)]}]},
    "actions_as_strings": lambda plan: {**plan, "goals": [{**plan["goals"][0], "actions": ["Open Settings", "Tap Battery"]}]},
    "empty_steps": lambda plan: {**plan, "goals": [{**plan["goals"][0], "actions": [
        {**a, "steps": []} for a in plan["goals"][0]["actions"]]}]},
    "null_everything": lambda plan: {"has_solution": None, "goals": None},
}


@pytest.mark.parametrize("kind", sorted(MALFORMED))
def test_attack10_malformed_output_is_repaired_or_rejected_within_bounds(tmp_path, kind):
    llm = GoldLLM(lambda plan, prompt: MALFORMED[kind](plan) if "SOURCE" in prompt else plan)
    c = make(tmp_path, llm, llm_repair_retries=1)
    try:
        r = run(c, GOLD["B01"]["query"], SIIS["B01"])
    except ServiceError as exc:  # a safe, typed error is acceptable; a crash or invalid JSON is not
        assert exc.code in ("extraction_unavailable", "plan_validation_failed")
        return
    assert len([x for x in llm.calls if "SOURCE" in x["prompt"]]) <= 2  # original + at most one repair
    assert_contract(c, r.body)
    assert len(c.cache) == 0 or r.telemetry["extraction_modes"] == ["llm"]  # rules-fallback plans are never cached


# ============================================================ ATTACK 11 — determinism
def test_attack11_identical_input_gives_identical_plans_for_every_query(tmp_path):
    c = make(tmp_path, GoldLLM())
    for qid in GOLD:
        a, b = (run(c, GOLD[qid]["query"], SIIS[qid], read_cache=False, write_cache=False).body for _ in range(2))
        assert a["response"] == b["response"], qid


# ============================================================ ATTACK 13 — no source
@pytest.mark.parametrize("query", ["My car stereo won't pair", "How do I cancel my phone contract?",
                                   "the fridge is making noise", "asdfgh qwerty"])
def test_attack13_no_source_means_no_plan_and_no_model_call(tmp_path, query):
    llm = GoldLLM()
    c = make(tmp_path, llm)
    r = run(c, query)
    assert r.body["response"]["contexts"] == [] and r.body["meta"]["fallback"] == "no_siis_context"
    assert not [x for x in llm.calls if "SOURCE" in x["prompt"]]  # nothing to ground on → no extraction
    assert_contract(c, r.body)


# ============================================================ ATTACK 14 — no solution
@pytest.mark.parametrize("siis", [
    "Water resistance\n\nThe phone is not designed for use under water. This cannot be changed in Settings.",
    "Screen damage\n\nA cracked screen must be assessed. Contact us.",
    "",  # empty text is treated as absent → retrieval path
])
def test_attack14_no_viable_solution_returns_no_match_structure(tmp_path, siis):
    c = make(tmp_path, GoldLLM(lambda plan, prompt: {"has_solution": False, "goals": []}))
    r = run(c, "Can I use my phone underwater and is my cracked screen covered?", siis)
    assert r.body["response"]["contexts"] == []
    assert r.body["meta"]["fallback"] in ("no_match", "no_siis_context")
    assert_contract(c, r.body)
    assert len(c.cache) == 0
