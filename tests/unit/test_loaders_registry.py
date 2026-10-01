from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.catalog.loaders import (
    link_queries_to_siis,
    load_catalog,
    load_samples,
    parse_catalog,
    parse_queries,
    parse_siis,
)
from app.catalog.registry import CatalogRegistry
from app.core.constants import DUMMY_POSITIVE_URI
from app.validation import text_rules as T

DEV = Path(__file__).resolve().parents[2] / "data" / "dev_fixtures"


@pytest.fixture(scope="module")
def dev_registry() -> CatalogRegistry:
    entries, issues, fp = load_catalog(DEV / "deeplinks.json")
    return CatalogRegistry(entries, fingerprint=fp, issues=issues)


def test_dev_catalog_loads_with_duplicate_and_ambiguity_reported(dev_registry):
    assert len(dev_registry) == 110
    assert len(dev_registry.duplicate_uris) == 1
    assert any(i.code == "duplicate_uri" for i in dev_registry.issues)
    assert dev_registry.ambiguous_groups, "battery / battery_legacy share description+message"


def test_every_catalog_uri_round_trips_exactly(dev_registry):
    raw = json.loads((DEV / "deeplinks.json").read_text(encoding="utf-8"))
    for rec in raw:
        entry = dev_registry.resolve_catalog_deeplink(rec["deeplink"])
        assert entry.uri == rec["deeplink"]
        assert entry.to_deeplink().deeplink == rec["deeplink"]
        if "validation" in rec:
            rule = dev_registry.get_validation_rule(rec["validation"]["deeplink"])
            assert rule is not None and rule.key == rec["validation"]["key"]


def test_registry_never_normalises(dev_registry):
    uri = dev_registry.entries[0].uri
    for variant in (uri.upper(), uri + "/", " " + uri, uri.replace("/", "%2F")):
        assert not dev_registry.is_valid_catalog_deeplink(variant)
        with pytest.raises(KeyError):
            dev_registry.resolve_catalog_deeplink(variant)
    assert not dev_registry.is_valid_catalog_deeplink(DUMMY_POSITIVE_URI)
    assert dev_registry.is_valid_catalog_deeplink(DUMMY_POSITIVE_URI, allow_dummy=True)


def test_semantic_text_never_contains_uri(dev_registry):
    for e in dev_registry.entries:
        assert "bixby://" not in e.semantic_text()


def test_catalog_alternative_shapes():
    rec = {"description": "Open Display settings", "message": "m"}
    by_uri = {"bixby://masked/act/x1": rec}
    entries, issues = parse_catalog(by_uri)
    assert entries[0].uri == "bixby://masked/act/x1"
    entries, _ = parse_catalog({"deeplinks": [{"uri": "bixby://masked/act/x2", "desc": "Open Battery"}]})
    assert entries[0].uri == "bixby://masked/act/x2" and entries[0].description == "Open Battery"


def test_catalog_malformed_records_are_reported_not_silently_dropped():
    entries, issues = parse_catalog(
        [
            {"deeplink": "https://www.samsung.com", "description": "web"},
            {"description": "no uri"},
            "not an object",
            {"deeplink": "bixby://masked/act/ok", "description": "Fine",
             "validation": {"key": "k", "resultType": "maybe", "condition": "equal", "value": True}},
        ]
    )
    codes = {i.code for i in issues}
    assert {"malformed_uri", "missing_uri", "record_not_object", "validation_bad_result_type"} <= codes
    assert [e.uri for e in entries] == ["bixby://masked/act/ok"]
    assert entries[0].validation.value == "True"


def test_queries_and_siis_link(tmp_path):
    queries, qi = parse_queries(json.loads((DEV / "queries.json").read_text()))
    docs, si = parse_siis(json.loads((DEV / "siis_responses.json").read_text()))
    links, li = link_queries_to_siis(queries, docs)
    assert len(queries) == 32 and len(docs) == 32 and len(links) == 32
    assert not qi and not si and not li
    assert {q.domain for q in queries} == {"Battery", "Display", "Camera", "Performance"}


def test_queries_shapes():
    qs, _ = parse_queries(["battery dies", "screen flickers"])
    assert [q.id for q in qs] == ["Q001", "Q002"]
    qs, _ = parse_queries({"a1": {"text": "x", "category": "Battery"}})
    assert qs[0].id == "a1" and qs[0].domain == "Battery"


def test_siis_index_alignment_fallback():
    qs, _ = parse_queries(["q1", "q2"])
    docs, _ = parse_siis(["text one", "text two"])
    links, issues = link_queries_to_siis(qs, docs)
    assert links == {"Q001": "S001", "Q002": "S002"}
    assert any(i.code == "linked_by_index" for i in issues)


def test_samples_load(tmp_path):
    samples, issues = load_samples(DEV / "samples")
    assert len(samples) == 5 and not issues
    assert all(s.expected_contexts for s in samples)
    # paired-file layout
    (tmp_path / "s1_input.json").write_text(json.dumps({"query": "q", "siis_response": "t"}))
    (tmp_path / "s1_output.json").write_text(json.dumps({"contexts": []}))
    samples, issues = load_samples(tmp_path)
    assert len(samples) == 1 and samples[0].query == "q"


@pytest.mark.parametrize(
    "step,n",
    [
        ("Tap Download and install.", 1),
        ("Tap Backup and restore.", 1),
        ("Tap Security and privacy.", 1),
        ("Clean the screen with a soft, dry cloth.", 1),
        ("Remove the case and clean the lens.", 2),
        ("Turn off the phone and wait 30 seconds.", 2),
        ("Gently clean the charging port with a soft brush.", 1),
    ],
)
def test_label_and_adjective_boundaries(step, n):
    assert T.count_interactions(step) == n
    assert T.is_imperative(step)[0]


def test_catalog_fingerprint_ignores_line_endings_and_formatting(tmp_path):
    """Regression: a Windows (CRLF) checkout changed the byte hash, so every shipped pre-warmed plan was
    discarded as built for another catalog (startup log: plans=0)."""
    import json as _json

    from app.catalog.loaders import load_catalog

    src = Path(__file__).resolve().parents[2] / "data" / "dev_fixtures" / "deeplinks.json"
    crlf = tmp_path / "crlf.json"
    crlf.write_bytes(src.read_bytes().replace(b"\n", b"\r\n"))
    reindented = tmp_path / "reindented.json"
    reindented.write_text(_json.dumps(_json.loads(src.read_text(encoding="utf-8")), indent=4), encoding="utf-8")
    fp = load_catalog(src)[2]
    assert load_catalog(crlf)[2] == fp and load_catalog(reindented)[2] == fp
    changed = _json.loads(src.read_text(encoding="utf-8"))
    changed[0]["description"] += " (edited)"
    edited = tmp_path / "edited.json"
    edited.write_text(_json.dumps(changed), encoding="utf-8")
    assert load_catalog(edited)[2] != fp  # a real content change still invalidates cached plans
