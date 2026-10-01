"""Business-rule gate: every contractual rule of PDF §4 checked by code (not by prompting)."""

from __future__ import annotations

import math
import re
from typing import Any, Optional

import schema
from app.core import constants as C
from app.services import category_rules
from app.validation import text_rules as T
from app.validation.deeplink_validator import CatalogLookup, validate_actionable, validate_validation
from app.validation.report import WARNING, ValidationReport
from app.validation.url_safety import find_urls, scan_payload

GOAL_RE = re.compile(
    r"^Follow these steps to perform this (?P<topic>[A-Za-z0-9][A-Za-z0-9 \-&'/+.]*?) (?P<kind>Troubleshooting|Configuration)$"
)
_TOPIC_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-&'/+.]*$")
_ROOT_NAV = re.compile(
    r"(?i)^(?:open|launch|go\s+to|navigate\s+to(?:\s+and\s+open)?|head\s+to|start)\s+(?:the\s+|your\s+)?"
    r"(?:settings|[\w\- ]{1,30}\s+app)\b"
)
_NAV_ONLY = re.compile(r"(?i)^(?:open|launch|go\s+to|navigate\s+to(?:\s+and\s+open)?|tap(?:\s+on)?|touch|select|head\s+to)\s+")
_BUNDLED_NAME = re.compile(r"(?i)\s(?:and|&|plus)\s+(?P<verb>[A-Za-z\-]+)\b|,\s*(?P<verb2>[A-Za-z\-]+)\b")


def parse_goal(goal: str) -> Optional[tuple[str, str]]:
    m = GOAL_RE.match(goal or "")
    if not m:
        return None
    return m.group("topic"), m.group("kind")


def check_goal(goal: str, path: str, report: ValidationReport) -> None:
    parsed = parse_goal(goal)
    if not report.check("goal.syntax", parsed is not None, path, f"goal does not match required syntax: {goal!r}"):
        return
    topic, _ = parsed  # type: ignore[misc]
    n = T.count_words(topic)
    report.check(
        "goal.topic_words",
        C.TOPIC_MIN_WORDS <= n <= C.TOPIC_MAX_WORDS and all(_TOPIC_TOKEN.match(t) for t in topic.split()),
        path,
        f"topic must be {C.TOPIC_MIN_WORDS}-{C.TOPIC_MAX_WORDS} plain words: {topic!r}",
    )
    report.check("goal.topic_title_case", T.is_title_case(topic), path, f"topic must be Title Case: {topic!r}")


def check_title(title: str, path: str, report: ValidationReport) -> None:
    n = T.count_words(title)
    report.check(
        "title.word_count",
        C.TITLE_MIN_WORDS <= n <= C.TITLE_MAX_WORDS,
        path,
        f"title must have {C.TITLE_MIN_WORDS}-{C.TITLE_MAX_WORDS} words, got {n}: {title!r}",
    )
    report.check("title.sentence_case", T.is_sentence_case(title), path, f"title must be sentence case: {title!r}")
    report.check(
        "title.punctuation",
        bool(title) and title.strip() == title and title[-1:] not in ".!?:;,",
        path,
        f"title must not end with punctuation or carry padding: {title!r}",
    )


def check_score(score: Any, path: str, report: ValidationReport) -> None:
    ok = (
        isinstance(score, float)
        and not isinstance(score, bool)
        and math.isfinite(score)
        and C.SCORE_MIN <= score <= C.SCORE_MAX
    )
    report.check("score.range", ok, path, f"score must be a float in [0.0, 1.0], got {score!r}")


def check_action_name(name: str, path: str, report: ValidationReport) -> None:
    n = T.count_words(name)
    report.check(
        "action.name_words",
        C.ACTION_NAME_MIN_WORDS <= n <= C.ACTION_NAME_MAX_WORDS,
        path,
        f"actionName must have {C.ACTION_NAME_MIN_WORDS}-{C.ACTION_NAME_MAX_WORDS} words: {name!r}",
    )
    report.check("action.name_title_case", T.is_title_case(name), path, f"actionName must be Title Case: {name!r}")
    bundled = False
    for m in _BUNDLED_NAME.finditer(name or ""):
        verb = (m.group("verb") or m.group("verb2") or "").lower()
        if verb in T.IMPERATIVE_VERBS:
            bundled = True
    report.check(
        "action.name_single_feature",
        not bundled,
        path,
        f"actionName bundles several operations (one action = one screen/feature): {name!r}",
    )


def check_description(desc: str, path: str, report: ValidationReport) -> None:
    report.check(
        "description.prefix",
        (desc or "").startswith(C.DESCRIPTION_PREFIX + " "),
        path,
        f"description must start with 'It will': {desc!r}",
    )
    n = T.count_words(desc)
    report.check(
        "description.word_count",
        C.DESCRIPTION_MIN_WORDS <= n <= C.DESCRIPTION_MAX_WORDS,
        path,
        f"description must have {C.DESCRIPTION_MIN_WORDS}-{C.DESCRIPTION_MAX_WORDS} words, got {n}: {desc!r}",
    )


def check_steps(steps: list[str], path: str, report: ValidationReport) -> None:
    if not report.check("steps.non_empty", bool(steps), path, "step group has no steps"):
        return
    prev = None
    for i, step in enumerate(steps):
        p = f"{path}[{i}]"
        if not report.check("step.non_empty", isinstance(step, str) and bool(step.strip()), p, "empty step"):
            continue
        ok, reason = T.is_imperative(step)
        report.check("step.imperative", ok, p, f"step is not an imperative UI instruction ({reason}): {step!r}")
        n = T.count_interactions(step)
        report.check("step.single_interaction", n == 1, p, f"step bundles {n} interactions: {step!r}")
        report.check(
            "step.length", T.count_words(step) <= C.STEP_MAX_WORDS, p, f"step longer than {C.STEP_MAX_WORDS} words"
        )
        report.check(
            "step.no_duplicate",
            prev is None or step.strip().lower() != prev,
            p,
            f"duplicate consecutive step: {step!r}",
        )
        prev = step.strip().lower()


def _category_value(action: schema.Action) -> Optional[str]:
    c = action.category
    return getattr(c, "value", c)


def _all_steps(action: schema.Action) -> list[str]:
    return [s for g in action.stepGroups for s in g.steps]


def check_action(action: schema.Action, path: str, report: ValidationReport, catalog: CatalogLookup) -> None:
    check_action_name(action.actionName, f"{path}.actionName", report)
    check_description(action.description, f"{path}.description", report)
    category = _category_value(action)
    report.check(
        "category.valid",
        category in C.CATEGORIES,
        f"{path}.category",
        f"category must be one of {C.CATEGORIES}, got {category!r}",
    )
    if not report.check("action.step_groups", bool(action.stepGroups), f"{path}.stepGroups", "action has no step groups"):
        return
    for gi, group in enumerate(action.stepGroups):
        gp = f"{path}.stepGroups[{gi}]"
        check_steps(group.steps, f"{gp}.steps", report)
        if group.actionableDeeplink is not None:
            validate_actionable(group.actionableDeeplink, category, catalog, f"{gp}.actionableDeeplink", report)
        if group.validationDeeplink is not None:
            validate_validation(group.validationDeeplink, catalog, f"{gp}.validationDeeplink", report)
    steps = _all_steps(action)
    roots = sum(1 for s in steps if _ROOT_NAV.match(T.core_clause(s)))
    report.check(
        "action.single_screen",
        roots <= 1,
        path,
        f"action navigates from a root screen {roots} times (bundles several screens)",
    )


def check_goal_actions(goal: schema.Goal, path: str, report: ValidationReport) -> None:
    actions = goal.actions
    names = [a.actionName.strip().lower() for a in actions]
    report.check(
        "action.unique_names",
        len(names) == len(set(names)),
        f"{path}.actions",
        "duplicate actionName inside a goal (fragmented action)",
    )
    uris = [
        g.actionableDeeplink.deeplink
        for a in actions
        for g in a.stepGroups
        if g.actionableDeeplink is not None and not C.is_dummy_uri(g.actionableDeeplink.deeplink)
    ]
    report.check(
        "action.unique_targets",
        len(uris) == len(set(uris)),
        f"{path}.actions",
        "two actions target the same catalog screen (should be one action)",
    )
    # Over-granular split: a navigation-only stub followed by an action that continues from it.
    for i in range(len(actions) - 1):
        cur, nxt = _all_steps(actions[i]), _all_steps(actions[i + 1])
        stub = 0 < len(cur) <= 2 and all(_NAV_ONLY.match(T.core_clause(s)) for s in cur)
        continues = bool(nxt) and not _ROOT_NAV.match(T.core_clause(nxt[0]))
        report.check(
            "action.not_fragmented",
            not (stub and continues),
            f"{path}.actions[{i}]",
            "navigation-only action continued by the next action (one screen split into several actions)",
        )
    # Critical actions form the suffix, ordered by disruption.
    seen_critical = False
    last_rank = -1
    for i, a in enumerate(actions):
        cat = _category_value(a)
        if cat == C.CATEGORY_CRITICAL:
            seen_critical = True
            rank = category_rules.order_key(cat, a.actionName, _all_steps(a))[1]
            report.check(
                "ordering.critical_rank",
                rank >= last_rank,
                f"{path}.actions[{i}]",
                "critical actions must go from least to most disruptive",
            )
            last_rank = max(last_rank, rank)
        else:
            report.check(
                "ordering.critical_last",
                not seen_critical,
                f"{path}.actions[{i}]",
                "non-critical action placed after a critical action",
            )


def check_url_free(value: str, path: str, report: ValidationReport) -> None:
    hits = find_urls(value or "")
    report.check("url.leak", not hits, path, f"URL found: {[h.match for h in hits]}")


def validate_plan(resp: schema.ContextDeeplinkResponse, catalog: CatalogLookup) -> ValidationReport:
    report = ValidationReport()
    for gi, goal in enumerate(resp.contexts):
        gp = f"$.response.contexts[{gi}]"
        check_goal(goal.goal, f"{gp}.goal", report)
        check_title(goal.title, f"{gp}.title", report)
        check_score(goal.score, f"{gp}.score", report)
        if not report.check("goal.has_actions", bool(goal.actions), f"{gp}.actions", "goal has no actions"):
            continue
        for ai, action in enumerate(goal.actions):
            check_action(action, f"{gp}.actions[{ai}]", report, catalog)
        check_goal_actions(goal, gp, report)
    leaks = scan_payload(resp.model_dump(mode="json"), path="$.response")
    report.check("url.leak", not leaks, "$.response", f"URL leaks: {[(l.path, l.match) for l in leaks][:5]}")
    return report


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", (s or "").lower())).strip()


def check_variations(query: str, variations: Any, report: ValidationReport) -> None:
    ok_type = isinstance(variations, list) and all(isinstance(v, str) for v in variations)
    if not report.check("variations.type", ok_type, "$.query_variations", "query_variations must be a list of strings"):
        return
    report.check(
        "variations.count",
        C.VARIATIONS_MIN <= len(variations) <= C.VARIATIONS_MAX,
        "$.query_variations",
        f"expected {C.VARIATIONS_MIN}-{C.VARIATIONS_MAX} variations, got {len(variations)}",
    )
    normed = [_norm(v) for v in variations]
    report.check("variations.non_empty", all(normed), "$.query_variations", "empty variation")
    report.check("variations.distinct", len(set(normed)) == len(normed), "$.query_variations", "duplicate variations")
    report.check(
        "variations.not_query",
        _norm(query) not in normed,
        "$.query_variations",
        "a variation repeats the original query",
        severity=WARNING,
    )


def validate_envelope(payload: Any, catalog: CatalogLookup) -> ValidationReport:
    """Full gate for one API response / results.jsonl line (already JSON-decoded)."""
    from app.api.schemas import TroubleshootResponse  # local import avoids a cycle

    report = ValidationReport()
    if not isinstance(payload, dict):
        report.schema_valid = False
        report.add("schema.envelope", "$", "response must be a JSON object")
        return report
    try:
        env = TroubleshootResponse.model_validate(payload)
    except Exception as exc:  # pydantic.ValidationError
        report.schema_valid = False
        report.add("schema.envelope", "$", f"envelope failed schema validation: {str(exc)[:300]}")
        leaks = scan_payload(payload)
        report.check("url.leak", not leaks, "$", f"URL leaks: {[(l.path, l.match) for l in leaks][:5]}")
        return report
    report.check("envelope.query", bool(env.query.strip()), "$.query", "query must be a non-empty string")
    # Pydantic's lax mode turns JSON true into 1.0 — check the raw JSON type as well.
    raw_contexts = (payload.get("response") or {}).get("contexts") or []
    for gi, raw_goal in enumerate(raw_contexts):
        raw_score = raw_goal.get("score") if isinstance(raw_goal, dict) else None
        report.check(
            "score.type",
            isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool),
            f"$.response.contexts[{gi}].score",
            f"score must be a JSON number, got {type(raw_score).__name__}",
        )
    check_variations(env.query, payload.get("query_variations"), report)
    contexts = env.response.contexts
    fb = env.meta.fallback
    if contexts:
        report.check("fallback.consistency", fb is None, "$.meta.fallback", "fallback set on a non-empty plan")
    else:
        report.check(
            "fallback.consistency",
            fb in C.FALLBACK_CODES,
            "$.meta.fallback",
            "empty contexts require meta.fallback of no_match or no_siis_context",
        )
    report.merge(validate_plan(env.response, catalog))
    leaks = scan_payload({k: v for k, v in payload.items() if k != "response"})
    report.check("url.leak", not leaks, "$", f"URL leaks: {[(l.path, l.match) for l in leaks][:5]}")
    return report
