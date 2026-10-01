"""Official dataset format: voiceassist:// catalog with its own dummy entry, generated descriptions with
validation keys, and SIIS payloads given as {title, content} objects."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.api.schemas import TroubleshootRequest
from app.catalog.loaders import parse_catalog, parse_siis, siis_text
from app.catalog.registry import CatalogRegistry
from app.core.constants import is_dummy_uri
from app.retrieval.indexes import candidate_label

ROOT = Path(__file__).resolve().parents[2]

CATALOG = {
    "_readme": "masked",
    "count": 3,
    "deeplinks": [
        {"id": "DL-0001", "deeplink": "voiceassist://masked/act/aa73a35e8d",
         "description": "Opens the 24-hour time format settings page in device Settings on the device.",
         "message": "Switch Time Format", "originalType": "onClickURL", "control_type": None,
         "qna_description": "Switches between 12-hour and 24-hour time format.",
         "validation": {"deeplink": "voiceassist://masked/val/ef6814259a", "key": "Use 24-hour format"}},
        {"id": "DL-0002", "deeplink": "voiceassist://masked/act/3af2385547",
         "description": "Disables extra dim via device Settings on the device.", "message": "Disable Extra dim",
         "originalType": "offURL", "control_type": 2, "qna_description": "Extra dims the screen.", "validation": None},
        {"id": "DL-0578", "deeplink": "voiceassist://dummy_positive",
         "description": "Generic placeholder for a Settings screen that has no dedicated entry in this catalog",
         "message": "Open the relevant Settings screen", "originalType": "placeholder", "control_type": None,
         "qna_description": None, "validation": None},
    ],
}


def _registry() -> CatalogRegistry:
    entries, issues = parse_catalog(CATALOG)
    return CatalogRegistry(entries, issues=issues)


def test_catalog_dummy_comes_from_the_catalog():
    reg = _registry()
    assert reg.dummy_uri == "voiceassist://dummy_positive"
    assert is_dummy_uri(reg.dummy_uri) and not is_dummy_uri("voiceassist://masked/act/aa73a35e8d")
    assert len(reg) == 2  # the placeholder is never a retrievable target
    assert reg.is_valid_catalog_deeplink("voiceassist://dummy_positive", allow_dummy=True)
    assert not reg.is_valid_catalog_deeplink("bixby://dummy_positive", allow_dummy=True)
    assert not reg.is_valid_catalog_deeplink("voiceassist://dummy_positive", allow_dummy=False)


def test_integer_control_type_is_kept_as_text():
    entries, _ = parse_catalog(CATALOG)
    assert entries[1].control_type == "2"


@pytest.mark.parametrize("idx,label", [(0, "Use 24-hour format"), (1, "extra dim")])
def test_generated_descriptions_yield_screen_labels(idx, label):
    entries, _ = parse_catalog(CATALOG)
    assert candidate_label(entries[idx]) == label


def test_siis_object_payloads():
    obj = {"title": "Screen flickers", "content": "Navigate to Settings.\nTap Display."}
    assert siis_text(obj) == "Screen flickers\n\nNavigate to Settings.\nTap Display."
    docs, issues = parse_siis({"responses": [{"id": "row_1", "original_query": "1. My screen flickers",
                                              "siis_response": obj}]})
    assert not issues and docs[0].title == "Screen flickers" and docs[0].text.endswith("Tap Display.")
    req = TroubleshootRequest(query="screen flickers", siis_response=obj)
    assert req.siis_response.startswith("Screen flickers")
    with pytest.raises(ValueError):
        TroubleshootRequest(query="screen flickers", siis_response=["x"])


@pytest.mark.skipif(not (ROOT / "data" / "official" / "deeplinks.json").exists(), reason="official data not present")
def test_official_files_load():
    from app.catalog.loaders import load_catalog, read_json

    entries, _, _ = load_catalog(ROOT / "data" / "official" / "deeplinks.json")
    reg = CatalogRegistry(entries)
    assert reg.dummy_uri == "voiceassist://dummy_positive" and len(reg) == 577
    docs, issues = parse_siis(read_json(ROOT / "data" / "official" / "siis_responses.json"))
    assert len(docs) == 20 and not [i for i in issues if i.code == "missing_text"]


def test_rules_extractor_reads_markdown_articles():
    from app.services.rules_extractor import extract_rules

    text = ("Screen issues\n\n# Troubleshooting\n## Step 1: Restart in Safe Mode\nPress and hold the Power button.\n"
            "Tap Safe mode.\n## Note\nNote: Safe mode is not supported on every model.\n"
            "## Check the display\nNavigate to Settings. Tap Display. Tap Touch sensitivity.")
    names = [a.action_name for a in extract_rules(text)]
    assert "Restart in Safe Mode" in names
    assert not any(n.startswith("#") or n.lower().startswith("note") for n in names)


def test_enable_name_selects_the_on_entry_when_steps_only_tap_the_item():
    from app.models.internal import ExtractedAction
    from app.retrieval.reranker import TargetResolver

    path = TargetResolver._path_with_hints(ExtractedAction(
        action_name="Enable Touch Sensitivity", description="", category="auto",
        steps=["Open Settings.", "Tap Display.", "Tap Touch sensitivity."]))
    assert (path.elements[-1].kind, path.elements[-1].state) == ("control", "on")
