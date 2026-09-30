#!/usr/bin/env python3
"""Calibrate the semantic-cache similarity threshold on held-out data (no LLM calls).

Calibration split: 2 paraphrases per canonical query + unrelated + borderline negatives.
Decision rule for a threshold t: serve the most similar *concept-compatible* KB plan if its
similarity >= t. Chosen t = midpoint of the widest range that maximises hit rate subject to
precision >= 0.99 (wrong-plan and negative false hits both count against precision).
The test split is then scored once with the chosen threshold.
"""

from __future__ import annotations

import argparse
import json

import _common
from app.core.config import Settings
from app.core.container import build_components


def best_compatible(comps, text: str):
    """Exactly the production decision path (gating + penalties), minus the threshold."""
    intent = comps.enricher.analyze(text)
    vec = comps.embedder.embed([intent.normalized_query])[0]
    rec, sim, _, _, _ = comps.cache.best_candidate(intent, vec, None)
    return sim, (rec.source_id if rec is not None else None), bool(intent.features)


def _thr(t: float, has_concept: bool, t_nc: float) -> float:
    return t if has_concept else max(t, t_nc)


def evaluate(rows_pos, rows_neg, t: float, t_nc: float = 0.93) -> dict:
    tp = sum(1 for s, ok, hc in rows_pos if s >= _thr(t, hc, t_nc) and ok)
    wrong = sum(1 for s, ok, hc in rows_pos if s >= _thr(t, hc, t_nc) and not ok)
    fn = sum(1 for s, ok, hc in rows_pos if s < _thr(t, hc, t_nc))
    fneg = sum(1 for s, hc in rows_neg if s >= _thr(t, hc, t_nc))
    served = tp + wrong + fneg
    return {
        "threshold": round(t, 3), "hit_rate": round(tp / len(rows_pos), 4) if rows_pos else 0.0,
        "wrong_plan": wrong, "negative_false_hits": fneg, "misses": fn,
        "precision": round(tp / served, 4) if served else 1.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-precision", type=float, default=0.99)
    args = ap.parse_args()
    s = Settings(llm_provider="none")
    comps = build_components(s)
    ev = s.resolved_data_dir / "eval"
    para = json.loads((ev / "paraphrases.json").read_text())
    neg = json.loads((ev / "negatives.json").read_text())
    doc_of = {q.id: comps.links.get(q.id) for q in comps.queries}

    def score_split(items):
        out = []
        for it in items:
            sim, src, hc = best_compatible(comps, it["text"])
            out.append((sim, src == doc_of.get(it["query_id"]), hc))
        return out

    calib_pos = score_split(para["calibration"])
    test_pos = score_split(para["test"])
    negs = [(r[0], r[2]) for r in (best_compatible(comps, t) for t in neg["unrelated"] + neg["borderline"])]
    calib_neg, test_neg = negs[::2], negs[1::2]  # disjoint halves

    grid = [round(0.50 + i * 0.01, 2) for i in range(49)]
    curve = [evaluate(calib_pos, calib_neg, t) for t in grid]
    ok = [c for c in curve if c["precision"] >= args.min_precision]
    best_hit = max((c["hit_rate"] for c in ok), default=0.0)
    plateau = [c["threshold"] for c in ok if c["hit_rate"] == best_hit]
    chosen = round((min(plateau) + max(plateau)) / 2, 2) if plateau else 0.9
    report = {
        "dataset": s.dataset_label,
        "embedding": comps.embedder.signature,
        "plans_in_cache": len(comps.cache),
        "rule": f"max hit rate s.t. precision >= {args.min_precision}; midpoint of plateau",
        "chosen_threshold": chosen,
        "calibration_at_chosen": evaluate(calib_pos, calib_neg, chosen),
        "test_at_chosen": evaluate(test_pos, test_neg, chosen),
        "sizes": {"calibration_pos": len(calib_pos), "calibration_neg": len(calib_neg),
                  "test_pos": len(test_pos), "test_neg": len(test_neg)},
        "curve": curve,
    }
    _common.write_json(s.artifacts_dir / "reports" / "cache_calibration.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "curve"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
