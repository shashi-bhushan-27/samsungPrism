from __future__ import annotations

import pytest

from app.validation import text_rules as T
from app.validation.url_safety import find_urls, sanitize_step, sanitize_text, scan_payload


@pytest.mark.parametrize(
    "step,n",
    [
        ("Navigate to and open Settings.", 1),
        ("Tap on Display.", 1),
        ("Select your preferred navigation type between Buttons and Swipe gestures.", 1),
        ("Optionally toggle on Gesture hint to display guidance lines at the bottom of the screen.", 1),
        ("Press and hold the Side key and the Volume down key.", 1),
        ("If prompted, tap Restart.", 1),
        ("In Settings, tap Display.", 1),
        ("Turn on Power saving to extend battery life.", 1),
        ("Open Settings and tap Display.", 2),
        ("Open Settings, tap Battery, and then tap Background usage limits.", 3),
        ("Tap Battery, then Background usage limits.", 2),
        ("Go to Settings > Display > Navigation bar.", 3),
        ("Open Settings. Tap Display.", 2),
    ],
)
def test_count_interactions(step, n):
    assert T.count_interactions(step) == n


@pytest.mark.parametrize(
    "step,expected",
    [
        ("Go to Settings > Display > Navigation bar.", ["Open Settings.", "Tap Display.", "Tap Navigation bar."]),
        ("Open Settings, tap Battery, and then tap Power saving.", ["Open Settings.", "Tap Battery.", "Tap Power saving."]),
        ("Tap Battery, then Background usage limits.", ["Tap Battery.", "Tap Background usage limits."]),
        ("Open Settings > Battery and turn on Power saving", ["Open Settings.", "Tap Battery.", "Turn on Power saving."]),
        ("you should tap Restart", ["Tap Restart."]),
        ("Please tap OK", ["Tap OK."]),
        ("2. Tap Display", ["Tap Display."]),
    ],
)
def test_expand_step(step, expected):
    assert T.expand_step(step) == expected


@pytest.mark.parametrize(
    "step,ok",
    [
        ("Tap Restart.", True),
        ("Optionally toggle on Gesture hint.", True),
        ("If needed, turn off Fast charging.", True),
        ("Do not use a damaged cable.", True),
        ("Make sure the phone is charged.", True),
        ("The phone will restart.", False),
        ("You can tap Restart.", False),
        ("It is recommended to restart.", False),
    ],
)
def test_imperative(step, ok):
    assert T.is_imperative(step)[0] is ok


@pytest.mark.parametrize(
    "text,ok",
    [
        ("Swipe navigation settings", True),
        ("Battery fast drain", True),
        ("Wi-Fi keeps dropping", True),
        ("HDR photos blurry", True),
        ("Swipe Navigation Settings", False),
        ("swipe navigation", False),
        ("BATTERY DRAIN NOW", False),
    ],
)
def test_sentence_case(text, ok):
    assert T.is_sentence_case(text) is ok


@pytest.mark.parametrize(
    "text,ok",
    [
        ("Configure Navigation Bar Settings", True),
        ("Put Unused Apps to Sleep", True),
        ("Turn On Power Saving", True),
        ("Clear Camera App Cache", True),
        ("Configure navigation bar", False),
    ],
)
def test_title_case(text, ok):
    assert T.is_title_case(text) is ok


def test_word_count_ignores_punctuation_tokens():
    assert T.count_words("It will let you choose navigation type") == 7
    assert T.count_words("It will - help") == 3


# ------------------------------------------------------------------------ URL gate
@pytest.mark.parametrize(
    "text,kind",
    [
        ("see https://www.samsung.com/support", "scheme_url"),
        ("visit http://example.org", "scheme_url"),
        ("go to www.samsung.com", "www"),
        ("[guide](https://x.io/a)", "markdown_link"),
        ("open samsung.com/us/support/answer", "bare_domain"),
        ("support.google.com/android", "bare_domain"),
        ("hxxps://evil[.]com", "defanged_url"),
        ("evil[.]com/login", "defanged_url"),
        ("samsung dot com slash support", "spoken_domain"),
        ("192.168.0.1/admin", "ip_address"),
        ("bixby://masked/act/123", "scheme_url"),
        ("mailto:help@example.com", "pseudo_scheme"),
    ],
)
def test_find_urls(text, kind):
    found = find_urls(text)
    assert found and found[0].kind == kind


@pytest.mark.parametrize("text", ["e.g. tap Display", "One UI 6.1 update", "Android 14", "Tap OK.", "Select 1.5x speed", "config.json"])
def test_no_false_positive_urls(text):
    assert find_urls(text) == []


@pytest.mark.parametrize(
    "step,expected",
    [
        ("Visit samsung.com/support for more help.", []),
        ("Open Settings and visit samsung.com for details", ["Open Settings."]),
        ("See [the guide](https://x.y/z) then tap OK.", ["Tap OK."]),
        ("Tap Display (www.samsung.com).", ["Tap Display."]),
        ("Tap [Display](https://evil.com) to continue.", ["Tap Display to continue."]),
        ("If needed, visit https://samsung.com/support.", []),
        ("Tap Display.", ["Tap Display."]),
    ],
)
def test_sanitize_step(step, expected):
    assert sanitize_step(step) == expected


def test_sanitize_text_keeps_anchor_text():
    cleaned, found = sanitize_text("Read the [Battery guide](https://a.b/c) now")
    assert cleaned == "Read the Battery guide now" and len(found) == 1


def test_scan_payload_skips_catalog_deeplinks_but_flags_web_urls():
    payload = {"deeplink": "bixby://masked/act/1", "steps": ["Open https://evil.com"], "nested": {"deeplink": "http://x.y"}}
    leaks = scan_payload(payload)
    kinds = sorted(l.kind for l in leaks)
    assert kinds == ["non_bixby_deeplink", "scheme_url"]
