# Requirements Traceability

Status is **Complete** only when executable evidence exists. Other values:

* **Partial**: implemented, but a target is only partly met or a gap is documented.
* **Not met**: measured and below the target.

The measured values are in `metrics.md`, rendered from `artifacts/reports/*.json`. The dataset is the official
starter data (`data/official/`); the earlier synthetic-fixture evidence (gold-label accuracy, ablation, live hostile
probes) is archived in `artifacts/reports/dev_fixtures/`.

## Problem statement — pipeline (PDF §2) and contract (PDF §4)

| Requirement | Implementation | Test | Status | Evidence |
| :--- | :--- | :--- | :--- | :--- |
| Query enrichment: normalise colloquial text, slang, typos | `app/services/query_enrichment.py`, `intent_lexicon.py` | `tests/unit/test_enrichment.py` | Complete | E01/E02 typo and colloquial edge cases → plan (`benchmark.json: edge_cases`) |
| Canonical technical query and semantic cache key | `CanonicalIntent.canonical_query`, intent signature | `test_enrichment.py` | Complete | cache keys in `artifacts/cache/prewarm_plans.jsonl` |
| 8–10 distinct `query_variations` across registers | model variations + templates, `finalize_variations` | `tests/unit/test_contract.py` (count, distinctness) | Complete | `variations.*` rule checks pass on every output line (`compliance_all_paths.checks_by_rule`) |
| Structure extraction into `Goal` / `Action` / steps | `app/services/structure_extraction.py`, `app/llm/prompts.py` | `tests/integration/test_pipeline.py` | Complete | `results.jsonl` |
| No hallucinated steps (grounded in the reference text only) | `app/services/grounding.py`; actions need grounded content | `test_hallucinated_steps_are_removed`; adversarial attacks 1 and 10 | Complete | live A1 probes (`hostile.json`) |
| No viable solution → `contexts: []` and `fallback: "no_match"` | orchestrator fallback | `test_no_match_fallback`; attack 14 | Complete | E07, E08 and the A14 probe |
| No SIIS context → `fallback: "no_siis_context"` | `SiisRetriever` concept gate and near-duplicate bar (H13) | attack 13 (scripted model and neural embedder) | Complete | 36/40 negatives → `no_siis_context`; E05/E06; A13 probe |
| `goal` exact syntax | `business_rules.check_goal`, `repair.build_goal` | `test_contract.py` | Complete | goal checks: 100% |
| `title`: 2–3 words, sentence case | `check_title`, `repair_title` | `test_contract.py`, `test_repair_and_categories.py` | Complete | title checks: 100% |
| `score` is a float in [0, 1], raw JSON type checked | `check_score` (H12) | `test_contract.py` | Complete | `score.*` checks |
| `actionName`: Title Case, one screen/feature | `check_action_name`, minimal repair (H1, H2) | `test_resolver.py`, `tests/regression/test_samples.py` | Complete | 5/5 samples reproduced after extraction |
| `description`: 5–7 words, starts with "It will" | `check_description`, `repair_description` | `test_contract.py` | Complete | description checks: 100% |
| Steps: imperative, one interaction each, no URLs | `text_rules`, `split_interactions`, `url_safety` | `test_text_and_url_rules.py` | Complete | step checks: 100% |
| Categories auto / manual / critical | `category_rules.classify` | `test_repair_and_categories.py`; attacks 6 and 7 | Complete | A6 probe |
| Manual actions carry no actionable deeplink | mapper and gate `deeplink.manual_actionable` | attack 7 | Complete | `manual_actions_with_actionable_deeplink = 0` |
| Critical actions ordered last (least disruptive first) | `sequencing.py`, critical ranks | attack 6 | Complete | `critical_order_violations = 0` |
| One Action = One Screen (no fragmentation, no bundling) | `action_grouping.py` (H3, H4) | attacks 4 and 5; `test_fragmented…`, `test_bundled…` | Complete | A4, A5 probes |
| `actionableDeeplink` copied verbatim from the catalog | `CatalogEntry.to_deeplink`; registry objects only | attack 2; `deeplink_validator` field-integrity checks | Complete | catalog validity 100% (`compliance_all_paths`) |
| The catalog's `dummy_positive` placeholder (`voiceassist://dummy_positive` on the official catalog) only for an unindexed valid Settings screen | resolver `dummy` decision (auto only) | `test_parent_menu_is_never_returned_for_unindexed_child` | Complete | sample D01 = Appendix B |
| Zero URL leaks | `url_safety.sanitize_*`, `scan_payload` gate | attack 1 (10 URL forms × every field) | Complete | `absolute_url_leaks = 0`; A1 probes |
| Pure JSON delivery, no markdown or prose | FastAPI `JSONResponse`; strict JSON loading | `tests/integration/test_api.py` | Complete | `results.jsonl` parses line by line |
| Matching on metadata, never on the masked URI string | `CatalogEntry.semantic_text` excludes the URI | `test_retrieval.py` | Complete | — |

## Retrieval, mapping and cache

| Requirement | Implementation | Test | Status | Evidence |
| :--- | :--- | :--- | :--- | :--- |
| BM25 keyword retrieval | `app/retrieval/bm25.py` | `test_retrieval.py` | Complete | ablation `mapping_only.bm25` |
| Dense embeddings (no re-embedding per request) | `embeddings.py` (bge-small, ONNX), persisted `dense.py` indexes | `test_retrieval.py` | Complete | ablation `mapping_only.dense` |
| Hybrid retrieval | `app/retrieval/hybrid.py` | `test_retrieval.py` | Complete | ablation variant A |
| Exact target-screen resolution | `reranker.TargetResolver`, `ui_path.py` | `test_resolver.py`; attack 3 (all 67 gold targets) | Complete | deeplink relevance (`metrics.md` §2) |
| Parent-menu protection | parent penalty; exactness by label cover | attack 3; `test_vague_llm_action_name…` (H7) | Complete | A3 probes; 0 parent outcomes on the live cold path |
| Duplicate and ambiguous catalog candidates | registry duplicates; ambiguity fallback | `test_duplicate_metadata_resolves_deterministically` | Complete | — |
| Exact cache | `PlanCache.lookup_exact` | `tests/unit/test_cache.py` | Complete | exact-hit latency (`metrics.md` §3) |
| Semantic cache with calibrated threshold | `PlanCache.lookup_semantic`, `scripts/calibrate_cache.py` | `test_cache.py`; attack 9 | **Partial** | official data: 83.75% correct-plan hits on fresh held-out round 3, with 4 wrong-plan hits (3 for queries without a cached plan); synthetic fixture: strict xfail `test_attack9_no_wrong_plan_is_ever_served` |
| Cache-poisoning protection | concept, qualifier and domain gates; scope isolation | attack 8 (constant embedder); `test_request_scoped_plan…` | Complete | A8 probes |
| Persistent cache with versioned invalidation | `SqliteCacheStore`; catalog fingerprint + `PIPELINE_VERSION` | `test_sqlite_persistence_and_version_invalidation` | Complete | — |
| Cache stores only validated plans; hits re-validated | `put` validator; revalidation on hit | `test_invalid_plan_is_never_cached`, `test_hit_that_fails_revalidation_is_evicted` | Complete | — |
| Cache hit makes no model call and costs $0 | orchestrator fast path | `test_cold_path_then_exact_and_semantic_hits` | Complete | `hit_model_calls_values = [0]`, `hit_cost_usd_values = [0.0]` |

## Service, operations and evaluation

| Requirement | Implementation | Test | Status | Evidence |
| :--- | :--- | :--- | :--- | :--- |
| `POST /v1/troubleshoot` | `app/api/routes.py` | `test_api.py` (23 tests) | Complete | `results.jsonl`; `docker_verification.json` |
| `GET /health` → `{"status":"ok"}` only when everything is ready | `health_components` | `test_api.py` | Complete | Docker HEALTHCHECK; container healthy in 5.3 s |
| Error boundaries (422 / 413 / 502 / 503, JSON envelope) | `app/api/errors.py`, `ServiceError` | `test_api.py`; attack 10 | Complete | E15–E17 → 422 |
| LLM provider abstraction | `app/llm/base.py`; Gemini and Groq providers | `test_llm_provider.py`, `test_groq_provider.py` | Complete | both providers used in this project |
| Bounded repair loop for malformed model output | `StructureExtractor` (≤ 1 repair) and rules fallback | attack 10 (11 malformation kinds) | Complete | — |
| Deterministic execution | temperature 0, fixed seed; cache gives identical repeats | attack 11 (scripted model) | **Partial** | the live model is not byte-deterministic (`benchmark.json: determinism`); cached answers are identical (A11) |
| Latency instrumentation and benchmarks (N ≥ 30 per path) | `StageTimer`, `X-Latency-Ms`, `scripts/run_benchmarks.py` | — | Complete | `metrics.md` §3 |
| Cost and token tracking | `CostMeter`, verified price table, `X-Cost-USD` | `test_llm_mapping_calls_are_billed…` (H5) | Complete | `metrics.md` §4 |
| Backpressure on model calls | `LLM_MAX_CONCURRENCY` (H6) | `test_concurrency_limit_queues_excess_calls` | Complete | — |
| Stress testing | `scripts/stress_test.py` | — | Complete | `artifacts/reports/stress.json` |
| Ablation: LLM vs hybrid vs rules | `scripts/run_ablation.py` (paired) | — | Complete (6-query live subset; free-tier token limits) | `artifacts/reports/ablation.json` |
| Edge-case evaluation | `data/dev_fixtures/eval/edge_cases.json` | adversarial suite | Complete | `metrics.md` §6 |
| `results.jsonl` (one response per query) | `scripts/run_benchmarks.py` | — | Complete | `results.jsonl` (32 lines) |
| `metrics.md` (Appendix C template, measured values only) | `scripts/render_metrics.py` | — | Complete | `metrics.md` |
| Docker build | `Dockerfile` (non-root, model and indexes baked in) | — | Complete | `docker_verification.json` |
| Regression and adversarial tests | `tests/regression`, `tests/adversarial` | `pytest` | Complete | full suite green; one documented strict xfail |
| Benchmark reproducibility | `Makefile`, fixed seeds, frozen held-out sets | — | Partial | live-model runs vary between runs (see determinism) and are bound by provider quotas |
| Official datasets (`input.txt`, `siis_responses.json`, 578-entry `deeplinks.json`, sample) | format-tolerant loaders (`voiceassist://` URIs, `{title, content}` SIIS objects, catalog-defined placeholder) | `test_official_format.py` | Complete | `metrics.md` (dataset: official); 18/20 queries grounded |
| Auto actions with a valid deeplink ≥ 90% | resolver + catalog placeholder | `test_resolver.py` | **Not met** | 24.5% on official data: most official auto steps are outside Settings (`metrics.md` §6, H16) |
