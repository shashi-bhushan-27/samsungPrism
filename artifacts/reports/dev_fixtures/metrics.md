# System Performance Metrics & Evaluation Report
**Model(s):** groq/openai/gpt-oss-120b (structure extraction, primary) · groq/openai/gpt-oss-20b (query variations) · fail-over chain: groq/openai/gpt-oss-20b → groq/qwen/qwen3.8-27b. Models that actually served the cold requests: openai/gpt-oss-120b ×40. The pre-warmed cache plans (served on hits) were produced earlier with the gemini chain (default `LLM_PROVIDER=gemini`, `LLM_MODEL=gemini-3.1-flash-lite`); that key's free-tier daily quota was exhausted on the evaluation day, so the live measurements use the groq provider (`LLM_PROVIDER=groq`). Both providers run through the same pipeline and validation gates.
**Embeddings:** fastembed:BAAI/bge-small-en-v1.5 (384-d, local ONNX via fastembed)
**Environment:** 4 vCPU / 15.7 GB RAM / Ubuntu 24.04.4 LTS (Linux-6.18.44-fc-v50-x86_64-with-glibc2.39, Python 3.11.15), single uvicorn worker

> **Dataset: dev_fixtures (synthetic, NOT OFFICIAL).** The official starter assets (queries.json, siis_responses.json, deeplinks.json with ~575 entries, samples/) were not supplied, so every number below was measured on the synthetic development fixture in `data/dev_fixtures/` (110 catalog entries, 32 queries, 4 domains, 5 samples), whose gold labels were written by the same author as the SIIS texts and catalog. Accuracy on the official data is expected to be lower; re-run `make benchmark` after dropping the official files into `data/official/`.
> Measured 2026-10-01T04:48:20+00:00 → 2026-10-01T05:00:48+00:00 (UTC) over real HTTP against a real uvicorn server and the live Groq API. Raw data: `artifacts/reports/*.json`, `results.jsonl`, `artifacts/reports/results_all.jsonl` + `results_index.jsonl`.

---

## 1. Schema & Rule Compliance
Evaluated on sample datasets and held-out validation scenarios.

| Metric | Target | Measured Value |
| :--- | :--- | :--- |
| Schema-valid output lines | >= 99% | 100.00% (447/447 lines) ✅ |
| Rule compliance (Goal / Title / Description syntax) | >= 95% | 100.00% (5325 checks) ✅ |
| Absolute URL leaks | 0 | 0 ✅ |
| Deeplink catalog validity (exact URI match) | 100% | 100.00% (1024/1024 URIs) ✅ |
| Auto actions carrying valid actionable deeplink | >= 90% | 91.50% (652 catalog + 37 dummy_positive of 753) ✅ |

Scope: every HTTP 200 body produced by the benchmark (cold full pipeline incl. the determinism re-runs, reference samples, edge cases, exact hits, LLM and hand-written paraphrases, negatives; the warm cache server ran without the model, so its misses were answered by the grounded rules fallback). The same gates restricted to `results.jsonl` (one line per query of queries.json, cold full pipeline): schema-valid 100.00% (32 lines), goal/title/description rules 100.00%, URL leaks 0, catalog validity 100.00%, auto actions with valid actionable deeplink 91.80%.

| Additional gate (all output lines) | Measured |
| :--- | :--- |
| All business rules (every check, every field) | 100.00% of 44463 checks |
| Lines passing every gate | 447/447 |
| Auto actions with a real catalog deeplink (dummy_positive excluded) | 86.59% |
| Manual actions carrying an actionable deeplink | 0 |
| Critical actions not placed last | 0 |
| Violations by rule | none |

---

## 2. Accuracy Benchmarks
Evaluated against reference ground truth scenarios across Battery, Display, Camera, and Performance.

| Evaluation Metric | Scale / Anchor | Score |
| :--- | :--- | :--- |
| Step accuracy (completeness, correctness, ordering) | 0.0 - 3.0 | 2.902 |
| Deeplink relevance (exact target screen vs. parent menu) | 0.0 - 2.0 | 1.896 |

Cold full pipeline (request SIIS text, cache disabled), 32 scored responses (every query of queries.json once). Step accuracy = completeness 0.925 + correctness 0.992 + ordering 0.984. Deeplink relevance over 67 gold targets, outcomes {"exact": 63, "unmatched_action": 3, "parent": 1} (2 = exact screen or an equally exact alternative, 1 = parent menu, 0 = wrong/missing/fabricated); links attached to actions whose gold has no Settings target: 0. Scoring code: `app/evaluation/scoring.py`.

| Domain | Responses | Step accuracy | Deeplink relevance | Deeplink outcomes |
| :--- | :--- | :--- | :--- | :--- |
| Battery | 8 | 3.000 | 2.000 | exact 16 |
| Camera | 8 | 2.930 | 1.812 | parent 1, exact 14, unmatched_action 1 |
| Display | 8 | 2.802 | 1.778 | exact 16, unmatched_action 2 |
| Performance | 8 | 2.875 | 2.000 | exact 17 |

| Consistency check | Measured |
| :--- | :--- |
| Cold round 1 | step accuracy 2.902, deeplink relevance 1.896 |
| Cold round 2 | step accuracy 2.846, deeplink relevance 2.000 |
| Pre-warmed cache plans (served on hits, KB-grounded) | step accuracy 2.895, deeplink relevance 1.940 (32 plans) |
| Determinism, identical input twice with cache disabled (8 pairs) | identical plan structure (goal, title, action names, categories, deeplinks) 5/8; identical deeplink sequence 6/8; byte-identical response 5/8; identical query_variations 3/8 |

Reference samples (`samples/`, pipeline output vs the expected output of each sample):

| Sample | Step accuracy vs sample | Exact deeplinks | Same deeplink sequence | Same goal / title | Actions (expected → produced) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| sample_01_D01 | 2.350 | 1/1 | yes | yes / no | Configure Navigation Bar Settings → Change Navigation Type |
| sample_02_B01 | 3.000 | 5/5 | yes | yes / yes | Check Battery Usage; Put Unused Apps to Sleep; Turn On Power Saving; Turn Off Always On Display; Restart Your Phone; Update Phone Software → Check Battery Usage; Put Unused Apps To Sleep; Enable Power Saving; Disable Always On Display; Restart Phone; Update Software |
| sample_03_C02 | 2.870 | 3/4 | no | yes / no | Check Camera Permissions; Force Stop the Camera App; Clear the Camera Cache; Restart Your Phone; Start Safe Mode; Update Phone Software → Check Camera Permissions; Force Stop Camera App; Clear Camera Cache; Restart Phone; Check Safe Mode |
| sample_04_P01 | 2.889 | 3/3 | yes | no / no | Update Your Apps; Optimize With Device Care; Free Up Storage Space; Restart Your Phone; Factory Data Reset → Update Apps; Optimize Device Care; Free Up Storage; Restart Phone; Factory Data Reset |
| sample_05_D02 | 3.000 | 3/3 | yes | no / yes | Turn Off Adaptive Brightness; Change Motion Smoothness; Visit A Service Center; Start Safe Mode; Update Phone Software → Turn Off Adaptive Brightness; Change Motion Smoothness; Have Screen Checked; Check App Problems In Safe Mode; Update Software |

---

## 3. Latency Benchmarks (N >= 30 requests per path)

| Execution Path | Target (P95) | P50 (ms) | P95 (ms) |
| :--- | :--- | :--- | :--- |
| Cache hit - exact query match | <= 300 ms | 5.4 | 10.0 ✅ |
| Cache hit - unseen semantic paraphrase | <= 300 ms | 17.4 | 24.2 ✅ |
| Cold query - full pipeline extraction & mapping | <= 8000 ms | 1,790.3 | 2,552.3 ✅ |

Client-side wall time per HTTP request (sequential requests, localhost, keep-alive). Details:

| Path | N | P50 | P95 | P99 | Max | Server-side P95 (`meta.latency_ms`) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Exact hit | 64 | 5.4 | 10.0 | 12.0 | 14.4 | 7.1 |
| Semantic hit — unseen LLM paraphrases | 116 | 17.4 | 24.2 | 25.7 | 28.5 | 21.5 |
| Semantic hit — hand-written paraphrases | 62 | 12.0 | 15.3 | 16.9 | 17.0 | 13.4 |
| Cold full pipeline | 40 | 1,790.3 | 2,552.3 | 2,767.2 | 2,821.5 | 2,548.6 |

Cold requests over 8 s: 0 of 40. The cold path is dominated by the remote model call (extraction, with query variations generated in parallel); its tail follows the provider's API (fail-over to the next model on 429/5xx, a hedged duplicate request after 5.0 s).

Concurrency (stress test, `artifacts/reports/stress.json`, one uvicorn worker):

| Run | Requests | Concurrency | Throughput (req/s) | P50 (ms) | P95 (ms) | P99 (ms) | Unexpected errors | Contract failures |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| exact_hits@1 | 320 | 1 | 164.4 | 5.8 | 7.9 | 10.0 | 0 | 0 |
| semantic_hits@1 | 320 | 1 | 14.1 | 19.6 | 31.9 | 1,996.0 | 0 | 0 |
| mixed@1 | 320 | 1 | 47.2 | 6.6 | 25.9 | 56.4 | 0 | 0 |
| exact_hits@8 | 320 | 8 | 229.7 | 33.2 | 44.1 | 48.8 | 0 | 0 |
| semantic_hits@8 | 320 | 8 | 44.0 | 112.1 | 180.4 | 2,483.1 | 0 | 0 |
| mixed@8 | 320 | 8 | 106.5 | 71.3 | 139.8 | 157.2 | 0 | 0 |
| exact_hits@32 | 320 | 32 | 195.2 | 143.1 | 313.9 | 480.7 | 0 | 0 |
| semantic_hits@32 | 320 | 32 | 33.6 | 562.5 | 781.0 | 7,036.8 | 0 | 0 |
| mixed@32 | 320 | 32 | 67.6 | 314.8 | 445.0 | 570.3 | 0 | 0 |
| exact_hits@64 | 320 | 64 | 109.0 | 381.6 | 1,507.3 | 1,984.5 | 0 | 0 |
| semantic_hits@64 | 320 | 64 | 29.0 | 1,347.2 | 1,772.0 | 6,111.7 | 0 | 0 |
| mixed@64 | 320 | 64 | 49.0 | 616.7 | 1,787.0 | 3,283.8 | 0 | 0 |
| same_query_burst@200 | 200 | 200 | 117.2 | 1,117.6 | 1,600.9 | 1,648.4 | 0 | 0 |

Semantic runs include the few paraphrases that miss the cache and run the full pipeline; those requests set the P99 column. Beyond ~8 concurrent requests one worker queues (CPU-bound embedding); scale out with workers or replicas.

Server start → healthy: 3.56 s; first request after healthy: 8.16 ms; RSS idle 288.3 MB, after load 303.4 MB.

---

## 4. Operational Cost & Cache Efficacy

| Metric Item | Target | Measured Value |
| :--- | :--- | :--- |
| Cold query average inference cost | Tracked | $0.000527 |
| Cache hit inference cost | $0.00 | $0.00 (334 hits, model calls on hits: [0]) ✅ |
| Semantic cache hit rate (on unseen paraphrases) | >= 80% | 87.50% (112/128 served the correct plan) ✅ |
| Cost derivation method | - | (prompt tokens + completion tokens) x rate |

Cold path per query: 1.95 model calls, 1256.8 prompt tokens, 706.1 completion tokens (thinking tokens, when a model emits them, are billed at the completion rate); P95 cost $0.000723; total for 40 cold requests $0.021086. Rates: https://ai.google.dev/gemini-api/docs/pricing (retrieved 2026-09-30); https://console.groq.com/docs/models (retrieved 2026-10-01) (`app/core/config.py: MODEL_PRICING_USD_PER_MTOK`), each call priced at the rate of the model that actually served it.

| Cache efficacy detail | Measured |
| :--- | :--- |
| Unseen LLM paraphrases, round 2 (generated by gemini-3.5-flash-lite ×116, gemini-3.6-flash ×12; created after the last change to enrichment/cache logic) | 128 items; any hit 90.62%; correct-plan hits 87.50%; wrong-plan hits 4 |
| — style `non_native` | 28/32 correct hits |
| — style `short_angry` | 28/32 correct hits |
| — style `spoken` | 28/32 correct hits |
| — style `ticket_summary` | 28/32 correct hits |
| Round 1 LLM paraphrases (gemini-3.5-flash-lite; seen while fixing the enrichment lexicon; not an unbiased estimate) | 96 items; correct-plan hits 94.79%; wrong-plan hits 1 (pre-fix measurement: HARDENING_REPORT.md H9) |
| Hand-written test paraphrases (seen while tuning; optimistic) | 64 items; correct-plan hits 96.88%; wrong-plan hits 0 |
| Out-of-scope / borderline queries (must not hit) | 40 queries; cache hits 0; `no_siis_context` 36 |
| Threshold calibration (`scripts/calibrate_cache.py`) | max hit rate s.t. precision >= 0.99; midpoint of plateau: chosen 0.77; calibration split hit rate 1.0, test split 0.9688, wrong plans 0, negative false hits 0 |

---

## 5. Architectural Ablation Analysis

| Architecture Variant | Step Accuracy | Latency (P95) | Cost / Query | Key Observations |
| :--- | :--- | :--- | :--- | :--- |
| Baseline: Full LLM Deeplink Mapping | 2.892 | 4,472.3 ms | $0.003652 | Deeplink relevance 1.538 end to end, 1.769 on gold actions (exact 11, parent 1, wrong 1, missing 0 of 13); mapping stage P95 2,187.6 ms; 1.833 extra model calls/query; invented URIs rejected: 0 |
| Variant A: Hybrid BM25 + Dense Embedding Retrieval | 2.892 | 2,387.0 ms | $0.000528 | Deeplink relevance 2.000 end to end, 2.000 on gold actions (exact 13, parent 0, wrong 0, missing 0 of 13); mapping stage P95 37.6 ms; 0.0 extra model calls/query; invented URIs rejected: 0 |
| Variant B: Pure Rules-Based Deeplink Mapping | 2.892 | 2,353.6 ms | $0.000528 | Deeplink relevance 2.000 end to end, 2.000 on gold actions (exact 13, parent 0, wrong 0, missing 0 of 13); mapping stage P95 4.3 ms; 0.0 extra model calls/query; invented URIs rejected: 0 |

End to end: the full pipeline on all 6 queries with their SIIS text, cache disabled, variants interleaved per query (rotating order) so they share API conditions; only the mapper differs. Latency is in-process service time (in-process service latency (no HTTP), cache disabled, sequential requests; variant latency = measured model phase of the real run + measured replay). The baseline gives the model the whole catalog (URI + description + message + qna_description) and accepts its answer only if it is byte-identical to a catalog URI. Pure rules tie hybrid here because the fixture's catalog labels and gold steps share one author; hybrid retrieval is kept for recall on unseen catalog wording.

Mapping only (paired: the same labelled gold actions go through every mapper):

| Mapper | Deeplink relevance (0–2) | Exact | Parent | Wrong | Missing | Abstain correct | P95 ms / action | Cost / action | Invented URIs |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| llm | 1.769 | 11 | 1 | 1 | 0 | 100.00% | 1,902.8 | $0.001404 | 0 |
| hybrid | 2.000 | 13 | 0 | 0 | 0 | 100.00% | 13.2 | $0.000000 | 0 |
| bm25 | 2.000 | 13 | 0 | 0 | 0 | 100.00% | 0.5 | $0.000000 | 0 |
| dense | 2.000 | 13 | 0 | 0 | 0 | 100.00% | 14.3 | $0.000000 | 0 |
| rules | 2.000 | 13 | 0 | 0 | 0 | 100.00% | 1.5 | $0.000000 | 0 |

---

## 6. Known Edge Cases & System Limitations
* Edge-case suite (18/18 behaved as specified; URL leaks across the suite: 0):

| Case | Kind | Expected | Observed | Result |
| :--- | :--- | :--- | :--- | :--- |
| E01 | typo_heavy | plan | plan | pass |
| E02 | colloquial | plan | plan | pass |
| E03 | multi_symptom | plan | plan | pass |
| E04 | multi_intent | plan | plan | pass |
| E05 | unknown_domain | no_siis_context | no_siis_context | pass |
| E06 | unsupported_domain | no_siis_context | no_siis_context | pass |
| E07 | no_solution_siis | no_match | no_match | pass |
| E08 | irrelevant_siis | no_match | no_match | pass |
| E09 | url_injection_query | plan | plan | pass |
| E10 | url_injection_siis | plan | plan | pass |
| E11 | fabricated_uri_siis | plan | plan | pass |
| E12 | prompt_injection_siis | plan | plan | pass |
| E13 | hierarchy_variation | plan | plan | pass |
| E14 | long_query | plan | plan | pass |
| E15 | empty_query | http_422 | http_422 | pass |
| E16 | whitespace_query | http_422 | http_422 | pass |
| E17 | too_long_query | http_422 | http_422 | pass |
| E18 | non_english | any | plan | pass |

* **Wrong-plan cache hits (4 of 128 unseen paraphrases).** A sibling plan was served: “froze and won't respond to touch or buttons” matched the touchscreen plan (touch concept subsumes freeze), “phone is completely full and freezing” missed the storage concept, and a symptom-less “battery life is a joke” matched a sibling battery plan. Tracked by the strict xfail `test_attack9_no_wrong_plan_is_ever_served`; fixing it needs a fresh held-out round to re-measure.
* **Semantic cache misses on unseen paraphrases (12 of 128).** Missed items fall back to the full pipeline (correct, but cold latency and cost). Examples: “Fix this garbage display! Max brightness looks like 10%! Useless outside!”; “The customer reports that the smartphone screen turns completely black after only a few seconds of use.”; “My phone screen not working for my touch after I put new protector glass. Please help fix this problem.”; “um hey siri why does my phone battery die so fast like literally every hour i have to charge it”. Descriptions written in the third person or without the symptom words (“A customer is frustrated because …”) are the hardest; the threshold was kept strict because a wrong-plan hit is worse than a miss.
* **Borderline complaints can be answered from a related knowledge-base article** (4 of 40 negatives): “Wireless charging is not working”; “Fingerprint sensor is slow to unlock”; “Can't record slow motion video”; “Portrait mode background blur isn't working”.
* **Multi-intent queries** are split only when each part maps to a known symptom family; each part is grounded and cached separately. A request-scoped `siis_response` that covers only one of the intents yields a plan for that intent alone; the uncovered intent is not mentioned (no hallucinated steps), and a single goal is never merged across unrelated intents.
* **Domain gaps.** Only Battery, Display, Camera and Performance knowledge exists. Out-of-scope complaints (connectivity, audio, accessories, other devices) return `contexts: []` with `fallback: no_siis_context` unless an SIIS text is supplied; non-English complaints are not translated (E18 records the observed behaviour).
* **Settings hierarchy variations.** Step paths from different One UI versions (`Battery` vs `Battery and device care > Battery`, `Lock screen` vs `Lock screen and AOD`) are resolved by label matching on the target control, not by the path prefix. A screen missing from the catalog gets `bixby://dummy_positive` (auto actions only) and never its parent menu; critical actions get a catalog link or none. With the official ~575-entry catalog, near-duplicate labels will produce more ambiguity fallbacks (dummy_positive or no link) than on the 110-entry fixture.
* **Determinism.** The same input is answered identically from the cache. Without the cache, the model call (temperature 0, fixed seed) is not guaranteed to be byte-identical; §2 reports the measured agreement. Plans are cached only when produced by the model path (rules-fallback plans are served but never cached).
* **Cold-path latency depends on the remote model**: rate limiting (HTTP 429) and overload (503) trigger fail-over to the next model and a hedged duplicate request; the tokens of a cancelled hedge are not reported by the API, so the cost of a hedged request can be slightly under-counted.
* **Evaluation bias.** Gold labels, SIIS texts and the catalog of the dev fixture share one author, and the deterministic rules extractor was tuned on the same formatting, so the rules-based numbers are optimistic. The hand-written paraphrase test split was inspected while tuning the cache; the LLM-generated paraphrases were created afterwards with a different model and are the reported hit rate.

