"""Accuracy scoring (PDF Appendix C §2).

Step accuracy (0.0–3.0) = completeness + correctness + ordering, each in [0, 1]:
  * completeness — share of gold steps matched by a predicted step (token F1 ≥ 0.6, on/off kept)
  * correctness  — share of predicted steps that match a gold step (penalises invented steps)
  * ordering     — pairwise order agreement of matched actions (Kendall-style), 1.0 if < 2 matched
Deeplink relevance (0.0–2.0), per gold action that has a Settings target:
  2 = exact target screen (gold URI or an equally exact alternative), 1 = parent menu of the target,
  0 = wrong screen, missing link, or fabricated URI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.constants import is_dummy_uri
from app.retrieval.text import overlap_f1, token_set

STEP_MATCH_F1 = 0.6
ACTION_MATCH_F1 = 0.3


def _steps_of(action: dict[str, Any]) -> list[str]:
    return [s for g in action.get("stepGroups", []) for s in g.get("steps", [])]


def _uri_of(action: dict[str, Any]) -> Optional[str]:
    for g in action.get("stepGroups", []):
        dl = g.get("actionableDeeplink")
        if isinstance(dl, dict) and dl.get("deeplink"):
            return dl["deeplink"]
    return None


def _step_tokens(step: str) -> frozenset[str]:
    return token_set(step)


def _match_steps(pred: list[str], gold: list[str]) -> tuple[int, int]:
    """(gold steps matched, predicted steps matched) with one-to-one greedy matching."""
    pt = [_step_tokens(s) for s in pred]
    gt = [_step_tokens(s) for s in gold]
    pairs = sorted(
        ((overlap_f1(p, g), i, j) for i, p in enumerate(pt) for j, g in enumerate(gt)),
        key=lambda t: (-t[0], t[1], t[2]),
    )
    used_p, used_g = set(), set()
    for f1, i, j in pairs:
        if f1 < STEP_MATCH_F1:
            break
        if i in used_p or j in used_g:
            continue
        used_p.add(i)
        used_g.add(j)
    return len(used_g), len(used_p)


def match_actions(pred: list[dict[str, Any]], gold: list[dict[str, Any]]) -> dict[int, int]:
    """gold index -> predicted index (greedy on step-set F1 + name overlap)."""
    sims = []
    for j, g in enumerate(gold):
        gt = token_set(" ".join(g["steps"])) | token_set(g["name"])
        for i, p in enumerate(pred):
            pt = token_set(" ".join(_steps_of(p))) | token_set(p.get("actionName", ""))
            sims.append((overlap_f1(pt, gt), j, i))
    sims.sort(key=lambda t: (-t[0], t[1], t[2]))
    out: dict[int, int] = {}
    used = set()
    for f1, j, i in sims:
        if f1 < ACTION_MATCH_F1:
            break
        if j in out or i in used:
            continue
        out[j] = i
        used.add(i)
    return out


@dataclass
class QueryScore:
    query_id: str
    completeness: float
    correctness: float
    ordering: float
    deeplink_scores: list[float] = field(default_factory=list)
    deeplink_outcomes: list[str] = field(default_factory=list)
    false_links: int = 0
    abstained_ok: int = 0
    pred_actions: int = 0
    gold_actions: int = 0

    @property
    def step_accuracy(self) -> float:
        return self.completeness + self.correctness + self.ordering

    @property
    def deeplink_relevance(self) -> Optional[float]:
        return sum(self.deeplink_scores) / len(self.deeplink_scores) if self.deeplink_scores else None


def score_plan(query_id: str, response: dict[str, Any], gold: dict[str, Any]) -> QueryScore:
    contexts = (response or {}).get("contexts") or []
    pred = [a for g in contexts for a in g.get("actions", [])]
    gold_actions = gold["actions"]
    all_pred_steps = [s for a in pred for s in _steps_of(a)]
    all_gold_steps = [s for a in gold_actions for s in a["steps"]]
    g_matched, p_matched = _match_steps(all_pred_steps, all_gold_steps)
    completeness = g_matched / len(all_gold_steps) if all_gold_steps else 1.0
    correctness = p_matched / len(all_pred_steps) if all_pred_steps else 0.0
    amap = match_actions(pred, gold_actions)
    matched = sorted(amap.items())  # gold order
    if len(matched) < 2:
        ordering = 1.0 if pred else 0.0
    else:
        conc = total = 0
        for x in range(len(matched)):
            for y in range(x + 1, len(matched)):
                total += 1
                conc += 1 if matched[x][1] < matched[y][1] else 0
        ordering = conc / total
    qs = QueryScore(query_id, completeness, correctness, ordering, pred_actions=len(pred), gold_actions=len(gold_actions))
    for j, ga in enumerate(gold_actions):
        target = ga.get("target_uri")
        pi = amap.get(j)
        puri = _uri_of(pred[pi]) if pi is not None else None
        if target is None:
            if pi is not None and puri is not None and not is_dummy_uri(puri):
                qs.false_links += 1
            elif pi is not None:
                qs.abstained_ok += 1
            continue
        if pi is None:
            qs.deeplink_scores.append(0.0)
            qs.deeplink_outcomes.append("unmatched_action")
        elif puri in {target, *ga.get("alt_uris", [])}:
            qs.deeplink_scores.append(2.0)
            qs.deeplink_outcomes.append("exact")
        elif puri in set(ga.get("parent_uris", [])):
            qs.deeplink_scores.append(1.0)
            qs.deeplink_outcomes.append("parent")
        elif puri is None:
            qs.deeplink_scores.append(0.0)
            qs.deeplink_outcomes.append("missing")
        else:
            qs.deeplink_scores.append(0.0)
            qs.deeplink_outcomes.append("wrong")
    return qs


def aggregate(scores: list[QueryScore]) -> dict[str, Any]:
    n = len(scores) or 1
    dl = [s for q in scores for s in q.deeplink_scores]
    outcomes: dict[str, int] = {}
    for q in scores:
        for o in q.deeplink_outcomes:
            outcomes[o] = outcomes.get(o, 0) + 1
    return {
        "queries": len(scores),
        "step_accuracy": round(sum(q.step_accuracy for q in scores) / n, 3),
        "completeness": round(sum(q.completeness for q in scores) / n, 3),
        "correctness": round(sum(q.correctness for q in scores) / n, 3),
        "ordering": round(sum(q.ordering for q in scores) / n, 3),
        "deeplink_relevance": round(sum(dl) / len(dl), 3) if dl else None,
        "deeplink_targets": len(dl),
        "deeplink_outcomes": outcomes,
        "false_links": sum(q.false_links for q in scores),
    }
