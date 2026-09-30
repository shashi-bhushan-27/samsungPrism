from __future__ import annotations

import pytest

from app.core import constants as C
from app.services import category_rules as R
from app.validation import repair
from app.validation import text_rules as T


@pytest.mark.parametrize(
    "title,fallback,expected",
    [
        ("Swipe Navigation Settings", "x y", "Swipe navigation settings"),
        ("Fix the battery drain issue", "x y", "Battery drain"),
        ("Battery drain.", "x y", "Battery drain"),
        ("Flickering", "Screen flickering", "Screen flickering"),
        ("Visit https://x.com for battery tips", "Battery fast drain", "Visit battery tips"),
    ],
)
def test_repair_title(title, fallback, expected):
    out = repair.repair_title(title, fallback)
    assert out == expected
    assert C.TITLE_MIN_WORDS <= T.count_words(out) <= C.TITLE_MAX_WORDS and T.is_sentence_case(out)


@pytest.mark.parametrize(
    "desc,name",
    [
        ("It will let you choose navigation type", "Configure Navigation Bar Settings"),
        ("Lets you choose the navigation type", "Configure Navigation Bar Settings"),
        ("This will really make your battery last a lot longer than before", "Turn On Power Saving"),
        ("It will save power", "Turn On Power Saving"),
        ("", "Put Unused Apps to Sleep"),
        ("It will help, see https://samsung.com", "Clear Camera Cache"),
    ],
)
def test_repair_description_always_valid(desc, name):
    out = repair.repair_description(desc, name)
    assert out.startswith("It will ")
    assert C.DESCRIPTION_MIN_WORDS <= T.count_words(out) <= C.DESCRIPTION_MAX_WORDS
    assert "http" not in out


def test_repair_description_keeps_compliant_text():
    assert repair.repair_description("It will let you choose navigation type", "X") == "It will let you choose navigation type"


def test_build_goal():
    assert repair.build_goal("swipe navigation", "Troubleshooting") == (
        "Follow these steps to perform this Swipe Navigation Troubleshooting"
    )
    assert repair.build_goal("Battery Drain Troubleshooting", "Troubleshooting").endswith("Battery Drain Troubleshooting")
    assert repair.build_goal("", "Configuration", "Navigation Bar").endswith("Navigation Bar Configuration")


def test_repair_steps_splits_and_scrubs():
    steps = ["Go to Settings > Battery", "Turn on Power saving and visit samsung.com/tips", "Turn on Power saving."]
    assert repair.repair_steps(steps) == ["Open Settings.", "Tap Battery.", "Turn on Power saving."]


# ----------------------------------------------------------------- categories
@pytest.mark.parametrize(
    "name,steps,expected",
    [
        ("Restart Your Phone", ["Press and hold the Side key.", "Tap Restart."], "critical"),
        ("Update Software", ["Open Settings.", "Tap Software update.", "Tap Download and install."], "critical"),
        ("Boot Into Safe Mode", ["Press and hold the Side key.", "Touch and hold Power off until Safe mode appears."], "critical"),
        ("Factory Reset", ["Open Settings.", "Tap General management.", "Tap Reset.", "Tap Factory data reset."], "critical"),
        ("Clean The Charging Port", ["Turn off the phone.", "Gently clean the charging port."], "manual"),
        ("Use A Different Charger", ["Use an original Samsung charger and cable."], "manual"),
        ("Visit A Service Center", ["Back up your data.", "Take your phone to an authorized service center."], "manual"),
        ("Turn On Power Saving", ["Open Settings.", "Tap Battery.", "Turn on Power saving."], "auto"),
        ("Enable Auto Restart", ["Open Settings.", "Tap Device care.", "Tap Auto optimization.", "Turn on Auto restart."], "auto"),
        ("Force Stop Camera", ["Open Settings.", "Tap Apps.", "Tap Camera.", "Tap Force stop."], "auto"),
        ("Update Apps", ["Open Galaxy Store.", "Tap Menu.", "Tap Updates.", "Tap Update all."], "auto"),
        ("Reset Camera Settings", ["Open the Camera app.", "Tap Settings.", "Tap Reset settings."], "auto"),
    ],
)
def test_category_classification(name, steps, expected):
    assert R.classify(name, steps).category == expected


def test_llm_critical_kept_when_destructive_wording():
    assert R.classify("Erase eSIM", ["Tap Erase eSIM profile."], proposed="critical").category == "critical"
    assert R.classify("Adjust Brightness", ["Drag the slider."], proposed="critical").category == "auto"


def test_order_keys_follow_disruption_hierarchy():
    toggle = R.order_key("auto", "Turn On Power Saving", ["Turn on Power saving."])
    cache = R.order_key("auto", "Clear Camera Cache", ["Tap Clear cache."])
    optimise = R.order_key("auto", "Optimize Device", ["Open Device care.", "Tap Optimize now."])
    manual = R.order_key("manual", "Clean Port", ["Clean the charging port."])
    restart = R.order_key("critical", "Restart", ["Tap Restart."])
    reset = R.order_key("critical", "Factory Reset", ["Tap Factory data reset."])
    assert toggle < cache < optimise < manual < restart < reset


def test_action_name_repair_keeps_valid_title_case():
    """Minimal intervention: a valid Title Case name is not re-cased (reference samples use "Turn On ...")."""
    from app.validation.repair import repair_action_name

    assert repair_action_name("Turn On Power Saving") == "Turn On Power Saving"
    assert repair_action_name("Turn on Power Saving") == "Turn on Power Saving"
    assert repair_action_name("turn on power saving") == "Turn on Power Saving"
    assert repair_action_name("Visit A Service Center.") == "Visit A Service Center"
