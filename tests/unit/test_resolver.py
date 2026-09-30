"""Exact target-screen resolution, parent-menu protection and dummy-positive behaviour."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.catalog.loaders import load_catalog, parse_catalog
from app.catalog.registry import CatalogRegistry
from app.core.constants import DUMMY_POSITIVE_URI
from app.models.internal import ExtractedAction
from app.retrieval.embeddings import HashingEmbeddingProvider
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.indexes import CatalogIndex
from app.retrieval.reranker import TargetResolver, dummy_texts
from app.retrieval.ui_path import parse_ui_path, primary_interaction

DEV = Path(__file__).resolve().parents[2] / "data" / "dev_fixtures"
META = json.loads((DEV / "eval" / "catalog_meta.json").read_text())["entries"]


def uri(entry_id: str) -> str:
    return META[entry_id]["uri"]


def build_resolver(registry: CatalogRegistry) -> TargetResolver:
    provider = HashingEmbeddingProvider()
    index = CatalogIndex(registry, provider, index_dir=None)
    retriever = HybridRetriever(index, top_k_bm25=25, top_k_dense=25, w_bm25=0.45, w_dense=0.55)
    return TargetResolver(index, retriever, provider, top_k_rerank=12, min_score=0.5, min_margin=0.03)


@pytest.fixture(scope="module")
def resolver() -> TargetResolver:
    entries, issues, fp = load_catalog(DEV / "deeplinks.json")
    return build_resolver(CatalogRegistry(entries, fingerprint=fp, issues=issues))


def resolve(resolver, name, steps, category="auto", domain=None):
    return resolver.resolve(ExtractedAction(name, "It will help fix it", category, steps), domain=domain)


@pytest.mark.parametrize(
    "name,steps,expected",
    [
        # rephrased navigation verbs
        ("Put Unused Apps to Sleep", ["Launch Settings.", "Select Battery.", "Select Background usage limits.",
                                      "Enable Put unused apps to sleep."], "put_unused_to_sleep"),
        ("Change Motion Smoothness", ["Go to Settings > Display > Motion smoothness.", "Pick Standard."], "motion_smoothness"),
        # no navigation at all
        ("Turn On Power Saving", ["Turn on Power saving."], "power_saving"),
        # toggle vs its screen: the toggle is the exact target
        ("Turn Off Always On Display", ["Open Settings.", "Tap Lock screen and AOD.", "Turn off Always On Display."], "aod"),
        # screen vs toggle with the same words: the screen is the exact target
        ("Schedule Dark Mode", ["Open Settings.", "Tap Display.", "Tap Dark mode settings.", "Turn on Turn on as scheduled."],
         "dark_mode_settings"),
        # button entry is more specific than its screen
        ("Optimize With Device Care", ["Open Settings.", "Tap Device care.", "Tap Optimize now."], "optimize_now"),
        # app-internal settings resolve inside the app, not to the system menu
        ("Reset Camera Settings", ["Open the Camera app.", "Tap Settings.", "Tap Reset settings.", "Tap Reset."], "camera_reset"),
        # One UI 5 hierarchy wording
        ("Turn On Power Saving", ["Open Settings.", "Tap Battery and device care.", "Tap Battery.", "Turn on Power saving."],
         "power_saving"),
    ],
)
def test_exact_target_with_varied_phrasing(resolver, name, steps, expected):
    d = resolve(resolver, name, steps)
    assert d.kind == "catalog", d.trace()
    assert d.uri == uri(expected), d.trace()


def test_parent_menu_is_never_returned_for_unindexed_child(resolver):
    """Appendix B: Navigation bar is not indexed → dummy_positive, never Display/Settings."""
    steps = ["Navigate to and open Settings.", "Tap on Display.", "Tap on Navigation bar.",
             "Select your preferred navigation type between Buttons and Swipe gestures.",
             "Optionally toggle on Gesture hint to display guidance lines at the bottom of the screen."]
    d = resolve(resolver, "Configure Navigation Bar Settings", steps)
    assert d.kind == "dummy"
    assert d.uri is None
    assert (d.dummy_description, d.dummy_message) == (
        "Open navigation bar settings under Display",
        "Choose navigation type in Display settings",
    )
    parents = [c for c in d.candidates if c.uri in (uri("display"), uri("settings"))]
    assert all(not c.exact for c in parents)


def test_exact_child_preferred_over_parent_when_indexed():
    records = json.loads((DEV / "deeplinks.json").read_text())
    records.append({
        "deeplink": "bixby://masked/act/navbar0001",
        "description": "Open Navigation bar settings",
        "message": "Choose buttons or swipe gestures",
        "qna_description": "Change the navigation type between navigation buttons and swipe gestures",
        "classes": {"category": "Display"},
        "originalType": "screen",
    })
    entries, issues = parse_catalog(records)
    r = build_resolver(CatalogRegistry(entries, issues=issues))
    steps = ["Open Settings.", "Tap Display.", "Tap Navigation bar.", "Select Swipe gestures."]
    d = resolve(r, "Configure Navigation Bar Settings", steps)
    assert d.kind == "catalog" and d.uri == "bixby://masked/act/navbar0001", d.trace()
    display = next(c for c in d.candidates if c.uri == uri("display"))
    assert display.parent and not display.exact


def test_duplicate_metadata_resolves_deterministically(resolver):
    steps = ["Open Settings.", "Tap Battery."]
    first = resolve(resolver, "Open Battery Settings", steps)
    second = resolve(resolver, "Open Battery Settings", steps)
    assert first.kind == "catalog" and first.uri == second.uri
    assert first.uri in (uri("battery"), uri("battery_legacy"))


def test_generic_app_screen_is_not_mapped_to_camera_specific_entry(resolver):
    steps = ["Open Settings.", "Tap Apps.", "Select the app.", "Tap Storage.", "Tap Clear cache."]
    d = resolve(resolver, "Clear The App Cache", steps)
    assert d.kind == "dummy", d.trace()


def test_unrelated_or_non_settings_actions_get_no_link(resolver):
    d = resolve(resolver, "Update Your Apps", ["Open Galaxy Store.", "Tap Menu.", "Tap Updates.", "Tap Update all."])
    assert d.kind == "none"
    d = resolve(resolver, "Close Background Apps", ["Tap the Recent apps button.", "Tap Close all."])
    assert d.kind == "none"


def test_manual_and_critical_rules(resolver):
    d = resolve(resolver, "Clean The Charging Port", ["Gently clean the charging port."], category="manual")
    assert d.kind == "none" and d.reason == "manual_action"
    d = resolve(resolver, "Restart Your Phone", ["Press and hold the Side key.", "Tap Restart."], category="critical")
    assert d.kind == "none"
    d = resolve(resolver, "Update Phone Software", ["Open Settings.", "Tap Software update.", "Tap Download and install."],
                category="critical")
    assert d.kind == "catalog" and d.uri == uri("software_update")
    # a critical action on an un-indexed screen never receives dummy_positive
    d = resolve(resolver, "Wipe Cache Partition", ["Open Settings.", "Tap Recovery options.", "Tap Wipe cache."],
                category="critical")
    assert d.kind == "none"


def test_catalog_uri_comes_from_registry_object(resolver):
    d = resolve(resolver, "Turn On Power Saving", ["Turn on Power saving."])
    assert d.doc is not None
    assert d.doc.entry is resolver.index.registry.get_entry(d.uri)


def test_dummy_text_variants():
    p = parse_ui_path(["Open Settings.", "Tap Apps.", "Select the app.", "Tap Storage.", "Tap Clear cache."])
    desc, msg = dummy_texts(p, primary_interaction(p, "Clear The App Cache"))
    assert desc == "Open storage settings for the selected app"
    assert msg == "Tap Clear cache in Storage settings"
