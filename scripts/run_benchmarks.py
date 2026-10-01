#!/usr/bin/env python3
"""Run the evaluation suite over real HTTP and write results.jsonl + artifacts/reports/benchmark.json.

  Server A (cache disabled)          → cold full-pipeline requests (every query, `--cold-rounds` times)
                                       and the reference samples
  Server B (pre-warmed, no writes)   → exact-query hits, unseen paraphrases, negatives, edge cases

results.jsonl holds one response body per query of queries.json (cold round 1, the full pipeline);
artifacts/reports/results_all.jsonl holds every 200 body of every path, indexed by results_index.jsonl.
Every number in metrics.md is rendered from the JSON written here (scripts/render_metrics.py).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import time
from collections import Counter, defaultdict
from pathlib import Path

import httpx

import _common
from bench_lib import Call, Server, dump_jsonl, percentiles, post
from app.catalog.loaders import link_queries_to_siis, load_catalog, load_samples, parse_queries, parse_siis, read_json
from app.catalog.registry import CatalogRegistry
from app.core.config import Settings
from app.core.constants import DUMMY_POSITIVE_URI
from app.evaluation.scoring import aggregate, score_plan
from app.validation.business_rules import validate_envelope
from app.validation.report import ValidationReport
from app.validation.url_safety import scan_payload

ROOT = _common.ROOT
RULE_FIELDS = ("goal.", "title.", "description.")


def environment() -> dict:
    env = {"python": platform.python_version(), "platform": platform.platform(), "vcpus": os.cpu_count()}
    try:
        kb = next(int(l.split()[1]) for l in Path("/proc/meminfo").read_text().splitlines() if l.startswith("MemTotal"))
        env["ram_gb"] = round(kb / 1024 / 1024, 1)
    except Exception:
        env["ram_gb"] = None
    try:
        rel = dict(l.split("=", 1) for l in Path("/etc/os-release").read_text().splitlines() if "=" in l)
        env["os"] = rel.get("PRETTY_NAME", "").strip('"')
    except Exception:
        env["os"] = platform.system()
    return env


def compliance(bodies: list[dict], registry: CatalogRegistry) -> dict:
    lines = len(bodies)
    schema_ok = rules_all_ok = 0
    field_checks = defaultdict(lambda: [0, 0])
    url_leaks = 0
    uris = catalog_ok = 0
    auto = auto_catalog = auto_dummy = 0
    manual_with_link = 0
    critical_not_last = 0
    violations = Counter()
    for b in bodies:
        rep: ValidationReport = validate_envelope(b, registry)
        schema_ok += 1 if rep.schema_valid else 0
        rules_all_ok += 1 if rep.ok else 0
        for rule, (n, p) in rep.checks.items():
            field_checks[rule][0] += n
            field_checks[rule][1] += p
        for v in rep.errors:
            violations[v.code] += 1
        url_leaks += len(scan_payload(b))
        for goal in (b.get("response") or {}).get("contexts", []):
            seen_critical = False
            for a in goal.get("actions", []):
                cat = a.get("category")
                if cat == "critical":
                    seen_critical = True
                elif seen_critical:
                    critical_not_last += 1
                for g in a.get("stepGroups", []):
                    dl = g.get("actionableDeeplink")
                    link = dl.get("deeplink") if isinstance(dl, dict) else None
                    if cat == "manual" and link:
                        manual_with_link += 1
                    for d in (dl, g.get("validationDeeplink")):
                        if isinstance(d, dict) and d.get("deeplink"):
                            uris += 1
                            u = d["deeplink"]
                            valid = (u == DUMMY_POSITIVE_URI and d is dl) or registry.get_entry(u) is not None \
                                or registry.get_validation_rule(u) is not None
                            catalog_ok += 1 if valid else 0
                if cat == "auto":
                    auto += 1
                    links = [g.get("actionableDeeplink") for g in a.get("stepGroups", []) if g.get("actionableDeeplink")]
                    if any(registry.get_entry(d["deeplink"]) for d in links):
                        auto_catalog += 1
                    elif any(d["deeplink"] == DUMMY_POSITIVE_URI for d in links):
                        auto_dummy += 1
    syntax_rules = {k: v for k, v in field_checks.items() if k.startswith(RULE_FIELDS)}
    n_syntax = sum(v[0] for v in syntax_rules.values())
    p_syntax = sum(v[1] for v in syntax_rules.values())
    n_all = sum(v[0] for v in field_checks.values())
    p_all = sum(v[1] for v in field_checks.values())
    pct = lambda a, b: round(100 * a / b, 2) if b else None  # noqa: E731
    return {
        "lines": lines,
        "schema_valid_lines": schema_ok,
        "schema_valid_pct": pct(schema_ok, lines),
        "rule_compliance_goal_title_description_pct": pct(p_syntax, n_syntax),
        "rule_checks_goal_title_description": n_syntax,
        "rule_compliance_all_rules_pct": pct(p_all, n_all),
        "rule_checks_all": n_all,
        "lines_fully_compliant": rules_all_ok,
        "lines_fully_compliant_pct": pct(rules_all_ok, lines),
        "absolute_url_leaks": url_leaks,
        "deeplinks_emitted": uris,
        "deeplinks_catalog_valid": catalog_ok,
        "deeplink_catalog_validity_pct": pct(catalog_ok, uris),
        "auto_actions": auto,
        "auto_with_catalog_deeplink": auto_catalog,
        "auto_with_dummy_positive": auto_dummy,
        "auto_with_valid_actionable_pct": pct(auto_catalog + auto_dummy, auto),
        "auto_with_catalog_actionable_pct": pct(auto_catalog, auto),
        "manual_actions_with_actionable_deeplink": manual_with_link,
        "critical_order_violations": critical_not_last,
        "violations_by_rule": dict(violations.most_common()),
        "checks_by_rule": {k: {"checks": v[0], "passed": v[1]} for k, v in sorted(field_checks.items())},
    }


def structure(response: dict) -> list[tuple]:
    """Plan skeleton used for the determinism check: goal/title + (actionName, category, deeplink)."""
    out = []
    for g in (response or {}).get("contexts", []):
        out.append(("goal", g.get("goal"), g.get("title")))
        for a in g.get("actions", []):
            uri = next((sg["actionableDeeplink"]["deeplink"] for sg in a.get("stepGroups", [])
                        if isinstance(sg.get("actionableDeeplink"), dict)), None)
            out.append((a.get("actionName"), a.get("category"), uri))
    return out


def gold_from_sample(contexts: list[dict]) -> dict:
    actions = []
    for g in contexts:
        for a in g.get("actions", []):
            uri = next((sg["actionableDeeplink"]["deeplink"] for sg in a.get("stepGroups", [])
                        if isinstance(sg.get("actionableDeeplink"), dict)), None)
            actions.append({"name": a.get("actionName", ""), "steps": [s for sg in a.get("stepGroups", [])
                                                                       for s in sg.get("steps", [])],
                            "target_uri": uri, "alt_uris": [], "parent_uris": []})
    return {"actions": actions}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--pace-cold", type=float, default=8.0, help="seconds between cold requests (provider RPM limits)")
    ap.add_argument("--pace-miss", type=float, default=6.0, help="pause after a cache MISS on the warm server")
    ap.add_argument("--cold-rounds", type=int, default=1)
    ap.add_argument("--determinism-subset", type=int, default=8,
                    help="queries (spread over the domains) sent a second time for the determinism check")
    ap.add_argument("--server-b-model", choices=("on", "off"), default="off",
                    help="off: the warm server runs without the model, so cache misses cost no quota "
                         "(hit rates and hit latency do not depend on the model)")
    ap.add_argument("--exact-rounds", type=int, default=2)
    ap.add_argument("--port-a", type=int, default=8011)
    ap.add_argument("--port-b", type=int, default=8012)
    ap.add_argument("--skip-cold", action="store_true")
    args = ap.parse_args()
    _common.load_env_file(args.env_file)
    s = Settings()
    data = s.resolved_data_dir
    ev = data / "eval"
    entries, issues, fp = load_catalog(data / "deeplinks.json")
    registry = CatalogRegistry(entries, fingerprint=fp, issues=issues)
    queries, _ = parse_queries(read_json(data / "queries.json"))
    docs, _ = parse_siis(read_json(data / "siis_responses.json"))
    links, _ = link_queries_to_siis(queries, docs)
    samples, _ = load_samples(data / "samples")
    doc_text = {d.id: d.text for d in docs}
    gold = json.loads((ev / "gold.json").read_text())["queries"] if (ev / "gold.json").exists() else {}
    para_manual = json.loads((ev / "paraphrases.json").read_text())["test"] if (ev / "paraphrases.json").exists() else []
    # Primary held-out set = the newest round (generated after the last change to enrichment/cache logic);
    # older rounds are still measured and reported, flagged as seen while fixing.
    rounds_available = sorted(ev.glob("paraphrases_llm_heldout*.json"))
    docs_by_round = {}
    for pth in rounds_available:
        d = json.loads(pth.read_text())
        docs_by_round[int(d.get("round", 1))] = d
    primary_round = max(docs_by_round) if docs_by_round else None
    para_llm_doc = docs_by_round.get(primary_round, {})
    para_llm = para_llm_doc.get("items", [])
    older = {r: d for r, d in docs_by_round.items() if r != primary_round}
    negatives = json.loads((ev / "negatives.json").read_text()) if (ev / "negatives.json").exists() else {}
    edges = json.loads((ev / "edge_cases.json").read_text())["cases"] if (ev / "edge_cases.json").exists() else []
    reports = s.artifacts_dir / "reports"
    started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    all_bodies: list[dict] = []
    index: list[dict] = []

    def keep(call: Call, **meta):
        index.append({"line": len(all_bodies) if call.status == 200 else None, **call.telemetry(), **meta})
        if call.status == 200 and isinstance(call.body, dict):
            all_bodies.append(call.body)

    client = httpx.Client(timeout=120.0, trust_env=False)
    out: dict = {"started_at": started, "dataset": s.dataset_label, "llm_provider": s.llm_provider,
                 "llm_model": s.llm_model, "llm_enrichment_model": s.enrichment_model,
                 "llm_fallbacks": s.fallback_models, "llm_thinking_level": s.llm_thinking_level,
                 "llm_hedge_after_s": s.llm_hedge_after_s,
                 "embedding_model": f"{s.embedding_provider}:{s.embedding_model}",
                 "semantic_threshold": s.semantic_cache_threshold,
                 "semantic_threshold_no_concept": s.semantic_cache_threshold_no_concept,
                 "environment": environment(),
                 "config": {"cold_rounds": args.cold_rounds, "exact_rounds": args.exact_rounds,
                            "pace_cold_s": args.pace_cold, "pace_after_miss_s": args.pace_miss,
                            "sequential_requests": True}}

    # ------------------------------------------------------------------ cold path
    cold_calls: list[Call] = []
    sample_calls: list[tuple] = []
    edge_calls: list[Call] = []
    if not args.skip_cold:
        a = Server(args.port_a, {"CACHE_READ_ENABLED": "false", "CACHE_WRITE_ENABLED": "false",
                                 "CACHE_BACKEND": "memory", "LOG_LEVEL": "WARNING"}).start()
        out["server_a_startup_s"] = round(a.startup_s, 2)
        step = max(1, len(queries) // max(1, args.determinism_subset))
        det_ids = {q.id for q in queries[::step][: args.determinism_subset]}
        plan = [(rnd, q) for rnd in range(1, args.cold_rounds + 1) for q in queries]
        if args.cold_rounds == 1 and args.determinism_subset:
            plan += [(2, q) for q in queries if q.id in det_ids]
        for rnd, q in plan:
            c = post(client, a.url, q.text, doc_text[links[q.id]], path="cold", query_id=q.id, round=rnd)
            cold_calls.append(c)
            keep(c, text=q.text)
            print(json.dumps({"path": "cold", "round": rnd, "id": q.id, "status": c.status, "ms": round(c.client_ms),
                              "model": (c.body or {}).get("meta", {}).get("model") if isinstance(c.body, dict) else None}),
                  flush=True)
            time.sleep(args.pace_cold)
        by_input = {(c.query, doc_text[links[c.tags["query_id"]]]): c for c in cold_calls if c.tags["round"] == 1}
        for sm in samples:
            reuse = by_input.get((sm.query, sm.siis_response))
            if reuse is not None:  # identical request already made on this server: reuse its response
                sample_calls.append((sm, reuse))
                continue
            c = post(client, a.url, sm.query, sm.siis_response, path="sample", sample_id=sm.id)
            sample_calls.append((sm, c))
            keep(c, text=sm.query)
            print(json.dumps({"path": "sample", "id": sm.id, "status": c.status, "ms": round(c.client_ms)}), flush=True)
            time.sleep(args.pace_cold)
        for e in edges:  # edge cases need the model path: cache-disabled server A
            c = post(client, a.url, e["query"], e.get("siis"), path="edge", edge_id=e["id"], kind=e["kind"])
            edge_calls.append(c)
            keep(c, text=e["query"][:120])
            print(json.dumps({"path": "edge", "id": e["id"], "status": c.status, "ms": round(c.client_ms)}), flush=True)
            if int(c.headers.get("x-model-calls", 0) or 0) > 0:
                time.sleep(args.pace_cold)
        out["server_a_rss_mb"] = a.rss_mb()
        a.stop()

    # ------------------------------------------------------------ warm server B
    bench_cache = s.artifacts_dir / "tmp" / "bench_cache.sqlite"
    for p in bench_cache.parent.glob("bench_cache.sqlite*"):
        p.unlink()
    env_b = {"CACHE_PATH": str(bench_cache), "CACHE_WRITE_ENABLED": "false", "LOG_LEVEL": "WARNING"}
    if args.server_b_model == "off":
        env_b.update({"LLM_PROVIDER": "none", "HEALTH_REQUIRE_LLM": "false"})
    out["server_b_model"] = args.server_b_model
    b = Server(args.port_b, env_b).start()
    out["server_b_startup_s"] = round(b.startup_s, 2)
    out["server_b_health"] = client.get(f"{b.url}/health/details").json()
    exact_calls, para_calls, manual_calls, neg_calls = [], [], [], []

    def post_b(query, siis=None, **tags):
        c = post(client, b.url, query, siis, **tags)
        if int(c.headers.get("x-model-calls", 0) or 0) > 0:
            time.sleep(args.pace_miss)  # the model was called: keep under the provider's per-minute limit
        return c

    for rnd in range(1, args.exact_rounds + 1):
        for q in queries:  # exact hits (no siis → KB-grounded pre-warmed plans)
            c = post_b(q.text, path="exact", query_id=q.id, round=rnd)
            exact_calls.append(c)
            keep(c, text=q.text)
    for it in para_llm:  # fresh LLM-generated unseen paraphrases (primary round)
        c = post_b(it["text"], path="paraphrase_llm", query_id=it["query_id"], style=it.get("style"))
        para_calls.append(c)
        keep(c, text=it["text"])
    older_calls: dict[int, list] = {}
    for r, d in sorted(older.items()):
        for it in d.get("items", []):
            c = post_b(it["text"], path=f"paraphrase_llm_r{r}", query_id=it["query_id"], style=it.get("style"))
            older_calls.setdefault(r, []).append(c)
            keep(c, text=it["text"])
    for it in para_manual:  # hand-written test split (seen during design; reported separately)
        c = post_b(it["text"], path="paraphrase_manual", query_id=it["query_id"])
        manual_calls.append(c)
        keep(c, text=it["text"])
    for kind in ("unrelated", "borderline"):
        for t in negatives.get(kind, []):
            c = post_b(t, path=f"negative_{kind}")
            neg_calls.append(c)
            keep(c, text=t)
    out["server_b_rss_mb"] = b.rss_mb()
    out["server_b_cpu_s"] = b.cpu_seconds()
    b.stop()
    client.close()

    # ------------------------------------------------------------------ analysis
    def lat(calls, *, only_cache=None):
        sel = [c for c in calls if c.status == 200 and (only_cache is None or c.cache.startswith(only_cache))]
        return {"client_ms": percentiles([c.client_ms for c in sel]),
                "server_ms": percentiles([c.server_ms for c in sel if c.server_ms is not None])}

    def cache_mix(calls):
        return dict(Counter(c.cache or f"HTTP_{c.status}" for c in calls))

    exact_ref = {}
    for c in exact_calls:
        if c.status == 200 and c.tags.get("query_id") not in exact_ref:
            exact_ref[c.tags["query_id"]] = c.body["response"]

    def hit_correct(c: Call) -> bool:
        """A hit is correct only if it serves the very plan pre-warmed for the paraphrased query."""
        if not c.cache.startswith("HIT") or not isinstance(c.body, dict):
            return False
        ref = exact_ref.get(c.tags.get("query_id"))
        return ref is not None and bool(ref.get("contexts")) and ref == c.body["response"]

    def paraphrase_stats(calls):
        n = len(calls)
        hits = [c for c in calls if c.cache.startswith("HIT")]
        correct = [c for c in hits if hit_correct(c)]
        ok_ids = {id(c) for c in correct}
        by_style = defaultdict(lambda: [0, 0])
        for c in calls:
            st = c.tags.get("style") or "manual"
            by_style[st][0] += 1
            by_style[st][1] += 1 if id(c) in ok_ids else 0
        return {"n": n, "hits": len(hits), "hit_rate_pct": round(100 * len(hits) / n, 2) if n else None,
                "correct_plan_hits": len(correct),
                "correct_hit_rate_pct": round(100 * len(correct) / n, 2) if n else None,
                "wrong_plan_hits": len(hits) - len(correct), "cache_mix": cache_mix(calls),
                "by_style": {k: {"n": v[0], "correct_hits": v[1]} for k, v in sorted(by_style.items())},
                "misses": [c.query for c in calls if not c.cache.startswith("HIT")][:40],
                "wrong_hits": [c.query for c in hits if id(c) not in ok_ids][:40],
                "latency_hits": lat(calls, only_cache="HIT")}

    # accuracy on the cold full pipeline (per round) and on the pre-warmed plans
    rounds = sorted({c.tags["round"] for c in cold_calls})
    out["accuracy_cold_rounds"] = {}
    for rnd in rounds:
        sc = [score_plan(c.tags["query_id"], c.body["response"], gold[c.tags["query_id"]])
              for c in cold_calls if c.tags["round"] == rnd and c.status == 200 and c.tags["query_id"] in gold]
        out["accuracy_cold_rounds"][str(rnd)] = aggregate(sc) if sc else None
    # accuracy: round 1 = every query exactly once (round 2 is only the determinism subset)
    scores = [score_plan(c.tags["query_id"], c.body["response"], gold[c.tags["query_id"]])
              for c in cold_calls if c.status == 200 and c.tags["query_id"] in gold and c.tags["round"] == 1]
    per_domain = defaultdict(list)
    for sc in scores:
        per_domain[gold[sc.query_id]["domain"]].append(sc)
    out["accuracy_cold"] = aggregate(scores) if scores else None
    out["accuracy_cold_by_domain"] = {d: aggregate(v) for d, v in sorted(per_domain.items())}
    first = {}
    for sc in scores:
        first.setdefault(sc.query_id, sc)
    out["accuracy_cold_by_query_round1"] = {
        q: {"step_accuracy": round(v.step_accuracy, 3), "deeplink_relevance": v.deeplink_relevance,
            "deeplink_outcomes": v.deeplink_outcomes, "false_links": v.false_links} for q, v in first.items()}
    ex_scores = [score_plan(qid, resp, gold[qid]) for qid, resp in exact_ref.items() if qid in gold and resp["contexts"]]
    out["accuracy_prewarmed_plans"] = aggregate(ex_scores) if ex_scores else None

    # determinism: identical complaint + SIIS, cache disabled, two independent cold runs
    by_q = defaultdict(dict)
    for c in cold_calls:
        if c.status == 200:
            by_q[c.tags["query_id"]][c.tags["round"]] = c.body
    pairs = [(v[1], v[2]) for v in by_q.values() if 1 in v and 2 in v]
    out["determinism"] = {
        "pairs": len(pairs),
        "identical_response": sum(1 for x, y in pairs if x["response"] == y["response"]),
        "identical_plan_structure": sum(1 for x, y in pairs if structure(x["response"]) == structure(y["response"])),
        "identical_deeplinks": sum(1 for x, y in pairs if [t[2] for t in structure(x["response"])]
                                   == [t[2] for t in structure(y["response"])]),
        "identical_variations": sum(1 for x, y in pairs if x["query_variations"] == y["query_variations"]),
    }

    # reference samples: pipeline output vs the expected sample output
    sample_rows = []
    for sm, c in sample_calls:
        exp_ctx = sm.expected_contexts
        row = {"id": sm.id, "status": c.status}
        if c.status == 200:
            got = c.body["response"]
            sc = score_plan(sm.id, got, gold_from_sample(exp_ctx))
            exp_names = [a["actionName"] for g in exp_ctx for a in g["actions"]]
            got_names = [a["actionName"] for g in got["contexts"] for a in g["actions"]]
            exp_uris = [t[2] for t in structure({"contexts": exp_ctx}) if t[0] != "goal"]
            got_uris = [t[2] for t in structure(got) if t[0] != "goal"]
            row.update(step_accuracy=round(sc.step_accuracy, 3), deeplink_exact=sc.deeplink_outcomes.count("exact"),
                       deeplink_targets=len(sc.deeplink_scores), expected_actions=exp_names, got_actions=got_names,
                       same_uri_sequence=exp_uris == got_uris,
                       goal_equal=[g["goal"] for g in exp_ctx] == [g["goal"] for g in got["contexts"]],
                       title_equal=[g["title"] for g in exp_ctx] == [g["title"] for g in got["contexts"]])
        sample_rows.append(row)
    out["samples"] = sample_rows

    cold_ok = [c for c in cold_calls if c.status == 200]
    out["latency"] = {
        "cold": lat(cold_calls),
        "cold_by_round": {str(r): lat([c for c in cold_calls if c.tags["round"] == r]) for r in rounds},
        "exact_hit": lat(exact_calls, only_cache="HIT-EXACT"),
        "semantic_hit_llm_paraphrases": lat(para_calls, only_cache="HIT-SEMANTIC"),
        "semantic_hit_manual_paraphrases": lat(manual_calls, only_cache="HIT-SEMANTIC"),
        "all_hits": lat(exact_calls + para_calls + manual_calls, only_cache="HIT"),
    }
    tele = [c.telemetry() for c in cold_ok]
    priced = all(t["cost_usd"] is not None for t in tele)
    out["cold_path"] = {
        "requests": len(cold_calls), "ok": len(cold_ok),
        "errors": dict(Counter(f"{c.status}:{(c.body or {}).get('error', {}).get('code') if isinstance(c.body, dict) else c.error}"
                               for c in cold_calls if c.status != 200)),
        "model_calls_per_query": round(sum(t["model_calls"] for t in tele) / len(tele), 3) if tele else None,
        "input_tokens_per_query": round(sum(t["tokens_in"] for t in tele) / len(tele), 1) if tele else None,
        "output_tokens_per_query": round(sum(t["tokens_out"] for t in tele) / len(tele), 1) if tele else None,
        "cost_usd_per_query": round(sum(t["cost_usd"] for t in tele) / len(tele), 8) if tele and priced else None,
        "cost_usd_p95": percentiles([t["cost_usd"] for t in tele], digits=8)["p95"] if tele and priced else None,
        "cost_usd_total": round(sum(t["cost_usd"] for t in tele), 6) if tele and priced else None,
        "models": dict(Counter(c.body["meta"]["model"] for c in cold_ok)),
        "over_8s": sum(1 for c in cold_ok if c.client_ms > 8000),
    }
    hits_all = [c for c in exact_calls + para_calls + manual_calls + [x for v in older_calls.values() for x in v]
                if c.cache.startswith("HIT")]
    out["cache"] = {
        "exact": {"n": len(exact_calls), "cache_mix": cache_mix(exact_calls)},
        "paraphrase_llm": {**paraphrase_stats(para_calls), "round": primary_round,
                           "generator_model": para_llm_doc.get("generator_model"),
                           "generated_at": para_llm_doc.get("generated_at"),
                           "dropped_as_seen": para_llm_doc.get("dropped_as_seen")},
        "paraphrase_llm_older_rounds": {
            str(r): {**paraphrase_stats(calls), "generator_model": older[r].get("generator_model"),
                     "note": "seen while fixing the enrichment lexicon; not an unbiased estimate"}
            for r, calls in older_calls.items()},
        "paraphrase_manual": paraphrase_stats(manual_calls),
        "hits_total": len(hits_all),
        "hit_cost_usd_values": sorted({c.telemetry()["cost_usd"] for c in hits_all}),
        "hit_model_calls_values": sorted({c.telemetry()["model_calls"] for c in hits_all}),
    }
    out["negatives"] = {
        "n": len(neg_calls),
        "cache_hits": sum(1 for c in neg_calls if c.cache.startswith("HIT")),
        "false_hits": [c.query for c in neg_calls if c.cache.startswith("HIT")],
        "no_siis_context": sum(1 for c in neg_calls if isinstance(c.body, dict)
                               and c.body.get("meta", {}).get("fallback") == "no_siis_context"),
        "kb_grounded_plans": [c.query for c in neg_calls if c.cache == "MISS" and isinstance(c.body, dict)
                              and (c.body.get("response") or {}).get("contexts")],
        "cache_mix": cache_mix(neg_calls),
    }
    edge_out = []
    for e, c in zip(edges, edge_calls):
        body = c.body if isinstance(c.body, dict) else {}
        fb = body.get("meta", {}).get("fallback")
        contexts = (body.get("response") or {}).get("contexts") or []
        observed = f"http_{c.status}" if c.status != 200 else (fb or ("plan" if contexts else "empty"))
        exp = e["expect"]
        edge_out.append({"id": e["id"], "kind": e["kind"], "expected": exp, "observed": observed,
                         "pass": exp == "any" or observed == exp, "cache": c.cache,
                         "url_leaks": len(scan_payload(body)) if body else 0,
                         "contract_ok": validate_envelope(body, registry).ok if c.status == 200 else None,
                         "actions": [a["actionName"] for g in contexts for a in g["actions"]][:6],
                         "error_code": body.get("error", {}).get("code") if isinstance(body.get("error"), dict) else None,
                         "client_ms": round(c.client_ms, 1)})
    out["edge_cases"] = edge_out

    # results.jsonl: one line per query of queries.json (cold round 1; round 2 if round 1 failed)
    results, missing = [], []
    for q in queries:
        body = by_q.get(q.id, {}).get(1) or by_q.get(q.id, {}).get(2)
        if body is None:
            missing.append(q.id)
        else:
            results.append(body)
    out["results_jsonl"] = {"lines": len(results), "queries": len(queries), "missing": missing}
    out["compliance"] = compliance(results, registry)
    out["compliance_all_paths"] = compliance(all_bodies, registry)
    out["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    dump_jsonl(ROOT / "results.jsonl", results)
    dump_jsonl(reports / "results_all.jsonl", all_bodies)
    dump_jsonl(reports / "results_index.jsonl", index)
    _common.write_json(reports / "benchmark.json", out)
    brief = {k: out[k] for k in ("latency", "accuracy_cold", "determinism", "cold_path") if k in out}
    brief["compliance"] = {k: v for k, v in out["compliance"].items() if k not in ("checks_by_rule",)}
    brief["cache_llm"] = {k: v for k, v in out["cache"]["paraphrase_llm"].items() if k not in ("misses", "wrong_hits")}
    print(json.dumps(brief, indent=1)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
