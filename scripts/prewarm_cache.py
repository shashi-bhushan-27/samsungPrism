#!/usr/bin/env python3
"""Pre-warm the semantic cache: run the full cold pipeline once per queries.json entry, grounded on
its linked SIIS document, store the validated plans and export them to prewarm_plans.jsonl.

Requires a working LLM (GEMINI_API_KEY). Paces requests to respect provider rate limits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import _common
from app.core.config import Settings
from app.core.container import build_components


async def main_async(args) -> int:
    _common.load_env_file(args.env_file)
    s = Settings()
    comps = build_components(s, import_prewarm=False)
    if args.fresh:
        comps.cache.clear()
    docs = {d.id: d for d in comps.siis_docs}
    report = []
    for q in comps.queries:
        doc = docs.get(comps.links.get(q.id, ""))
        if doc is None:
            report.append({"id": q.id, "status": "no_linked_siis"})
            continue
        intent = comps.enricher.analyze(q.text)
        if comps.cache.lookup_exact(intent, None) is not None and not args.fresh:
            report.append({"id": q.id, "status": "already_cached"})
            continue
        for attempt in range(1 + args.retries):
            t0 = time.perf_counter()
            try:
                r = await comps.service.troubleshoot(q.text, None, kb_doc=doc, read_cache=False, write_cache=True)
            except Exception as exc:
                status, extra = "error", {"error": type(exc).__name__}
            else:
                modes = r.telemetry.get("extraction_modes") or []
                written = r.telemetry.get("cache_written") or []
                status = "cached" if written else ("degraded_not_cached" if "rules" in modes else "not_cached")
                extra = {"modes": modes, "actions": len(r.telemetry.get("actions", [])),
                         "tokens": r.telemetry.get("tokens"), "cost_usd": r.telemetry.get("cost_usd"),
                         "fallback": r.telemetry.get("fallback")}
            ms = round((time.perf_counter() - t0) * 1000)
            print(json.dumps({"id": q.id, "attempt": attempt, "status": status, "ms": ms, **extra}), flush=True)
            await asyncio.sleep(args.pace)
            if status == "cached":
                break
        report.append({"id": q.id, "status": status, "ms": ms, **extra})
    n = comps.cache.export_jsonl(s.resolved_prewarm_path)
    summary = {"exported_plans": n, "path": str(s.resolved_prewarm_path),
               "statuses": {k: sum(1 for r in report if r["status"] == k) for k in {r["status"] for r in report}}}
    print(json.dumps(summary, indent=2))
    _common.write_json(s.artifacts_dir / "reports" / "prewarm_report.json", {"summary": summary, "items": report})
    await comps.llm.aclose()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--pace", type=float, default=2.0, help="seconds between requests (rate limits)")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--fresh", action="store_true")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
