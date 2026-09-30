"""Contract tests: schema.py + business rules + URL gate + catalog gate (Phase 1)."""

from __future__ import annotations

import json

import pytest

import schema
from app.validation.business_rules import validate_envelope
from app.validation.schema_validator import parse_jsonl_line, strict_json_loads
from tests.conftest import catalog_deeplink, make_action, make_envelope


def codes(report):
    return {v.code for v in report.errors}


# ----------------------------------------------------------------- worked example
def test_worked_example_is_schema_valid_and_rule_compliant(worked_example, test_registry):
    schema.ContextDeeplinkResponse.model_validate(worked_example["response"])
    report = validate_envelope(worked_example, test_registry)
    assert report.ok, report.summary()


def test_valid_catalog_backed_response(test_registry):
    action = make_action(deeplink=catalog_deeplink(test_registry, "bixby://masked/act/aaa004"))
    report = validate_envelope(make_envelope([action]), test_registry)
    assert report.ok, report.summary()


# ------------------------------------------------------------------------- score
@pytest.mark.parametrize("score", [1.5, -0.1, float("nan"), float("inf")])
def test_invalid_score(score, test_registry):
    env = make_envelope([make_action()], score=score)
    assert "score.range" in codes(validate_envelope(env, test_registry))


def test_integer_score_is_coerced_but_bool_rejected(test_registry):
    env = make_envelope([make_action()], score=1)
    assert validate_envelope(env, test_registry).ok
    env = make_envelope([make_action()], score=True)
    assert not validate_envelope(env, test_registry).ok


# ------------------------------------------------------------------------- title
@pytest.mark.parametrize("title", ["Swipe Navigation Settings", "battery fast drain", "BATTERY FAST DRAIN"])
def test_title_not_sentence_case(title, test_registry):
    assert "title.sentence_case" in codes(validate_envelope(make_envelope([make_action()], title=title), test_registry))


@pytest.mark.parametrize("title", ["Battery", "Battery drains very fast", "My phone battery drains very fast today"])
def test_title_word_count(title, test_registry):
    assert "title.word_count" in codes(validate_envelope(make_envelope([make_action()], title=title), test_registry))


def test_title_trailing_punctuation(test_registry):
    assert "title.punctuation" in codes(validate_envelope(make_envelope([make_action()], title="Battery drain."), test_registry))


# -------------------------------------------------------------------------- goal
@pytest.mark.parametrize(
    "goal",
    [
        "Follow these steps to perform Battery Drain Troubleshooting",
        "Follow these steps to perform this battery drain Troubleshooting",
        "Follow these steps to perform this Battery Drain troubleshooting",
        "Follow these steps to perform this Battery Drain Fixing",
        "Follow these steps to perform this Troubleshooting",
    ],
)
def test_goal_syntax(goal, test_registry):
    report = validate_envelope(make_envelope([make_action()], goal=goal), test_registry)
    assert codes(report) & {"goal.syntax", "goal.topic_title_case", "goal.topic_words"}


def test_goal_configuration_variant(test_registry):
    goal = "Follow these steps to perform this Navigation Bar Configuration"
    assert validate_envelope(make_envelope([make_action()], goal=goal), test_registry).ok


# ------------------------------------------------------------------- description
@pytest.mark.parametrize(
    "desc,code",
    [
        ("It will save power", "description.word_count"),
        ("It will make your battery last much longer today", "description.word_count"),
        ("This will extend your battery life", "description.prefix"),
        ("Extends your battery life a lot", "description.prefix"),
        ("it will extend your battery life", "description.prefix"),
    ],
)
def test_description_rules(desc, code, test_registry):
    env = make_envelope([make_action(description=desc)])
    assert code in codes(validate_envelope(env, test_registry))


# ---------------------------------------------------------------------- category
def test_invalid_category_fails_schema(test_registry):
    env = make_envelope([make_action(category="urgent")])
    report = validate_envelope(env, test_registry)
    assert not report.schema_valid


def test_null_category_rejected_by_business_rule(test_registry):
    env = make_envelope([make_action(category=None)])
    assert "category.valid" in codes(validate_envelope(env, test_registry))


def test_manual_action_with_actionable_deeplink(test_registry):
    action = make_action(
        name="Clean The Charging Port",
        description="It will restore a reliable charging connection",
        category="manual",
        steps=["Turn off the phone.", "Gently clean the charging port with a soft brush."],
        deeplink=catalog_deeplink(test_registry, "bixby://masked/act/aaa004"),
    )
    assert "deeplink.manual_actionable" in codes(validate_envelope(make_envelope([action]), test_registry))


def test_dummy_positive_not_allowed_for_critical(test_registry):
    action = make_action(
        name="Restart Your Phone",
        description="It will clear temporary system glitches",
        category="critical",
        steps=["Press and hold the Side key.", "Tap Restart."],
        deeplink={"deeplink": "bixby://dummy_positive", "description": "Open power menu"},
    )
    assert "deeplink.dummy_not_allowed" in codes(validate_envelope(make_envelope([action]), test_registry))


# ---------------------------------------------------------------------- ordering
def test_critical_action_incorrectly_ordered(test_registry):
    restart = make_action(
        name="Restart Your Phone",
        description="It will clear temporary system glitches",
        category="critical",
        steps=["Press and hold the Side key.", "Tap Restart."],
    )
    settings = make_action()
    assert "ordering.critical_last" in codes(validate_envelope(make_envelope([restart, settings]), test_registry))
    assert validate_envelope(make_envelope([settings, restart]), test_registry).ok


def test_critical_actions_ordered_by_disruption(test_registry):
    reset = make_action(
        name="Factory Reset Your Phone",
        description="It will restore original factory settings",
        category="critical",
        steps=["Open Settings.", "Tap General management.", "Tap Reset.", "Tap Factory data reset."],
    )
    restart = make_action(
        name="Restart Your Phone",
        description="It will clear temporary system glitches",
        category="critical",
        steps=["Press and hold the Side key.", "Tap Restart."],
    )
    assert "ordering.critical_rank" in codes(validate_envelope(make_envelope([reset, restart]), test_registry))


# --------------------------------------------------------------------- deeplinks
def test_fabricated_deeplink(test_registry):
    fake = {"deeplink": "bixby://masked/act/zzz999", "description": "Open Power saving settings"}
    report = validate_envelope(make_envelope([make_action(deeplink=fake)]), test_registry)
    assert "deeplink.catalog_membership" in codes(report)


@pytest.mark.parametrize(
    "mutated",
    ["bixby://masked/act/aaa004/", "BIXBY://masked/act/aaa004", "bixby://masked/act/AAA004", " bixby://masked/act/aaa004",
     "bixby://masked%2Fact/aaa004"],
)
def test_uri_mutation_is_rejected(mutated, test_registry):
    dl = catalog_deeplink(test_registry, "bixby://masked/act/aaa004")
    dl["deeplink"] = mutated
    report = validate_envelope(make_envelope([make_action(deeplink=dl)]), test_registry)
    assert "deeplink.catalog_membership" in codes(report)


def test_catalog_metadata_must_be_copied_verbatim(test_registry):
    dl = catalog_deeplink(test_registry, "bixby://masked/act/aaa004")
    dl["description"] = "Open power saving"  # altered text
    report = validate_envelope(make_envelope([make_action(deeplink=dl)]), test_registry)
    assert "deeplink.field_integrity" in codes(report)


def test_validation_deeplink_must_come_from_catalog(test_registry):
    good = test_registry.get_validation_rule("bixby://masked/val/aaa003").to_schema().model_dump(mode="json")
    action = make_action(
        name="Turn On Adaptive Brightness",
        description="It will adjust brightness to surroundings",
        steps=["Open Settings.", "Tap Display.", "Turn on Adaptive brightness."],
        deeplink=catalog_deeplink(test_registry, "bixby://masked/act/aaa003"),
        validation=good,
    )
    assert validate_envelope(make_envelope([action]), test_registry).ok
    bad = dict(good, deeplink="bixby://masked/val/nope")
    action["stepGroups"][0]["validationDeeplink"] = bad
    assert "validation.catalog_membership" in codes(validate_envelope(make_envelope([action]), test_registry))
    tampered = dict(good, value="false")
    action["stepGroups"][0]["validationDeeplink"] = tampered
    assert "validation.field_integrity" in codes(validate_envelope(make_envelope([action]), test_registry))


def test_duplicate_deeplink_target_across_actions(test_registry):
    dl = catalog_deeplink(test_registry, "bixby://masked/act/aaa004")
    a1 = make_action(deeplink=dl)
    a2 = make_action(name="Adjust Power Saving Options", deeplink=dl,
                     steps=["Open Settings.", "Tap Battery.", "Tap Power saving."])
    assert "action.unique_targets" in codes(validate_envelope(make_envelope([a1, a2]), test_registry))


def test_duplicate_action_names(test_registry):
    assert "action.unique_names" in codes(validate_envelope(make_envelope([make_action(), make_action()]), test_registry))


# ------------------------------------------------------------------- URL leakage
@pytest.mark.parametrize(
    "bad_step",
    [
        "Visit https://www.samsung.com/support for help.",
        "Go to www.samsung.com.",
        "Read [the guide](https://example.com/guide).",
        "Open samsung.com/us/support.",
        "Open bixby://masked/act/aaa004 directly.",
    ],
)
def test_url_in_steps_is_a_leak(bad_step, test_registry):
    env = make_envelope([make_action(steps=["Open Settings.", bad_step])])
    assert "url.leak" in codes(validate_envelope(env, test_registry))


def test_url_in_variations_or_query_is_a_leak(test_registry):
    env = make_envelope([make_action()])
    env["query_variations"][0] = "see https://support.example.com"
    assert "url.leak" in codes(validate_envelope(env, test_registry))
    env = make_envelope([make_action()])
    env["query"] = "battery dies, check http://x.io"
    assert "url.leak" in codes(validate_envelope(env, test_registry))


def test_https_value_in_deeplink_field_is_a_leak(test_registry):
    dl = {"deeplink": "https://www.samsung.com/support", "description": "Support"}
    report = validate_envelope(make_envelope([make_action(deeplink=dl)]), test_registry)
    assert {"url.leak", "deeplink.catalog_membership"} <= codes(report)


# ------------------------------------------------------------------------- steps
@pytest.mark.parametrize(
    "step,code",
    [
        ("The battery usage screen shows the apps.", "step.imperative"),
        ("You should restart the phone.", "step.imperative"),
        ("Open Settings and tap Battery.", "step.single_interaction"),
        ("Go to Settings > Battery > Power saving.", "step.single_interaction"),
        ("Open Settings, tap Battery, and then tap Power saving.", "step.single_interaction"),
    ],
)
def test_step_rules(step, code, test_registry):
    env = make_envelope([make_action(steps=[step, "Turn on Power saving."])])
    assert code in codes(validate_envelope(env, test_registry))


def test_over_bundled_action_is_flagged(test_registry):
    steps = ["Open Settings.", "Tap Display.", "Turn on Adaptive brightness.", "Open Settings.", "Tap Apps.", "Tap Camera."]
    env = make_envelope([make_action(name="Adjust Brightness", steps=steps)])
    assert "action.single_screen" in codes(validate_envelope(env, test_registry))


def test_bundled_action_name_is_flagged(test_registry):
    env = make_envelope([make_action(name="Configure Display and Clear Cache")])
    assert "action.name_single_feature" in codes(validate_envelope(env, test_registry))


def test_fragmented_actions_are_flagged(test_registry):
    a1 = make_action(name="Open Settings", description="It will open the settings app", steps=["Open Settings."])
    a2 = make_action(name="Open Display", description="It will open display settings screen", steps=["Tap Display."])
    a3 = make_action(name="Choose Navigation Type", description="It will let you choose navigation type",
                     steps=["Tap Navigation bar.", "Select Swipe gestures."])
    assert "action.not_fragmented" in codes(validate_envelope(make_envelope([a1, a2, a3]), test_registry))


# ----------------------------------------------------------------- JSON / fallback
def test_malformed_json_rejected():
    for bad in ['{"query": "x",', '```json\n{"a": 1}\n```', '{"score": NaN}', "Here is your plan: {}"]:
        obj, err = parse_jsonl_line(bad)
        assert obj is None and err


def test_strict_json_accepts_plain_object():
    assert strict_json_loads(json.dumps({"a": 1})) == {"a": 1}


def test_no_match_fallback_structure(test_registry):
    env = make_envelope(None, fallback="no_match")
    assert validate_envelope(env, test_registry).ok
    env = make_envelope(None, fallback="no_siis_context")
    assert validate_envelope(env, test_registry).ok


@pytest.mark.parametrize("fallback", [None, "nothing"])
def test_invalid_fallback_structures(fallback, test_registry):
    env = make_envelope(None, fallback=fallback)
    report = validate_envelope(env, test_registry)
    assert not report.ok


def test_fallback_with_nonempty_contexts_is_invalid(test_registry):
    env = make_envelope([make_action()], fallback="no_match")
    assert "fallback.consistency" in codes(validate_envelope(env, test_registry))


def test_variation_count_and_distinctness(test_registry):
    env = make_envelope([make_action()])
    env["query_variations"] = env["query_variations"][:5]
    assert "variations.count" in codes(validate_envelope(env, test_registry))
    env = make_envelope([make_action()])
    env["query_variations"][1] = env["query_variations"][0].upper()
    assert "variations.distinct" in codes(validate_envelope(env, test_registry))


def test_extra_envelope_keys_rejected(test_registry):
    env = make_envelope([make_action()])
    env["debug"] = {"prompt": "..."}
    assert not validate_envelope(env, test_registry).schema_valid
