"""End-to-end pipeline tests with a deterministic fake LLM (no network)."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from pathlib import Path

import pytest

from app.cache.store import MemoryCacheStore
from app.core.config import Settings
from app.core.constants import DUMMY_POSITIVE_URI
from app.core.container import build_components
from app.llm.base import LLMError
from app.llm.fake import FakeLLMProvider
from app.llm.prompts import VARIATION_REGISTERS
from app.retrieval.embeddings import HashingEmbeddingProvider
from app.services.troubleshooting import ServiceError
from app.validation.business_rules import validate_envelope
from app.validation.url_safety import scan_payload

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "data" / "dev_fixtures"
GOLD = json.loads((DEV / "eval" / "gold.json").read_text())["queries"]
SIIS = {d["query_id"]: d["siis_response"] for d in json.loads((DEV / "siis_responses.json").read_text())}
QUERY_TO_ID = {g["query"]: qid for qid, g in GOLD.items()}


def gold_plan(qid: str) -> dict:
    g = GOLD[qid]
    return {
        "has_solution": True,
        "goals": [{
            "topic": g["topic"], "goal_type": g["kind"], "title": g["title"],
            "actions": [{"action_name": a["name"], "description": a["desc"], "category": a["category"],
                         "steps": a["steps"]} for a in g["actions"]],
        }],
    }


def complaint_of(prompt: str) -> str:
    m = re.search(r"COMPLAINT:\n(.*?)\n", prompt)
    return m.group(1).strip() if m else ""


class GoldLLM(FakeLLMProvider):
    """Echoes the gold plan of the SIIS document in the prompt; `mutate` injects hostile output."""

    def __init__(self, mutate=None, fail=False):
        super().__init__(self._respond)
        self.mutate = mutate
        self.fail = fail

    def _respond(self, system, prompt, schema):
        if self.fail:
            return LLMError("model down", code="llm_unavailable")
        if "paraphrases" in system:
            q = complaint_of(prompt)
            return {r: f"{q} ({r.replace('_', ' ')} phrasing)" for r in VARIATION_REGISTERS}
        qid = next((k for k, t in SIIS.items() if t.strip()[:60] in prompt), None)
        plan = gold_plan(qid) if qid else {"has_solution": False, "goals": []}
        return self.mutate(plan, prompt) if self.mutate else plan


def make(tmp_path, llm, **overrides):
    params = dict(
        data_dir=DEV, artifacts_dir=tmp_path, llm_provider="fake", embedding_provider="hashing",
        cache_backend="memory", semantic_cache_threshold=0.75, siis_retrieval_min_score=0.55,
    )
    params.update(overrides)
    s = Settings(**params)
    return build_components(s, llm=llm, embedder=HashingEmbeddingProvider(), cache_store=MemoryCacheStore(),
                            import_prewarm=False)


def run(c, query, siis=None, **kw):
    return asyncio.run(c.service.troubleshoot(query, siis, **kw))


def assert_contract(c, body):
    rep = validate_envelope(body, c.registry)
    assert rep.ok, rep.summary()
    assert scan_payload(body) == []


# --------------------------------------------------------------------------- core flow
def test_cold_path_then_exact_and_semantic_hits(tmp_path):
    llm = GoldLLM()
    c = make(tmp_path, llm)
    q = GOLD["B01"]["query"]
    r1 = run(c, q, SIIS["B01"])
    assert r1.status_code == 200 and r1.body["meta"]["cache_hit"] is False
    assert_contract(c, r1.body)
    calls = len(llm.calls)
    assert calls == 2  # extraction + variations
    r2 = run(c, q, SIIS["B01"])
    assert r2.body["meta"]["cache_hit"] is True and r2.telemetry["cache_kind"] == "exact"
    assert r2.body["meta"]["cost_usd"] == 0.0 and len(llm.calls) == calls  # no LLM on a hit
    assert r2.body["response"] == r1.body["response"]


def test_kb_grounding_without_siis_and_paraphrase_hit(tmp_path):
    llm = GoldLLM()
    c = make(tmp_path, llm)
    r1 = run(c, GOLD["C02"]["query"])  # no siis_response → KB retrieval
    assert r1.status_code == 200 and r1.telemetry["sources"][0]["doc_id"] == "S-C02"
    assert_contract(c, r1.body)
    n = len(llm.calls)
    r2 = run(c, "Camera app keeps crashing with a camera failed warning")
    assert r2.body["meta"]["cache_hit"] is True and len(llm.calls) == n


def test_worked_example_shape(tmp_path):
    c = make(tmp_path, GoldLLM())
    r = run(c, GOLD["D01"]["query"], SIIS["D01"])
    goal = r.body["response"]["contexts"][0]
    assert goal["goal"] == "Follow these steps to perform this Swipe Navigation Troubleshooting"
    assert goal["title"] == "Swipe navigation settings"
    (action,) = goal["actions"]
    assert action["category"] == "auto"
    dl = action["stepGroups"][0]["actionableDeeplink"]
    assert dl == {"deeplink": DUMMY_POSITIVE_URI, "description": "Open navigation bar settings under Display",
                  "message": "Choose navigation type in Display settings"}
    assert action["stepGroups"][0]["validationDeeplink"] is None


def test_no_siis_context_fallback(tmp_path):
    c = make(tmp_path, GoldLLM())
    r = run(c, "How do I change my ringtone?")
    assert r.body["response"]["contexts"] == [] and r.body["meta"]["fallback"] == "no_siis_context"
    assert_contract(c, r.body)


def test_no_match_fallback(tmp_path):
    c = make(tmp_path, GoldLLM())
    text = "Fingerprint recognition underwater\n\nThis is a hardware limitation and cannot be changed."
    r = run(c, "Can I use the fingerprint sensor underwater?", text)
    assert r.body["response"]["contexts"] == [] and r.body["meta"]["fallback"] == "no_match"
    assert_contract(c, r.body)
    assert len(c.cache) == 0  # fallbacks are never cached


def test_model_failure_uses_grounded_rules_fallback_and_is_not_cached(tmp_path):
    c = make(tmp_path, GoldLLM(fail=True))
    r = run(c, GOLD["P01"]["query"], SIIS["P01"])
    assert r.status_code == 200 and "rules_fallback" in r.telemetry["notes"]
    assert r.body["meta"]["model"] == "rules-extractor"
    assert_contract(c, r.body)
    assert len(c.cache) == 0


def test_model_failure_without_grounded_steps_is_a_safe_error(tmp_path):
    c = make(tmp_path, GoldLLM(fail=True))
    with pytest.raises(ServiceError) as exc:
        run(c, "battery drains", "Battery information\n\nBattery life varies with usage.")
    assert exc.value.code == "extraction_unavailable"


def test_determinism_without_cache(tmp_path):
    c = make(tmp_path, GoldLLM())
    bodies = [run(c, GOLD["C02"]["query"], SIIS["C02"], read_cache=False, write_cache=False).body for _ in range(3)]
    assert all(b["response"] == bodies[0]["response"] for b in bodies)


# ------------------------------------------------------------------ hostile model output
def _first_actions(plan):
    return plan["goals"][0]["actions"]


def test_url_and_uri_injection_is_scrubbed(tmp_path):
    def mutate(plan, prompt):
        p = copy.deepcopy(plan)
        acts = _first_actions(p)
        acts[0]["steps"].append("Visit https://www.samsung.com/support for more help.")
        acts[0]["steps"].insert(1, "Read [this guide](http://example.com/x) and tap Battery.")
        acts[0]["description"] = "It will help, see www.samsung.com"
        acts[0]["action_name"] += " (samsung.com/battery)"
        acts[1]["steps"].append("Open bixby://masked/act/deadbeef00 to jump there.")
        p["goals"][0]["title"] = "Battery drain [guide](https://x.y)"
        return p

    c = make(tmp_path, GoldLLM(mutate))
    r = run(c, GOLD["B01"]["query"], SIIS["B01"])
    assert_contract(c, r.body)
    text = json.dumps(r.body)
    assert "http" not in text and "www." not in text and "samsung.com" not in text and "deadbeef" not in text


def test_fragmented_actions_are_regrouped(tmp_path):
    def mutate(plan, prompt):
        return {"has_solution": True, "goals": [{
            "topic": "Swipe Navigation", "goal_type": "Troubleshooting", "title": "Swipe navigation settings",
            "actions": [
                {"action_name": "Open Settings", "description": "It will open the settings app", "category": "auto",
                 "steps": ["Open Settings."]},
                {"action_name": "Open Display", "description": "It will open display settings now", "category": "auto",
                 "steps": ["Tap Display."]},
                {"action_name": "Choose Navigation Type", "description": "It will let you choose navigation type",
                 "category": "auto", "steps": ["Tap Navigation bar.",
                                               "Select your preferred navigation type between Buttons and Swipe gestures."]},
            ]}]}

    c = make(tmp_path, GoldLLM(mutate))
    r = run(c, GOLD["D01"]["query"], SIIS["D01"])
    acts = r.body["response"]["contexts"][0]["actions"]
    assert len(acts) == 1 and acts[0]["stepGroups"][0]["steps"][:3] == ["Open Settings.", "Tap Display.", "Tap Navigation bar."]
    assert_contract(c, r.body)


def test_bundled_screens_are_split(tmp_path):
    def mutate(plan, prompt):
        return {"has_solution": True, "goals": [{
            "topic": "Battery Drain", "goal_type": "Troubleshooting", "title": "Battery fast drain",
            "actions": [{"action_name": "Save Battery", "description": "It will save battery power", "category": "auto",
                         "steps": ["Open Settings.", "Tap Battery.", "Turn on Power saving.",
                                   "Open Settings.", "Tap Lock screen and AOD.", "Turn off Always On Display."]}]}]}

    c = make(tmp_path, GoldLLM(mutate))
    r = run(c, GOLD["B01"]["query"], SIIS["B01"])
    acts = r.body["response"]["contexts"][0]["actions"]
    assert len(acts) == 2
    uris = [a["stepGroups"][0]["actionableDeeplink"]["deeplink"] for a in acts]
    assert len(set(uris)) == 2
    assert_contract(c, r.body)


def test_critical_actions_are_sequenced_last_and_manual_never_linked(tmp_path):
    def mutate(plan, prompt):
        p = copy.deepcopy(plan)
        acts = _first_actions(p)
        acts.reverse()  # restart/update first, settings last
        for a in acts:
            if a["category"] == "manual":
                a["category"] = "auto"  # the LLM mislabels a physical step
        return p

    c = make(tmp_path, GoldLLM(mutate))
    r = run(c, GOLD["B04"]["query"], SIIS["B04"])
    acts = r.body["response"]["contexts"][0]["actions"]
    cats = [a["category"] for a in acts]
    assert cats[-1] == "critical" and "critical" not in cats[:-1]
    for a in acts:
        if a["category"] == "manual":
            assert a["stepGroups"][0]["actionableDeeplink"] is None
    assert "manual" in cats
    assert_contract(c, r.body)


@pytest.mark.parametrize(
    "raw",
    [
        "```json\n{goal}\n```",
        "Sure! Here is the plan you asked for:\n{goal}\nLet me know if you need more.",
        "{goal_trailing_comma}",
    ],
)
def test_malformed_but_recoverable_json(tmp_path, raw):
    def responder(plan, prompt):
        text = json.dumps(plan)
        return raw.replace("{goal}", text).replace("{goal_trailing_comma}", text[:-2] + ",]}")

    c = make(tmp_path, GoldLLM(responder))
    r = run(c, GOLD["C07"]["query"], SIIS["C07"])
    assert r.body["response"]["contexts"] and r.telemetry["extraction_modes"] == ["llm"]
    assert_contract(c, r.body)


def test_unparseable_output_is_retried_once_then_rules_fallback(tmp_path):
    llm = GoldLLM(lambda plan, prompt: "I cannot comply" if "SOURCE" in prompt else plan)
    c = make(tmp_path, llm, llm_repair_retries=1)
    r = run(c, GOLD["C08"]["query"], SIIS["C08"])
    extraction_calls = [x for x in llm.calls if "SOURCE" in x["prompt"]]
    assert len(extraction_calls) == 2  # original + one bounded repair
    assert r.telemetry["extraction_modes"] == ["rules"]
    assert_contract(c, r.body)


def test_wrong_types_invalid_categories_and_bad_text_are_repaired(tmp_path):
    def mutate(plan, prompt):
        p = copy.deepcopy(plan)
        a = _first_actions(p)[0]
        a["category"] = "URGENT!!"
        a["steps"] = " ".join(a["steps"])  # a single string instead of a list
        a["description"] = "This will make your battery last much longer than it does now"
        a["action_name"] = "check battery usage"
        p["goals"][0]["title"] = "The battery of my phone drains extremely quickly"
        return p

    c = make(tmp_path, GoldLLM(mutate))
    r = run(c, GOLD["B01"]["query"], SIIS["B01"])
    assert_contract(c, r.body)
    first = r.body["response"]["contexts"][0]["actions"][0]
    assert first["actionName"] == "Check Battery Usage"
    assert first["stepGroups"][0]["steps"][:2] == ["Open Settings.", "Tap Battery."]


def test_hallucinated_steps_are_removed(tmp_path):
    def mutate(plan, prompt):
        p = copy.deepcopy(plan)
        _first_actions(p).append({"action_name": "Recalibrate Battery", "description": "It will recalibrate your battery",
                                  "category": "auto", "steps": ["Open Settings.", "Tap Battery calibration.",
                                                                "Tap Start calibration."]})
        _first_actions(p)[0]["steps"].append("Tap Advanced battery diagnostics.")
        return p

    c = make(tmp_path, GoldLLM(mutate))
    r = run(c, GOLD["B01"]["query"], SIIS["B01"])
    text = json.dumps(r.body)
    assert "calibration" not in text.lower() and "diagnostics" not in text.lower()
    assert_contract(c, r.body)


def test_request_scoped_plan_does_not_poison_kb_answers(tmp_path):
    def mutate(plan, prompt):
        if "EVIL" in prompt:
            return {"has_solution": True, "goals": [{"topic": "Battery Drain", "goal_type": "Troubleshooting",
                    "title": "Battery fast drain", "actions": [{"action_name": "Factory Reset",
                    "description": "It will erase everything on phone", "category": "critical",
                    "steps": ["Open Settings.", "Tap General management.", "Tap Reset.", "Tap Factory data reset."]}]}]}
        return plan

    c = make(tmp_path, GoldLLM(mutate))
    evil = "EVIL guide\n\nOpen Settings, tap General management, tap Reset, and then tap Factory data reset."
    q = GOLD["B01"]["query"]
    r1 = run(c, q, evil)
    assert "Factory" in json.dumps(r1.body)
    r2 = run(c, q)  # another user, no siis → must not receive the attacker's plan
    assert "Factory" not in json.dumps(r2.body)
    assert r2.telemetry["sources"] and r2.telemetry["sources"][0]["doc_id"] == "S-B01"


def test_multi_intent_query_from_cache(tmp_path):
    # The lexical hashing embedder needs a lower threshold than the calibrated neural model.
    c = make(tmp_path, GoldLLM(), semantic_cache_threshold=0.6)
    run(c, GOLD["D02"]["query"])  # warm the flicker plan (KB)
    run(c, GOLD["B01"]["query"])  # warm the battery plan (KB)
    r = run(c, "Screen flickers and the battery dies fast")
    assert r.body["meta"]["cache_hit"] is True
    assert len(r.body["response"]["contexts"]) == 2
    assert_contract(c, r.body)


def test_llm_mapping_calls_are_billed_and_invented_uris_never_emitted(tmp_path):
    """Ablation baseline (DEEPLINK_MAPPER=llm): every mapping call is metered like any other model call,
    and a URI the model invents is recorded but never reaches the response."""
    from app.llm.prompts import MAPPING_SYSTEM

    invented = "bixby://masked/act/000000000000"

    class MappingLLM(GoldLLM):
        def _respond(self, system, prompt, schema):
            if system == MAPPING_SYSTEM:
                return {"deeplink": invented}
            return super()._respond(system, prompt, schema)

        def estimate_cost(self, usage, model=None):
            return 0.001 * usage.calls

    llm = MappingLLM()
    c = make(tmp_path, llm, deeplink_mapper="llm", cache_read_enabled=False, cache_write_enabled=False)
    r = run(c, GOLD["B01"]["query"], SIIS["B01"])
    assert r.status_code == 200
    assert_contract(c, r.body)
    mapping_calls = sum(1 for call in llm.calls if call["system"] == MAPPING_SYSTEM)
    assert mapping_calls >= 1 and r.telemetry["mapping_llm_calls"] == mapping_calls
    assert r.telemetry["model_calls"] == len(llm.calls)  # extraction + variations + mapping
    assert r.body["meta"]["cost_usd"] == pytest.approx(0.001 * len(llm.calls))
    assert invented in r.telemetry["fabricated_uris"]
    assert invented not in json.dumps(r.body)
