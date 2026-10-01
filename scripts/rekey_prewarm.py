#!/usr/bin/env python3
"""Re-key a pre-warmed plan file after a change to query enrichment (no model call).

The plan payloads (validated responses) are kept as they are. For each record, the intent, the cache keys
and the plan id are recomputed from the stored query and its query_variations with the *current* enricher,
exactly as `TroubleshootingService._write_cache` does, and each record goes through `PlanCache.put`, which
re-validates the payload and stamps the current catalog and pipeline versions. Records that no longer
validate are dropped and reported.

    python scripts/rekey_prewarm.py --data-dir data/dev_fixtures
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import _common  # noqa: F401  (sys.path)
from app.cache.models import CacheKey, CacheRecord, make_plan_id
from app.cache.semantic import compatibility, intent_summary
from app.core.config import Settings
from app.core.container import build_components


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--path", default=None, help="plan file (default: the dataset's pre-warm path)")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        s = Settings(data_dir=Path(args.data_dir), llm_provider="none", cache_path=Path(tmp) / "c.sqlite",
                     cache_prewarm_path=Path(tmp) / "none.jsonl")
        path = Path(args.path) if args.path else Settings(data_dir=Path(args.data_dir)).resolved_prewarm_path
        comps = build_components(s, import_prewarm=False)
        comps.cache.clear()
        enricher = comps.enricher
        kept = dropped = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            old = CacheRecord.from_json(json.loads(line))
            intent = enricher.analyze(old.query, _allow_split=False)
            plan_intent = intent_summary(intent)
            variations = list(old.payload.get("query_variations") or [])
            keys = [CacheKey(old.query, intent.normalized_query, "query")]
            if intent.symptoms:
                keys.append(CacheKey(intent.canonical_query, intent.canonical_query, "canonical"))
            keys += [CacheKey(v, enricher.normalize_query(v), "variation") for v in variations]

            def admit(k: CacheKey, _pi=plan_intent) -> bool:
                return compatibility(intent_summary(enricher.analyze(k.text)), _pi)[0]

            rec = CacheRecord(
                plan_id=make_plan_id(old.origin, old.source_fp, intent.normalized_query),
                origin=old.origin, source_id=old.source_id, source_fp=old.source_fp, query=old.query,
                normalized_query=intent.normalized_query, intent=plan_intent, payload=old.payload, model=old.model,
                versions={}, keys=keys, provenance={**(old.provenance or {}), "rekeyed_from": old.versions},
            )
            if comps.cache.put(rec, admit_keys=admit):
                kept += 1
            else:
                dropped += 1
                print(json.dumps({"dropped": old.query}))
        n = comps.cache.export_jsonl(path)
    print(json.dumps({"path": str(path), "rekeyed": kept, "dropped": dropped, "exported": n}))
    return 0 if not dropped else 1


if __name__ == "__main__":
    raise SystemExit(main())
