#!/usr/bin/env python3
"""Architectural ablation of the deeplink-mapping stage (PDF Appendix C §5) → artifacts/reports/ablation.json.

Part 1 — end to end, paired (the Appendix C table). For every query of queries.json (with its SIIS text, cache
disabled) the full pipeline runs ONCE for real with the production mapper; the model's extraction and variations
are recorded. The pipeline is then replayed with each mapper on exactly that extraction:
  baseline_llm      Full LLM deeplink mapping: the model reads the whole catalog (URI + metadata) and names a
                    URI; only byte-identical catalog URIs are accepted, anything else is counted as fabricated
  variant_a_hybrid  Hybrid BM25 + dense retrieval + deterministic exact-screen resolver (production default)
  variant_b_rules   Pure rules: label/keyword matching over the whole catalog (no BM25, no embeddings)
Only the mapper differs between variants, so accuracy differences are caused by mapping alone. End-to-end latency
of a variant = the measured model phase of the real run + the measured replay of everything else with that
mapper (including the baseline's own mapping calls). Cost = the recorded model usage (extraction + variations)
+ the variant's mapping calls, billed at the serving model's rate.

Part 2 — mapping only (paired). The labelled gold actions, identical input for every mapper, are mapped by
llm / hybrid / bm25 / dense / rules. This isolates mapping quality from extraction entirely.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import time
from collections import Counter, defaultdict

import _common
from bench_lib import percentiles
from app.catalog.loaders import link_queries_to_siis, parse_queries, parse_siis, read_json
from app.core.config import Settings
from app.core.constants import is_dummy_uri
from app.core.container import build_components
from app.evaluation.scoring import aggregate, score_plan
from app.models.internal import ExtractedAction, TokenUsage
from app.services.deeplink_mapping import DeeplinkMapper
from app.services.troubleshooting import ServiceError
from app.validation.business_rules import validate_envelope

E2E_VARIANTS = {"baseline_llm": "llm", "variant_a_hybrid": "hybrid", "variant_b_rules": "rules"}
MAPPING_MODES = ("llm", "hybrid", "bm25", "dense", "rules")


class Memo:
    """Records the model's extraction and variations on the real run; replays them for the other mappers."""

    def __init__(self, service):
        self.real_extract = service.extractor.extract
        self.real_variations = service._llm_variations
        self.ext: dict = {}
        self.var: dict = {}
        self.replay = False

    async def extract(self, query, intent, siis_text, *, source_id=None):
        key = (query, siis_text)
        if self.replay and key in self.ext:
            return self.ext[key]
        res = await self.real_extract(query, intent, siis_text, source_id=source_id)
        self.ext[key] = res
        return res

    async def variations(self, query):
        if self.replay:  # a real-run timeout (templates used) replays as "no model variations"
            return self.var.get(query) or ([], TokenUsage(), 0.0, None)
        out = await self.real_variations(query)
        self.var[query] = out
        return out


def mapping_ms(tel: dict) -> float:
    st = tel.get("stages", {})
    return float(st.get("deeplink_retrieval", 0.0)) + float(st.get("deeplink_reranking", 0.0))


async def run(args) -> dict:
    _common.load_env_file(args.env_file)
    s = Settings(cache_backend="memory", cache_read_enabled=False, cache_write_enabled=False)
    comps = build_components(s, import_prewarm=False)
    data = s.resolved_data_dir
    gold = json.loads((data / "eval" / "gold.json").read_text())["queries"]
    queries, _ = parse_queries(read_json(data / "queries.json"))
    if args.limit:
        queries = queries[: args.limit]
        gold = {k: v for k, v in list(gold.items())[: args.limit]}
    docs, _ = parse_siis(read_json(data / "siis_responses.json"))
    links, _ = link_queries_to_siis(queries, docs)
    doc_text = {d.id: d.text for d in docs}
    resolver, registry, llm = comps.resolver, comps.registry, comps.llm
    mapping_model = args.mapping_model or s.llm_model
    mappers = {m: DeeplinkMapper(resolver, registry, mode=m, llm=llm, llm_model=mapping_model) for m in MAPPING_MODES}
    out: dict = {"started_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 "dataset": s.dataset_label, "llm_provider": s.llm_provider, "llm_model": s.llm_model, "mapping_model": mapping_model, "llm_fallbacks": s.fallback_models,
                 "embedding_model": f"{s.embedding_provider}:{s.embedding_model}", "catalog_entries": len(registry.entries),
                 "latency_scope": "in-process service latency (no HTTP), cache disabled, sequential requests; "
                                  "variant latency = measured model phase of the real run + measured replay"}

    # ------------------------------------------------------------------ part 1: end to end (paired)
    service = comps.service
    memo = Memo(service)
    service.extractor.extract = memo.extract  # instance attributes shadow the bound methods
    service._llm_variations = memo.variations
    rows: dict[str, list[dict]] = defaultdict(list)
    real_rows: list[dict] = []
    names = list(E2E_VARIANTS)
    if not args.skip_e2e:
        for q in queries:
            memo.replay = False
            service.mapper = mappers["hybrid"]
            try:
                real = await service.troubleshoot(q.text, doc_text[links[q.id]], request_id=f"abl-real-{q.id}",
                                                  read_cache=False, write_cache=False)
            except ServiceError as exc:
                real_rows.append({"query_id": q.id, "status": exc.status, "error": exc.code})
                print(json.dumps(real_rows[-1]), flush=True)
                await asyncio.sleep(args.pace)
                continue
            model_ms = float(real.telemetry.get("stages", {}).get("llm_inference", 0.0))
            real_rows.append({"query_id": q.id, "status": real.status_code, "ms": real.telemetry.get("latency_ms"),
                              "model_phase_ms": round(model_ms, 2), "cost_usd": real.telemetry.get("cost_usd"),
                              "models": real.telemetry.get("models_used"),
                              "extraction_modes": real.telemetry.get("extraction_modes")})
            memo.replay = True
            for name in names:
                service.mapper = mappers[E2E_VARIANTS[name]]
                try:
                    r = await service.troubleshoot(q.text, doc_text[links[q.id]], request_id=f"abl-{name}-{q.id}",
                                                   read_cache=False, write_cache=False)
                    status, body, tel = r.status_code, r.body, r.telemetry
                except ServiceError as exc:
                    status, body, tel = exc.status, None, {"error": exc.code}
                replay_ms = float(tel.get("latency_ms") or 0.0) - float(tel.get("stages", {}).get("llm_inference", 0.0))
                row = {"query_id": q.id, "status": status, "ms": round(model_ms + replay_ms, 2),
                       "replay_ms": round(replay_ms, 3), "mapping_ms": round(mapping_ms(tel), 3),
                       "cost_usd": tel.get("cost_usd"), "model_calls": tel.get("model_calls"),
                       "mapping_llm_calls": tel.get("mapping_llm_calls", 0),
                       "fabricated_uris": tel.get("fabricated_uris", []), "error": tel.get("error")}
                if body is not None:
                    row["contract_ok"] = validate_envelope(body, registry).ok
                    if q.id in gold:
                        sc = score_plan(q.id, body["response"], gold[q.id])
                        row.update(step_accuracy=round(sc.step_accuracy, 4), deeplink_scores=sc.deeplink_scores,
                                   deeplink_outcomes=sc.deeplink_outcomes, false_links=sc.false_links)
                        row["_score"] = sc
                rows[name].append(row)
                print(json.dumps({"variant": name, **{k: v for k, v in row.items()
                                                      if k not in ("_score", "deeplink_scores")}}), flush=True)
            await asyncio.sleep(args.pace)
        service.mapper = mappers["hybrid"]
    e2e = {}
    for name in names:
        rs = rows.get(name, [])
        ok = [r for r in rs if r["status"] == 200]
        scores = [r["_score"] for r in ok if "_score" in r]
        costs = [r["cost_usd"] for r in ok]
        e2e[name] = {
            "mapper": E2E_VARIANTS[name], "requests": len(rs), "ok": len(ok),
            "errors": dict(Counter(r.get("error") or r["status"] for r in rs if r["status"] != 200)),
            "accuracy": aggregate(scores) if scores else None,
            "latency_ms": percentiles([r["ms"] for r in ok]),
            "replay_ms": percentiles([r["replay_ms"] for r in ok]),
            "mapping_stage_ms": percentiles([r["mapping_ms"] for r in ok]),
            "cost_usd_per_query": round(sum(costs) / len(costs), 8) if costs and None not in costs else None,
            "model_calls_per_query": round(sum(r["model_calls"] or 0 for r in ok) / len(ok), 3) if ok else None,
            "mapping_llm_calls_per_query": round(sum(r["mapping_llm_calls"] for r in ok) / len(ok), 3) if ok else None,
            "fabricated_uris": sum(len(r["fabricated_uris"]) for r in ok),
            "contract_ok_pct": round(100 * sum(1 for r in ok if r.get("contract_ok")) / len(ok), 2) if ok else None,
        }
        for r in rs:
            r.pop("_score", None)
    real_ok = [r for r in real_rows if r.get("status") == 200]
    out["end_to_end"] = e2e
    out["end_to_end_real_runs"] = {
        "requests": len(real_rows), "ok": len(real_ok),
        "latency_ms": percentiles([r["ms"] for r in real_ok]),
        "model_phase_ms": percentiles([r["model_phase_ms"] for r in real_ok]),
        "rules_fallback_runs": sum(1 for r in real_ok if "rules" in (r.get("extraction_modes") or [])),
        "models": dict(Counter(m for r in real_ok for m in (r.get("models") or []))),
    }
    out["end_to_end_rows"] = rows
    out["end_to_end_real_rows"] = real_rows

    # ------------------------------------------------------------------ part 2: mapping only
    items = []
    for qid, g in gold.items():
        for ga in g["actions"]:
            items.append((qid, g["domain"], ga))
    per_mode: dict[str, dict] = {}
    for mode in MAPPING_MODES:
        m = mappers[mode]
        outcomes, lat, costs, fabricated, calls = Counter(), [], [], [], 0
        for qid, domain, ga in items:
            action = ExtractedAction(ga["name"], ga["desc"], ga["category"], list(ga["steps"]))
            o = await m.map(action, domain=domain)
            lat.append(o.retrieval_ms)
            if o.usage.calls:
                calls += o.usage.calls
                costs.append(o.cost_usd)
            if o.fabricated:
                fabricated.append(o.fabricated)
            got = o.uri
            target = ga.get("target_uri")
            if target is None:
                outcomes["abstain_ok" if got is None or is_dummy_uri(got) else "false_link"] += 1
            elif got in {target, *ga.get("alt_uris", [])}:
                outcomes["exact"] += 1
            elif got in set(ga.get("parent_uris", [])):
                outcomes["parent"] += 1
            elif got is None:
                outcomes["missing"] += 1
            else:
                outcomes["wrong"] += 1
            if got is not None and not is_dummy_uri(got) and registry.get_entry(got) is None:
                raise AssertionError(f"non-catalog URI emitted by {mode}: {got}")  # must never happen
            if mode == "llm":
                await asyncio.sleep(args.pace_mapping)
        targets = sum(outcomes[k] for k in ("exact", "parent", "missing", "wrong"))
        per_mode[mode] = {
            "actions": len(items), "targets": targets, "outcomes": dict(outcomes),
            "deeplink_relevance": round((2 * outcomes["exact"] + outcomes["parent"]) / targets, 3) if targets else None,
            "exact_pct": round(100 * outcomes["exact"] / targets, 2) if targets else None,
            "abstain_accuracy_pct": (round(100 * outcomes["abstain_ok"] / (outcomes["abstain_ok"] + outcomes["false_link"]), 2)
                                     if outcomes["abstain_ok"] + outcomes["false_link"] else None),
            "latency_ms_per_action": percentiles(lat),
            "llm_calls": calls,
            "cost_usd_per_action": round(sum(costs) / len(items), 8) if costs and None not in costs else (0.0 if not calls else None),
            "fabricated_uris": len(fabricated), "fabricated_examples": fabricated[:5],
        }
        print(json.dumps({"mode": mode, **{k: v for k, v in per_mode[mode].items() if k != "fabricated_examples"}}),
              flush=True)
    out["mapping_only"] = per_mode
    out["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    await llm.aclose()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--pace", type=float, default=12.0, help="seconds between queries (provider RPM limits)")
    ap.add_argument("--pace-mapping", type=float, default=4.0)
    ap.add_argument("--mapping-model", default="", help="model for the full-LLM mapping baseline (default: LLM_MODEL)")
    ap.add_argument("--skip-e2e", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="only the first N queries / gold queries (smoke tests)")
    ap.add_argument("--out", default="ablation.json")
    args = ap.parse_args()
    out = asyncio.run(run(args))
    s = Settings()
    _common.write_json(s.artifacts_dir / "reports" / args.out, out)
    print(json.dumps({"end_to_end": {k: {kk: vv for kk, vv in v.items() if kk in ("accuracy", "latency_ms", "cost_usd_per_query",
                                                                                   "fabricated_uris")}
                                     for k, v in out["end_to_end"].items()},
                      "mapping_only": {k: {kk: vv for kk, vv in v.items() if kk in ("deeplink_relevance", "outcomes")}
                                       for k, v in out["mapping_only"].items()}}, indent=1)[:5000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
