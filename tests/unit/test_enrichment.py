from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.constants import VARIATIONS_MAX, VARIATIONS_MIN
from app.services.query_enrichment import QueryEnricher, dedupe_variations, finalize_variations

DEV = Path(__file__).resolve().parents[2] / "data" / "dev_fixtures"


@pytest.fixture(scope="module")
def enricher():
    siis = json.loads((DEV / "siis_responses.json").read_text())
    cat = json.loads((DEV / "deeplinks.json").read_text())
    return QueryEnricher([s["siis_response"] for s in siis] + [c["description"] for c in cat])


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  My   PHONE's battery   DIES!!! ", "my phone battery dies"),
        ("yo my cam pics r all blurry af", "my camera photos are all blurry"),
        ("it's very very slow", "it is very slow"),
        ("Hi, please help me, the screen won't turn on", "the screen will not turn on"),
        ("battery dies, see https://evil.example.com", "battery dies see"),
        ("How do I reset my Samsung TV remote?", "how do i reset my samsung tv remote"),
    ],
)
def test_normalize_query(enricher, raw, expected):
    assert enricher.normalize_query(raw) == expected


def test_typo_heavy_query_is_repaired(enricher):
    out = enricher.normalize_query("my phnoe batery drainz sooo fsat plz hlp")
    assert out.startswith("my phone battery drain") and "fast" in out


def test_negation_is_preserved(enricher):
    assert "not" in enricher.normalize_query("phone is NOT charging")


def test_all_canonical_queries_get_correct_domain_and_kind(enricher):
    gold = json.loads((DEV / "eval" / "gold.json").read_text())["queries"]
    for qid, g in gold.items():
        i = enricher.analyze(g["query"])
        assert (i.domain, i.goal_kind) == (g["domain"], g["kind"]), qid


def test_intent_is_deterministic(enricher):
    a = enricher.analyze("Swipe gestures go the wrong way after installing an app")
    b = enricher.analyze("Swipe gestures go the wrong way after installing an app")
    assert a == b


@pytest.mark.parametrize(
    "a,b",
    [
        ("My phone's swipe is going the wrong way after installing an app",
         "swipe gestures move in the wrong direction after app installation"),
        ("Battery drains quickly", "My phone battery dies very fast"),
        ("Camera photos come out blurry", "pics are fuzzy and out of focus"),
    ],
)
def test_paraphrases_converge_to_same_signature(enricher, a, b):
    assert enricher.analyze(a).signature == enricher.analyze(b).signature


@pytest.mark.parametrize(
    "a,b",
    [
        ("battery drains after update", "phone becomes hot during charging"),
        ("screen is too dim", "screen flickers"),
        ("camera app crashes", "camera photos are blurry"),
    ],
)
def test_distinct_complaints_do_not_collapse(enricher, a, b):
    assert enricher.analyze(a).signature != enricher.analyze(b).signature


def test_multi_intent_split(enricher):
    i = enricher.analyze("Screen flickers and the battery dies fast")
    assert [s.domain for s in i.sub_intents] == ["Display", "Battery"]
    single = enricher.analyze("my screen flickers and flashes")
    assert not single.sub_intents


def test_template_variations_are_distinct_and_in_range(enricher):
    for q in ["My phone battery dies very fast", "Camera photos come out blurry", "zzz something odd happened"]:
        v = enricher.template_variations(q, enricher.analyze(q))
        assert VARIATIONS_MIN <= len(v) <= VARIATIONS_MAX
        assert len({x.lower() for x in v}) == len(v)
        assert all("http" not in x for x in v)


def test_finalize_variations_tops_up_and_dedupes():
    q = "battery dies fast"
    llm = ["Battery dies fast!", "battery dies fast", "My battery drains quickly", "", None]
    fb = [f"fallback variant number {i}" for i in range(10)]
    out = finalize_variations(q, llm, fb)
    assert VARIATIONS_MIN <= len(out) <= VARIATIONS_MAX
    assert out[0] == "My battery drains quickly"
    assert dedupe_variations(q, ["battery dies fast"]) == []
