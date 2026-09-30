from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Tests never call a real LLM or download models unless explicitly marked `live`.
os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("EMBEDDING_PROVIDER", "hashing")
os.environ.setdefault("CACHE_BACKEND", "memory")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from app.catalog.loaders import parse_catalog  # noqa: E402
from app.catalog.registry import CatalogRegistry  # noqa: E402

# Appendix B of the problem statement, verbatim (the long query lines are truncated in the
# PDF rendering; the visible text is used).
WORKED_EXAMPLE = {
    "query": "The mobile phone swipe navigation moves up or down instead of left or right after downloading an app.",
    "query_variations": [
        "Ever since I installed a new app, swiping on my phone scrolls up and down instead of going left or right.",
        "Why does my phone swipe vertically when I try to swipe sideways after downloading an app?",
        "phone swipe gestures wrong direction after app install",
        "I downloaded an application yesterday and now the swipe navigation on my Galaxy moves up or down when it should go sideways.",
        "Screen navigation gestures are misbehaving after an app download; horizontal swipes register as vertical.",
        "My phone's gesture navigation got messed up by a new app and swipes go the wrong way.",
        "What should I do when swiping left or right on my phone scrolls the screen up and down instead?",
        "Swipe navigation broken after installing app.",
        "This is so annoying - I can't swipe sideways anymore since installing that app, everything just scrolls vertically.",
        "Navigation swipes on my Samsung phone respond in the wrong axis after a recent app installation.",
    ],
    "response": {
        "contexts": [
            {
                "goal": "Follow these steps to perform this Swipe Navigation Troubleshooting",
                "title": "Swipe navigation settings",
                "score": 0.93,
                "actions": [
                    {
                        "actionName": "Configure Navigation Bar Settings",
                        "description": "It will let you choose navigation type",
                        "category": "auto",
                        "stepGroups": [
                            {
                                "steps": [
                                    "Navigate to and open Settings.",
                                    "Tap on Display.",
                                    "Tap on Navigation bar.",
                                    "Select your preferred navigation type between Buttons and Swipe gestures.",
                                    "Optionally toggle on Gesture hint to display guidance lines at the bottom of the screen.",
                                ],
                                "actionableDeeplink": {
                                    "deeplink": "bixby://dummy_positive",
                                    "description": "Open navigation bar settings under Display",
                                    "message": "Choose navigation type in Display settings",
                                },
                                "validationDeeplink": None,
                            }
                        ],
                    }
                ],
            }
        ]
    },
    "meta": {"latency_ms": 212, "cache_hit": True, "model": "gpt-4o-mini", "cost_usd": 0.0},
}

TEST_CATALOG_RECORDS = [
    {
        "deeplink": "bixby://masked/act/aaa001",
        "description": "Open Settings",
        "message": "Open the Settings app",
        "qna_description": "Open the main Settings menu",
        "classes": {"domain": "General", "screen": "Settings"},
        "originalType": "screen",
    },
    {
        "deeplink": "bixby://masked/act/aaa002",
        "description": "Open Display settings",
        "message": "Adjust display options",
        "qna_description": "Change brightness, screen mode, navigation and other display settings",
        "classes": {"domain": "Display", "screen": "Display"},
        "originalType": "screen",
    },
    {
        "deeplink": "bixby://masked/act/aaa003",
        "description": "Turn on Adaptive brightness",
        "message": "Automatically adjust brightness",
        "qna_description": "Let the phone adjust screen brightness to your surroundings",
        "classes": {"domain": "Display", "screen": "Display"},
        "originalType": "toggle",
        "validation": {
            "deeplink": "bixby://masked/val/aaa003",
            "key": "adaptive_brightness",
            "resultType": "boolean",
            "condition": "equal",
            "value": "true",
        },
    },
    {
        "deeplink": "bixby://masked/act/aaa004",
        "description": "Open Power saving settings",
        "message": "Extend battery life",
        "qna_description": "Turn on power saving to limit background usage",
        "classes": {"domain": "Battery", "screen": "Power saving"},
        "originalType": "screen",
    },
    {
        "deeplink": "bixby://masked/act/aaa005",
        "description": "Open Software update",
        "message": "Download and install the latest software",
        "qna_description": "Check for and install software updates",
        "classes": {"domain": "Performance", "screen": "Software update"},
        "originalType": "screen",
    },
]


@pytest.fixture(scope="session")
def test_registry() -> CatalogRegistry:
    entries, issues = parse_catalog(copy.deepcopy(TEST_CATALOG_RECORDS), "test_catalog")
    return CatalogRegistry(entries, issues=issues)


@pytest.fixture()
def worked_example() -> dict:
    return copy.deepcopy(WORKED_EXAMPLE)


def make_action(
    name="Turn On Power Saving",
    description="It will extend your battery life",
    category="auto",
    steps=None,
    deeplink=None,
    validation=None,
) -> dict:
    return {
        "actionName": name,
        "description": description,
        "category": category,
        "stepGroups": [
            {
                "steps": steps or ["Open Settings.", "Tap Battery.", "Turn on Power saving."],
                "validationDeeplink": validation,
                "actionableDeeplink": deeplink,
            }
        ],
    }


def catalog_deeplink(registry: CatalogRegistry, uri: str) -> dict:
    return registry.resolve_catalog_deeplink(uri).to_deeplink().model_dump(mode="json")


def make_envelope(actions: list[dict], *, title="Battery fast drain", score=0.9, goal=None, fallback=None) -> dict:
    contexts = (
        []
        if actions is None
        else [
            {
                "goal": goal or "Follow these steps to perform this Battery Drain Troubleshooting",
                "title": title,
                "score": score,
                "actions": actions,
            }
        ]
    )
    meta = {"latency_ms": 10, "cache_hit": False, "model": "test-model", "cost_usd": 0.0}
    if fallback:
        meta["fallback"] = fallback
    return {
        "query": "my battery dies really fast",
        "query_variations": [f"battery drains quickly variant {i}" for i in range(8)],
        "response": {"contexts": contexts},
        "meta": meta,
    }
