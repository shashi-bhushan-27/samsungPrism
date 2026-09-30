#!/usr/bin/env python3
"""Render metrics.md (PDF Appendix C template) from the measured JSON reports — no hand-typed numbers.

Inputs (artifacts/reports/): benchmark.json (run_benchmarks.py), ablation.json (run_ablation.py),
stress.json (stress_test.py), cache_calibration.json (calibrate_cache.py), prewarm_report.json.
A missing input renders as "not measured", never as a number.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import _common

ROOT = _common.ROOT
REPORTS = ROOT / "artifacts" / "reports"
NM = "not measured"


def load(name: str) -> Optional[dict]:
    p = REPORTS / name
    return json.loads(p.read_text()) if p.exists() else None


def pct(v: Optional[float]) -> str:
    return NM if v is None else f"{v:.2f}%"


def ms(v: Optional[float]) -> str:
    return NM if v is None else f"{v:,.1f}"


def usd(v: Optional[float], digits: int = 6) -> str:
    return NM if v is None else f"${v:.{digits}f}"


def num(v: Optional[float], digits: int = 3) -> str:
    return NM if v is None else f"{v:.{digits}f}"


def verdict(ok: Optional[bool]) -> str:
    return "" if ok is None else (" ✅" if ok else " ❌")


def render() -> str:
    b = load("benchmark.json") or {}
    ab = load("ablation.json") or {}
    st = load("stress.json") or {}
    cal = load("cache_calibration.json") or {}
    env = b.get("environment", {})
    comp = b.get("compliance", {})
    comp_all = b.get("compliance_all_paths", {})
    acc = b.get("accuracy_cold") or {}
    lat = b.get("latency", {})
    cold = dict(b.get("cold_path", {}))
    idx_path = REPORTS / "results_index.jsonl"
    if idx_path.exists():  # per-request costs (older benchmark.json rounded the P95 to cents)
        from bench_lib import percentiles

        costs = [r["cost_usd"] for r in map(json.loads, idx_path.read_text().splitlines())
                 if r.get("path") == "cold" and r.get("status") == 200 and r.get("cost_usd") is not None]
        if costs:
            cold["cost_usd_p95"] = percentiles(costs, digits=8)["p95"]
    cache = b.get("cache", {})
    pl = cache.get("paraphrase_llm", {})
    pm = cache.get("paraphrase_manual", {})
    L: list[str] = []
    w = L.append

    # ------------------------------------------------------------------ header
    models = cold.get("models") or {}
    mix = ", ".join(f"{m} ×{n}" for m, n in sorted(models.items(), key=lambda t: -t[1]))
    from app.core.config import Settings

    shipped = Settings.model_fields["llm_model"].default
    w("# System Performance Metrics & Evaluation Report")
    w(f"**Model(s):** gemini/{b.get('llm_model', NM)} (primary during this evaluation; fail-over chain: "
      f"{' → '.join('gemini/' + m for m in b.get('llm_fallbacks', [])) or 'none'}; thinking level "
      f"`{b.get('llm_thinking_level', NM)}`). Models that actually served the cold requests: {mix or NM}."
      + (f" The shipped default primary is gemini/{shipped}: its free-tier daily quota (500 requests) was used up "
         "during development on the evaluation day, so the measurements below were taken with the next model of "
         "the fail-over chain as primary." if shipped != b.get("llm_model") else ""))
    w(f"**Embeddings:** {b.get('embedding_model', NM)} (384-d, local ONNX via fastembed)")
    w(f"**Environment:** {env.get('vcpus', NM)} vCPU / {env.get('ram_gb', NM)} GB RAM / {env.get('os', NM)} "
      f"({env.get('platform', '')}, Python {env.get('python', NM)}), single uvicorn worker")
    w("")
    w(f"> **Dataset: {b.get('dataset', NM)}.** The official starter assets (queries.json, siis_responses.json, "
      "deeplinks.json with ~575 entries, samples/) were not supplied, so every number below was measured on the "
      "synthetic development fixture in `data/dev_fixtures/` (110 catalog entries, 32 queries, 4 domains, 5 samples), "
      "whose gold labels were written by the same author as the SIIS texts and catalog. Accuracy on the official data "
      "is expected to be lower; re-run `make benchmark` after dropping the official files into `data/official/`.")
    w(f"> Measured {b.get('started_at', NM)} → {b.get('finished_at', NM)} (UTC) over real HTTP against a real uvicorn "
      "server and the live Gemini API. Raw data: `artifacts/reports/*.json`, `results.jsonl`, "
      "`artifacts/reports/results_all.jsonl` + `results_index.jsonl`.")
    w("")
    w("---")
    w("")

    # ------------------------------------------------------------------ 1. compliance
    w("## 1. Schema & Rule Compliance")
    w("Evaluated on sample datasets and held-out validation scenarios.")
    w("")
    w("| Metric | Target | Measured Value |")
    w("| :--- | :--- | :--- |")
    sv = comp_all.get("schema_valid_pct")
    w(f"| Schema-valid output lines | >= 99% | {pct(sv)} ({comp_all.get('schema_valid_lines', NM)}/"
      f"{comp_all.get('lines', NM)} lines){verdict(sv is not None and sv >= 99)} |")
    rc = comp_all.get("rule_compliance_goal_title_description_pct")
    w(f"| Rule compliance (Goal / Title / Description syntax) | >= 95% | {pct(rc)} "
      f"({comp_all.get('rule_checks_goal_title_description', NM)} checks){verdict(rc is not None and rc >= 95)} |")
    lk = comp_all.get("absolute_url_leaks")
    w(f"| Absolute URL leaks | 0 | {lk if lk is not None else NM}{verdict(lk == 0 if lk is not None else None)} |")
    dv = comp_all.get("deeplink_catalog_validity_pct")
    w(f"| Deeplink catalog validity (exact URI match) | 100% | {pct(dv)} ({comp_all.get('deeplinks_catalog_valid', NM)}/"
      f"{comp_all.get('deeplinks_emitted', NM)} URIs){verdict(dv == 100.0 if dv is not None else None)} |")
    av = comp_all.get("auto_with_valid_actionable_pct")
    w(f"| Auto actions carrying valid actionable deeplink | >= 90% | {pct(av)} ({comp_all.get('auto_with_catalog_deeplink', NM)} "
      f"catalog + {comp_all.get('auto_with_dummy_positive', NM)} dummy_positive of {comp_all.get('auto_actions', NM)})"
      f"{verdict(av is not None and av >= 90)} |")
    w("")
    w("Scope: every HTTP 200 body produced by the benchmark (cold full pipeline incl. the determinism re-runs, "
      "reference samples, edge cases, exact hits, LLM and hand-written paraphrases, negatives"
      + ("; the warm cache server ran without the model, so its misses were answered by the grounded rules "
         "fallback" if b.get("server_b_model") == "off" else "") + "). The same gates restricted to `results.jsonl` "
      f"(one line per query of queries.json, cold full pipeline): schema-valid {pct(comp.get('schema_valid_pct'))} "
      f"({comp.get('lines', NM)} lines), goal/title/description rules {pct(comp.get('rule_compliance_goal_title_description_pct'))}, "
      f"URL leaks {comp.get('absolute_url_leaks', NM)}, catalog validity {pct(comp.get('deeplink_catalog_validity_pct'))}, "
      f"auto actions with valid actionable deeplink {pct(comp.get('auto_with_valid_actionable_pct'))}.")
    w("")
    w("| Additional gate (all output lines) | Measured |")
    w("| :--- | :--- |")
    w(f"| All business rules (every check, every field) | {pct(comp_all.get('rule_compliance_all_rules_pct'))} of "
      f"{comp_all.get('rule_checks_all', NM)} checks |")
    w(f"| Lines passing every gate | {comp_all.get('lines_fully_compliant', NM)}/{comp_all.get('lines', NM)} |")
    w(f"| Auto actions with a real catalog deeplink (dummy_positive excluded) | {pct(comp_all.get('auto_with_catalog_actionable_pct'))} |")
    w(f"| Manual actions carrying an actionable deeplink | {comp_all.get('manual_actions_with_actionable_deeplink', NM)} |")
    w(f"| Critical actions not placed last | {comp_all.get('critical_order_violations', NM)} |")
    viol = comp_all.get("violations_by_rule") or {}
    w(f"| Violations by rule | {', '.join(f'{k}: {v}' for k, v in viol.items()) or 'none'} |")
    w("")
    w("---")
    w("")

    # ------------------------------------------------------------------ 2. accuracy
    w("## 2. Accuracy Benchmarks")
    w("Evaluated against reference ground truth scenarios across Battery, Display, Camera, and Performance.")
    w("")
    w("| Evaluation Metric | Scale / Anchor | Score |")
    w("| :--- | :--- | :--- |")
    w(f"| Step accuracy (completeness, correctness, ordering) | 0.0 - 3.0 | {num(acc.get('step_accuracy'))} |")
    w(f"| Deeplink relevance (exact target screen vs. parent menu) | 0.0 - 2.0 | {num(acc.get('deeplink_relevance'))} |")
    w("")
    w(f"Cold full pipeline (request SIIS text, cache disabled), {acc.get('queries', NM)} scored responses "
      f"(every query of queries.json once). Step accuracy = completeness "
      f"{num(acc.get('completeness'))} + correctness {num(acc.get('correctness'))} + ordering {num(acc.get('ordering'))}. "
      f"Deeplink relevance over {acc.get('deeplink_targets', NM)} gold targets, outcomes "
      f"{json.dumps(acc.get('deeplink_outcomes', {}))} (2 = exact screen or an equally exact alternative, 1 = parent menu, "
      f"0 = wrong/missing/fabricated); links attached to actions whose gold has no Settings target: "
      f"{acc.get('false_links', NM)}. Scoring code: `app/evaluation/scoring.py`.")
    w("")
    w("| Domain | Responses | Step accuracy | Deeplink relevance | Deeplink outcomes |")
    w("| :--- | :--- | :--- | :--- | :--- |")
    for d, v in (b.get("accuracy_cold_by_domain") or {}).items():
        w(f"| {d} | {v['queries']} | {num(v['step_accuracy'])} | {num(v['deeplink_relevance'])} | "
          f"{', '.join(f'{k} {n}' for k, n in v['deeplink_outcomes'].items())} |")
    w("")
    rounds = b.get("accuracy_cold_rounds") or {}
    pre = b.get("accuracy_prewarmed_plans") or {}
    det = b.get("determinism") or {}
    w("| Consistency check | Measured |")
    w("| :--- | :--- |")
    for r, v in rounds.items():
        if v:
            w(f"| Cold round {r} | step accuracy {num(v['step_accuracy'])}, deeplink relevance {num(v['deeplink_relevance'])} |")
    if pre:
        w(f"| Pre-warmed cache plans (served on hits, KB-grounded) | step accuracy {num(pre.get('step_accuracy'))}, "
          f"deeplink relevance {num(pre.get('deeplink_relevance'))} ({pre.get('queries')} plans) |")
    if det:
        n = det.get("pairs", 0)
        w(f"| Determinism, identical input twice with cache disabled ({n} pairs) | identical plan structure "
          f"(goal, title, action names, categories, deeplinks) {det.get('identical_plan_structure')}/{n}; identical "
          f"deeplink sequence {det.get('identical_deeplinks')}/{n}; byte-identical response {det.get('identical_response')}/{n}; "
          f"identical query_variations {det.get('identical_variations')}/{n} |")
    w("")
    samples = b.get("samples") or []
    if samples:
        w("Reference samples (`samples/`, pipeline output vs the expected output of each sample):")
        w("")
        w("| Sample | Step accuracy vs sample | Exact deeplinks | Same deeplink sequence | Same goal / title | Actions (expected → produced) |")
        w("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for s_ in samples:
            if s_.get("status") != 200:
                w(f"| {s_['id']} | HTTP {s_.get('status')} | | | | |")
                continue
            w(f"| {s_['id']} | {num(s_['step_accuracy'])} | {s_['deeplink_exact']}/{s_['deeplink_targets']} | "
              f"{'yes' if s_['same_uri_sequence'] else 'no'} | {'yes' if s_['goal_equal'] else 'no'} / "
              f"{'yes' if s_['title_equal'] else 'no'} | {'; '.join(s_['expected_actions'])} → {'; '.join(s_['got_actions'])} |")
        w("")
    w("---")
    w("")

    # ------------------------------------------------------------------ 3. latency
    ex = lat.get("exact_hit", {}).get("client_ms", {})
    se = lat.get("semantic_hit_llm_paraphrases", {}).get("client_ms", {})
    co = lat.get("cold", {}).get("client_ms", {})
    w("## 3. Latency Benchmarks (N >= 30 requests per path)")
    w("")
    w("| Execution Path | Target (P95) | P50 (ms) | P95 (ms) |")
    w("| :--- | :--- | :--- | :--- |")
    w(f"| Cache hit - exact query match | <= 300 ms | {ms(ex.get('p50'))} | {ms(ex.get('p95'))}"
      f"{verdict(ex.get('p95') is not None and ex['p95'] <= 300)} |")
    w(f"| Cache hit - unseen semantic paraphrase | <= 300 ms | {ms(se.get('p50'))} | {ms(se.get('p95'))}"
      f"{verdict(se.get('p95') is not None and se['p95'] <= 300)} |")
    w(f"| Cold query - full pipeline extraction & mapping | <= 8000 ms | {ms(co.get('p50'))} | {ms(co.get('p95'))}"
      f"{verdict(co.get('p95') is not None and co['p95'] <= 8000)} |")
    w("")
    w("Client-side wall time per HTTP request (sequential requests, localhost, keep-alive). Details:")
    w("")
    w("| Path | N | P50 | P95 | P99 | Max | Server-side P95 (`meta.latency_ms`) |")
    w("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    for label, key in (("Exact hit", "exact_hit"), ("Semantic hit — unseen LLM paraphrases", "semantic_hit_llm_paraphrases"),
                       ("Semantic hit — hand-written paraphrases", "semantic_hit_manual_paraphrases"),
                       ("Cold full pipeline", "cold")):
        c = lat.get(key, {}).get("client_ms", {})
        sv_ = lat.get(key, {}).get("server_ms", {})
        w(f"| {label} | {c.get('n', 0)} | {ms(c.get('p50'))} | {ms(c.get('p95'))} | {ms(c.get('p99'))} | {ms(c.get('max'))} | "
          f"{ms(sv_.get('p95'))} |")
    w("")
    w(f"Cold requests over 8 s: {cold.get('over_8s', NM)} of {cold.get('ok', NM)}. The cold path is dominated by the "
      "remote model call (extraction, with query variations generated in parallel); its tail follows the Gemini API "
      "(fail-over to the next model on 429/5xx, a hedged duplicate request after "
      f"{b.get('llm_hedge_after_s', NM)} s).")
    if st:
        w("")
        w("Concurrency (stress test, `artifacts/reports/stress.json`, one uvicorn worker):")
        w("")
        w("| Run | Requests | Concurrency | Throughput (req/s) | P50 (ms) | P95 (ms) | P99 (ms) | Unexpected errors | Contract failures |")
        w("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for name, r in (st.get("runs") or {}).items():
            lm = r["latency_ms_200"]
            w(f"| {name} | {r['requests']} | {r['concurrency']} | {r['throughput_rps']} | {ms(lm.get('p50'))} | "
              f"{ms(lm.get('p95'))} | {ms(lm.get('p99'))} | {r['unexpected_errors']} | {r['contract_failures']} |")
        w("")
        w(f"Server start → healthy: {st.get('startup_s', NM)} s; first request after healthy: "
          f"{(st.get('first_request_after_healthy') or {}).get('ms', NM)} ms; RSS idle {st.get('rss_mb_idle', NM)} MB, "
          f"after load {st.get('rss_mb_after_load', NM)} MB.")
    w("")
    w("---")
    w("")

    # ------------------------------------------------------------------ 4. cost & cache
    hit_costs = cache.get("hit_cost_usd_values") or []
    hit_cost = max(hit_costs) if hit_costs else None
    w("## 4. Operational Cost & Cache Efficacy")
    w("")
    w("| Metric Item | Target | Measured Value |")
    w("| :--- | :--- | :--- |")
    w(f"| Cold query average inference cost | Tracked | {usd(cold.get('cost_usd_per_query'))} |")
    w(f"| Cache hit inference cost | $0.00 | {usd(hit_cost, 2)} ({cache.get('hits_total', NM)} hits, model calls on hits: "
      f"{cache.get('hit_model_calls_values', NM)}){verdict(hit_cost == 0.0 if hit_cost is not None else None)} |")
    hr = pl.get("correct_hit_rate_pct")
    w(f"| Semantic cache hit rate (on unseen paraphrases) | >= 80% | {pct(hr)} ({pl.get('correct_plan_hits', NM)}/"
      f"{pl.get('n', NM)} served the correct plan){verdict(hr is not None and hr >= 80)} |")
    w("| Cost derivation method | - | (prompt tokens + completion tokens) x rate |")
    w("")
    w(f"Cold path per query: {cold.get('model_calls_per_query', NM)} model calls, {cold.get('input_tokens_per_query', NM)} "
      f"prompt tokens, {cold.get('output_tokens_per_query', NM)} completion tokens (thinking tokens, when a model emits "
      f"them, are billed at the completion rate); P95 cost {usd(cold.get('cost_usd_p95'))}; total for "
      f"{cold.get('ok', NM)} cold requests {usd(cold.get('cost_usd_total'))}. Rates: Gemini API pricing page retrieved "
      "2026-09-30 (`app/core/config.py: MODEL_PRICING_USD_PER_MTOK`), each call priced at the rate of the model that "
      "actually served it.")
    w("")
    w("| Cache efficacy detail | Measured |")
    w("| :--- | :--- |")
    w(f"| Unseen LLM paraphrases, round {pl.get('round', NM)} (generator {pl.get('generator_model', NM)}, created after "
      f"the last change to enrichment/cache logic) | {pl.get('n', NM)} items; any hit {pct(pl.get('hit_rate_pct'))}; "
      f"correct-plan hits {pct(pl.get('correct_hit_rate_pct'))}; wrong-plan hits {pl.get('wrong_plan_hits', NM)} |")
    for style, v in (pl.get("by_style") or {}).items():
        w(f"| — style `{style}` | {v['correct_hits']}/{v['n']} correct hits |")
    for r, v in (cache.get("paraphrase_llm_older_rounds") or {}).items():
        w(f"| Round {r} LLM paraphrases ({v.get('generator_model')}; {v.get('note')}) | {v['n']} items; correct-plan hits "
          f"{pct(v.get('correct_hit_rate_pct'))}; wrong-plan hits {v.get('wrong_plan_hits')} (pre-fix measurement: "
          "HARDENING_REPORT.md H9) |")
    w(f"| Hand-written test paraphrases (seen while tuning; optimistic) | {pm.get('n', NM)} items; correct-plan hits "
      f"{pct(pm.get('correct_hit_rate_pct'))}; wrong-plan hits {pm.get('wrong_plan_hits', NM)} |")
    neg = b.get("negatives") or {}
    w(f"| Out-of-scope / borderline queries (must not hit) | {neg.get('n', NM)} queries; cache hits {neg.get('cache_hits', NM)}; "
      f"`no_siis_context` {neg.get('no_siis_context', NM)} |")
    if cal:
        ca, te = cal.get("calibration_at_chosen", {}), cal.get("test_at_chosen", {})
        w(f"| Threshold calibration (`scripts/calibrate_cache.py`) | {cal.get('rule', '')}: chosen {cal.get('chosen_threshold')}; "
          f"calibration split hit rate {ca.get('hit_rate')}, test split {te.get('hit_rate')}, wrong plans "
          f"{ca.get('wrong_plan', 0) + te.get('wrong_plan', 0)}, negative false hits "
          f"{ca.get('negative_false_hits', 0) + te.get('negative_false_hits', 0)} |")
    w("")
    w("---")
    w("")

    # ------------------------------------------------------------------ 5. ablation
    e2e = ab.get("end_to_end") or {}
    mo = ab.get("mapping_only") or {}
    w("## 5. Architectural Ablation Analysis")
    w("")
    w("| Architecture Variant | Step Accuracy | Latency (P95) | Cost / Query | Key Observations |")
    w("| :--- | :--- | :--- | :--- | :--- |")
    labels = {"baseline_llm": ("Baseline: Full LLM Deeplink Mapping", "llm"),
              "variant_a_hybrid": ("Variant A: Hybrid BM25 + Dense Embedding Retrieval", "hybrid"),
              "variant_b_rules": ("Variant B: Pure Rules-Based Deeplink Mapping", "rules")}
    for key, (label, mode) in labels.items():
        v = e2e.get(key)
        m = mo.get(mode) or {}
        if not v:
            w(f"| {label} | {NM} | {NM} | {NM} | |")
            continue
        a = v.get("accuracy") or {}
        obs = (f"Deeplink relevance {num(a.get('deeplink_relevance'))} end to end, {num(m.get('deeplink_relevance'))} on "
               f"gold actions (exact {m.get('outcomes', {}).get('exact', 0)}, parent {m.get('outcomes', {}).get('parent', 0)}, "
               f"wrong {m.get('outcomes', {}).get('wrong', 0)}, missing {m.get('outcomes', {}).get('missing', 0)} of "
               f"{m.get('targets', NM)}); mapping stage P95 {ms((v.get('mapping_stage_ms') or {}).get('p95'))} ms; "
               f"{v.get('mapping_llm_calls_per_query', 0)} extra model calls/query; invented URIs rejected: "
               f"{v.get('fabricated_uris', 0) + (m.get('fabricated_uris') or 0)}")
        w(f"| {label} | {num(a.get('step_accuracy'))} | {ms((v.get('latency_ms') or {}).get('p95'))} ms | "
          f"{usd(v.get('cost_usd_per_query'))} | {obs} |")
    w("")
    if e2e:
        w(f"End to end: the full pipeline on all {e2e.get('variant_a_hybrid', {}).get('requests', NM)} queries with their "
          "SIIS text, cache disabled, variants interleaved per query (rotating order) so they share API conditions; only "
          f"the mapper differs. Latency is in-process service time ({ab.get('latency_scope', '')}). The baseline gives the "
          "model the whole catalog (URI + description + message + qna_description) and accepts its answer only if it is "
          "byte-identical to a catalog URI.")
        w("")
    if mo:
        w("Mapping only (paired: the same labelled gold actions go through every mapper):")
        w("")
        w("| Mapper | Deeplink relevance (0–2) | Exact | Parent | Wrong | Missing | Abstain correct | P95 ms / action | Cost / action | Invented URIs |")
        w("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for mode, v in mo.items():
            o = v.get("outcomes", {})
            w(f"| {mode} | {num(v.get('deeplink_relevance'))} | {o.get('exact', 0)} | {o.get('parent', 0)} | {o.get('wrong', 0)} | "
              f"{o.get('missing', 0)} | {pct(v.get('abstain_accuracy_pct'))} | {ms((v.get('latency_ms_per_action') or {}).get('p95'))} | "
              f"{usd(v.get('cost_usd_per_action'))} | {v.get('fabricated_uris', 0)} |")
        w("")
    w("---")
    w("")

    # ------------------------------------------------------------------ 6. limitations
    w("## 6. Known Edge Cases & System Limitations")
    edges = b.get("edge_cases") or []
    if edges:
        passed = sum(1 for e in edges if e["pass"])
        w(f"* Edge-case suite ({passed}/{len(edges)} behaved as specified; URL leaks across the suite: "
          f"{sum(e['url_leaks'] for e in edges)}):")
        w("")
        w("| Case | Kind | Expected | Observed | Result |")
        w("| :--- | :--- | :--- | :--- | :--- |")
        for e in edges:
            w(f"| {e['id']} | {e['kind']} | {e['expected']} | {e['observed']} | {'pass' if e['pass'] else 'FAIL'} |")
        w("")
    for line in limitations(b, ab, st):
        w(f"* {line}")
    w("")
    return "\n".join(L)


def limitations(b: dict, ab: dict, st: dict) -> list[str]:
    out = []
    pl = (b.get("cache") or {}).get("paraphrase_llm") or {}
    misses = pl.get("misses") or []
    if misses:
        out.append(f"**Semantic cache misses on unseen paraphrases ({len(misses)} of {pl.get('n')}).** Missed items fall "
                   "back to the full pipeline (correct, but cold latency and cost). Examples: "
                   + "; ".join(f"“{m}”" for m in misses[:4]) + ". Descriptions written in the third person or without "
                   "the symptom words (“A customer is frustrated because …”) are the hardest; the threshold was kept "
                   "strict because a wrong-plan hit is worse than a miss.")
    neg = b.get("negatives") or {}
    grounded = neg.get("kb_grounded_plans") or []
    if grounded:
        out.append("**Borderline complaints can be answered from a related knowledge-base article** "
                   f"({len(grounded)} of {neg.get('n')} negatives): " + "; ".join(f"“{g}”" for g in grounded[:4]) + ".")
    out.extend([
        "**Multi-intent queries** are split only when each part maps to a known symptom family; each part is grounded "
        "and cached separately. A request-scoped `siis_response` that covers only one of the intents yields a plan for "
        "that intent alone; the uncovered intent is not mentioned (no hallucinated steps), and a single goal is never "
        "merged across unrelated intents.",
        "**Domain gaps.** Only Battery, Display, Camera and Performance knowledge exists. Out-of-scope complaints "
        "(connectivity, audio, accessories, other devices) return `contexts: []` with `fallback: no_siis_context` "
        "unless an SIIS text is supplied; non-English complaints are not translated (E18 records the observed behaviour).",
        "**Settings hierarchy variations.** Step paths from different One UI versions (`Battery` vs `Battery and device "
        "care > Battery`, `Lock screen` vs `Lock screen and AOD`) are resolved by label matching on the target control, "
        "not by the path prefix. A screen missing from the catalog gets `bixby://dummy_positive` (auto actions only) "
        "and never its parent menu; critical actions get a catalog link or none. With the official ~575-entry catalog, "
        "near-duplicate labels will produce more ambiguity fallbacks (dummy_positive or no link) than on the 110-entry "
        "fixture.",
        "**Determinism.** The same input is answered identically from the cache. Without the cache, the model call "
        "(temperature 0, fixed seed) is not guaranteed to be byte-identical; §2 reports the measured agreement. "
        "Plans are cached only when produced by the model path (rules-fallback plans are served but never cached).",
        "**Cold-path latency depends on the remote model**: rate limiting (HTTP 429) and overload (503) trigger "
        "fail-over to the next model and a hedged duplicate request; the tokens of a cancelled hedge are not "
        "reported by the API, so the cost of a hedged request can be slightly under-counted.",
        "**Evaluation bias.** Gold labels, SIIS texts and the catalog of the dev fixture share one author, and the "
        "deterministic rules extractor was tuned on the same formatting, so the rules-based numbers are optimistic. "
        "The hand-written paraphrase test split was inspected while tuning the cache; the LLM-generated paraphrases "
        "were created afterwards with a different model and are the reported hit rate.",
    ])
    return out


def main() -> int:
    text = render()
    (ROOT / "metrics.md").write_text(text + "\n", encoding="utf-8")
    print(f"wrote metrics.md ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
