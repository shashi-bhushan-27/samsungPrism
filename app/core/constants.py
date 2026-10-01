"""Contract constants taken from the problem statement (PDF §4) — single source for every gate."""

from __future__ import annotations

PIPELINE_VERSION = "1.1.0"

# Reserved placeholder (PDF §3): only for a step that opens a valid Settings screen
# that is not indexed in the catalog.
DUMMY_POSITIVE_URI = "bixby://dummy_positive"  # default; a catalog may define its own (e.g. voiceassist://)


def is_dummy_uri(uri: object) -> bool:
    """The reserved placeholder in any URI scheme (bixby://dummy_positive, voiceassist://dummy_positive, ...)."""
    return isinstance(uri, str) and uri.split("://", 1)[-1] == "dummy_positive" and "://" in uri

# Fallback codes (PDF §4.2.3 and §8 Phase 4).
FALLBACK_NO_MATCH = "no_match"
FALLBACK_NO_SIIS_CONTEXT = "no_siis_context"
FALLBACK_CODES = frozenset({FALLBACK_NO_MATCH, FALLBACK_NO_SIIS_CONTEXT})

# goal: "Follow these steps to perform this <Topic> Troubleshooting" (or "<Topic> Configuration").
GOAL_PREFIX = "Follow these steps to perform this "
GOAL_KIND_TROUBLESHOOTING = "Troubleshooting"
GOAL_KIND_CONFIGURATION = "Configuration"
GOAL_KINDS = (GOAL_KIND_TROUBLESHOOTING, GOAL_KIND_CONFIGURATION)
TOPIC_MIN_WORDS = 1
TOPIC_MAX_WORDS = 5

# title: 2 to 3 words, sentence case (PDF §4.1; see AUDIT.md D1).
TITLE_MIN_WORDS = 2
TITLE_MAX_WORDS = 3

# description: exactly 5 to 7 words, starting with "It will".
DESCRIPTION_PREFIX = "It will"
DESCRIPTION_MIN_WORDS = 5
DESCRIPTION_MAX_WORDS = 7

# actionName sanity bounds (spec only says Title Case / one screen).
ACTION_NAME_MIN_WORDS = 1
ACTION_NAME_MAX_WORDS = 8

STEP_MAX_WORDS = 40

# query_variations: 8 to 10 distinct paraphrases.
VARIATIONS_MIN = 8
VARIATIONS_MAX = 10

SCORE_MIN = 0.0
SCORE_MAX = 1.0

DOMAINS = ("Battery", "Display", "Camera", "Performance")

CATEGORY_AUTO = "auto"
CATEGORY_MANUAL = "manual"
CATEGORY_CRITICAL = "critical"
CATEGORIES = (CATEGORY_AUTO, CATEGORY_MANUAL, CATEGORY_CRITICAL)
