# System Performance Metrics & Evaluation Report
**Model(s):** gemini/gemini-3.1-flash-lite (structure extraction, primary) · gemini/gemini-3.1-flash-lite (query variations) · fail-over chain: gemini/gemini-3.5-flash-lite → gemini/gemini-3.6-flash. Models that actually served the cold requests: gemini-3.1-flash-lite ×26.
**Embeddings:** fastembed:BAAI/bge-small-en-v1.5 (384-d, local ONNX via fastembed)
**Environment:** 4 vCPU / 15.7 GB RAM / Ubuntu 24.04.4 LTS (Linux-6.18.44-fc-v51-x86_64-with-glibc2.39, Python 3.11.15), single uvicorn worker

> **Dataset: official** (`data/official/`): the organisers' 20 customer complaints (`input.txt`), their SIIS answers (`siis_responses.json`), the 578-entry masked deeplink catalog and 1 reference sample. No gold labels ship with it, so step accuracy and deeplink relevance can be scored only against the reference sample (§2); every other number is measured on all requests. Negatives, edge cases and held-out paraphrases in `data/official/eval/` were written by the team (paraphrases LLM-generated) and are labelled as such. The semantic-cache threshold was calibrated earlier on the synthetic fixture and frozen; it was not re-tuned on the official data. The earlier synthetic-fixture report is kept in `artifacts/reports/dev_fixtures/`.
> Measured 2026-10-02T03:39:46+00:00 → 2026-10-02T03:46:41+00:00 (UTC) over real HTTP against a real uvicorn server and the live Gemini API. Raw data: `artifacts/reports/*.json`, `results.jsonl`, `artifacts/reports/results_all.jsonl` + `results_index.jsonl`.

> **Earlier run on Groq** (2026-10-01T22:36:12+00:00; cold requests served by openai/gpt-oss-20b ×18, openai/gpt-oss-120b ×8) is archived in `artifacts/reports/official_groq/` (its `metrics.md`, `results.jsonl` and pre-warmed plans). Same code and data; only the model differs.

---

## 1. Schema & Rule Compliance
Evaluated on sample datasets and held-out validation scenarios.

| Metric | Target | Measured Value |
| :--- | :--- | :--- |
| Schema-valid output lines | >= 99% | 100.00% (262/262 lines) ✅ |
| Rule compliance (Goal / Title / Description syntax) | >= 95% | 100.00% (3131 checks) ✅ |
| Absolute URL leaks | 0 | 0 ✅ |
| Deeplink catalog validity (exact URI match) | 100% | 100.00% (85/85 URIs) ✅ |
| Auto actions carrying valid actionable deeplink | >= 90% | 31.46% (29 catalog + 27 dummy_positive of 178) ❌ |

Scope: every HTTP 200 body produced by the benchmark (cold full pipeline incl. the determinism re-runs, reference samples, edge cases, exact hits, LLM and hand-written paraphrases, negatives; the warm cache server ran without the model, so its misses were answered by the grounded rules fallback). The same gates restricted to `results.jsonl` (one line per query of queries.json, cold full pipeline): schema-valid 100.00% (20 lines), goal/title/description rules 100.00%, URL leaks 0, catalog validity 100.00%, auto actions with valid actionable deeplink 28.57%.

| Additional gate (all output lines) | Measured |
| :--- | :--- |
| All business rules (every check, every field) | 100.00% of 25774 checks |
| Lines passing every gate | 262/262 |
| Auto actions with a real catalog deeplink (dummy_positive excluded) | 16.29% |
| Manual actions carrying an actionable deeplink | 0 |
| Critical actions not placed last | 0 |
| Violations by rule | none |

---

## 2. Accuracy Benchmarks
Evaluated against the official reference sample. The official queries have no gold labels, so the per-query accuracy table below is empty; see the sample table and §1 for what is measured on every query.

| Evaluation Metric | Scale / Anchor | Score |
| :--- | :--- | :--- |
| Step accuracy (completeness, correctness, ordering) | 0.0 - 3.0 | not measured |
| Deeplink relevance (exact target screen vs. parent menu) | 0.0 - 2.0 | not measured |

Without gold labels the cold responses cannot be scored per query; the reference sample below is the only accuracy anchor, and §1 applies every rule check to every response.

| Consistency check | Measured |
| :--- | :--- |
| Determinism, identical input twice with cache disabled (6 pairs) | identical plan structure (goal, title, action names, categories, deeplinks) 6/6; identical deeplink sequence 6/6; byte-identical response 6/6; identical query_variations 4/6 |

Reference samples (`samples/`, pipeline output vs the expected output of each sample):

| Sample | Step accuracy vs sample | Exact deeplinks | Same deeplink sequence | Same goal / title | Actions (expected → produced) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| sample_01_screen_damage | 1.000 | 0/1 | no | no / no | Back Up Phone Data; Schedule Screen Repair Service → Schedule Repair Service; Visit Service Center |

---

## 3. Latency Benchmarks (N >= 30 requests per path)

| Execution Path | Target (P95) | P50 (ms) | P95 (ms) |
| :--- | :--- | :--- | :--- |
| Cache hit - exact query match | <= 300 ms | 9.6 | 13.7 ✅ |
| Cache hit - unseen semantic paraphrase | <= 300 ms | 29.2 | 41.6 ✅ |
| Cold query - full pipeline extraction & mapping | <= 8000 ms | 2,141.2 | 2,584.9 ✅ |

Client-side wall time per HTTP request (sequential requests, localhost, keep-alive). Details:

| Path | N | P50 | P95 | P99 | Max | Server-side P95 (`meta.latency_ms`) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Exact hit | 36 | 9.6 | 13.7 | 14.3 | 14.6 | 9.6 |
| Semantic hit — unseen LLM paraphrases | 66 | 29.2 | 41.6 | 45.5 | 46.9 | 38.4 |
| Semantic hit — hand-written paraphrases | 0 | not measured | not measured | not measured | not measured | not measured |
| Cold full pipeline | 26 | 2,141.2 | 2,584.9 | 2,689.7 | 2,720.5 | 2,580.3 |

Cold requests over 8 s: 0 of 26. The cold path is dominated by the remote model call (extraction, with query variations generated in parallel); its tail follows the provider's API (fail-over to the next model on 429/5xx, a hedged duplicate request after 5.0 s).

Concurrency (stress test, `artifacts/reports/stress.json`, one uvicorn worker):

| Run | Requests | Concurrency | Throughput (req/s) | P50 (ms) | P95 (ms) | P99 (ms) | Unexpected errors | Contract failures |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| exact_hits@1 | 320 | 1 | 2.6 | 11.3 | 46.5 | 13,333.1 | 0 | 0 |
| semantic_hits@1 | 320 | 1 | 1.7 | 31.1 | 2,312.2 | 13,604.4 | 0 | 0 |
| mixed@1 | 320 | 1 | 5.1 | 13.5 | 1,734.2 | 2,698.8 | 0 | 0 |
| exact_hits@8 | 320 | 8 | 16.0 | 48.3 | 172.1 | 10,929.3 | 0 | 0 |
| semantic_hits@8 | 320 | 8 | 18.6 | 110.9 | 2,688.3 | 2,956.3 | 0 | 0 |
| mixed@8 | 320 | 8 | 33.2 | 84.3 | 1,572.8 | 2,717.1 | 0 | 0 |
| exact_hits@32 | 320 | 32 | 50.1 | 335.9 | 722.6 | 4,186.2 | 0 | 0 |
| semantic_hits@32 | 320 | 32 | 11.7 | 535.2 | 16,624.4 | 18,478.6 | 0 | 0 |
| mixed@32 | 320 | 32 | 29.1 | 333.1 | 5,004.4 | 9,364.9 | 0 | 0 |
| exact_hits@64 | 320 | 64 | 34.5 | 755.4 | 3,782.9 | 5,841.5 | 0 | 0 |
| semantic_hits@64 | 320 | 64 | 9.8 | 1,516.3 | 13,668.6 | 20,488.6 | 0 | 0 |
| mixed@64 | 320 | 64 | 12.3 | 1,015.3 | 5,313.2 | 9,714.4 | 0 | 0 |
| same_query_burst@200 | 200 | 200 | 52.5 | 2,801.1 | 3,661.4 | 3,748.7 | 0 | 0 |

Semantic runs include the few paraphrases that miss the cache and run the full pipeline; those requests set the P99 column. Exact-hit runs cycle through every official query, including the ones with no cached plan (`no_match`); each of those calls the live model, so provider latency and rate-limit back-off set their throughput and P99 while P50/P95 stay at cache speed.  Beyond ~8 concurrent requests one worker queues (CPU-bound embedding); scale out with workers or replicas.

Server start → healthy: 5.12 s; first request after healthy: 14.22 ms; RSS idle 296.5 MB, after load 322.6 MB.

---

## 4. Operational Cost & Cache Efficacy

| Metric Item | Target | Measured Value |
| :--- | :--- | :--- |
| Cold query average inference cost | Tracked | $0.001176 |
| Cache hit inference cost | $0.00 | $0.00 (170 hits, model calls on hits: [0]) ✅ |
| Semantic cache hit rate (on unseen paraphrases) | >= 80% | 81.25% (65/80 served the correct plan) ✅ |
| Cost derivation method | - | (prompt tokens + completion tokens) x rate |

Cold path per query: 1.462 model calls, 1606.9 prompt tokens, 516.4 completion tokens (thinking tokens, when a model emits them, are billed at the completion rate); P95 cost $0.001792; total for 26 cold requests $0.030585. Rates: https://ai.google.dev/gemini-api/docs/pricing (retrieved 2026-09-30); https://console.groq.com/docs/models (retrieved 2026-10-01) (`app/core/config.py: MODEL_PRICING_USD_PER_MTOK`), each call priced at the rate of the model that actually served it.

| Cache efficacy detail | Measured |
| :--- | :--- |
| Unseen LLM paraphrases, round 3 (generated by qwen/qwen3.8-27b ×76, openai/gpt-oss-20b ×4; created after the last change to enrichment/cache logic) | 80 items; any hit 82.50%; correct-plan hits 81.25%; wrong-plan hits 1 |
| — style `forum_post` | 16/20 correct hits |
| — style `keywords` | 17/20 correct hits |
| — style `question` | 16/20 correct hits |
| — style `text_message` | 16/20 correct hits |
| Round 2 LLM paraphrases (qwen/qwen3.8-27b; seen while fixing the enrichment lexicon; not an unbiased estimate) | 80 items; correct-plan hits 80.00%; wrong-plan hits 2 (pre-fix measurement: HARDENING_REPORT.md H18) |
| Out-of-scope / borderline queries (must not hit) | 20 queries; cache hits 0; `no_siis_context` 20 |

---

## 5. Architectural Ablation Analysis

Not run on the official data. The full-LLM baseline sends the whole 577-entry catalog with every mapping call (about 25K tokens), which exceeds the free-tier limit of 8K tokens per minute, and the mapping-only comparison needs gold actions, which the official data does not have. The measured ablation on the synthetic fixture (same code path) is archived in `artifacts/reports/dev_fixtures/ablation.json` and `artifacts/reports/dev_fixtures/metrics.md` §5.

---

## 6. Known Edge Cases & System Limitations
* Edge-case suite (17/18 behaved as specified; URL leaks across the suite: 0):

| Case | Kind | Expected | Observed | Result |
| :--- | :--- | :--- | :--- | :--- |
| E01 | typo_heavy | plan | plan | pass |
| E02 | colloquial | plan | plan | pass |
| E03 | multi_symptom | plan | plan | pass |
| E04 | multi_intent | plan | no_match | FAIL |
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
| E18 | non_english | any | no_siis_context | pass |

* **Wrong-plan cache hits (1 of 80 unseen paraphrases).** A sibling plan was served for a paraphrase of a different official complaint; 0 of them paraphrase a query that has no cached plan of its own (row_12, row_7 ended `no_match`), so only a sibling could match. The 20 official complaints are all screen/display issues, so many are near neighbours. Examples: “cracked hinge, unresponsive touch, display visibility”.
* **Semantic cache misses on unseen paraphrases (14 of 80).** Missed items fall back to the full pipeline (correct, but cold latency and cost). Examples: “How do I transfer data from my Nexa Fold X1 when the screen is completely black and unresponsive?”; “Why is my tablet screen staying dark with only three app icons lit, causing everything else to fail to load?”; “My tablet screen remains mostly black with only three app icons visible, while the rest are obscured and unresponsive. Because of this, nothing loads up and I am completely unable to use the device.”; “dark screen, only 3 icons lit, apps won't open, tablet unresponsive”. Descriptions written in the third person or without the symptom words (“A customer is frustrated because …”) are the hardest; the threshold was kept strict because a wrong-plan hit is worse than a miss.
* **Knowledge scope.** The official knowledge base holds 20 SIIS articles, all about screen and display problems. Out-of-scope complaints return `contexts: []` with `fallback: no_siis_context` unless an SIIS text is supplied; non-English complaints are not translated (E18 records the observed behaviour).
* **SIIS answers that do not address the complaint.** Some official SIIS answers describe a related feature rather than the reported symptom (for example a Multi window article for a dark-screen complaint). The model then returns `no_match` instead of inventing steps; 2 official queries end this way in the pre-warmed cache. Borderline cases are judgement calls and can flip between runs.
* **Catalog coverage.** Screens named in the SIIS text that have no catalog entry (app storage, Smart View, Quick Access panel) get `voiceassist://dummy_positive` for auto actions, or no link when no Settings screen is identified (quick-panel gestures, the Apps screen). Physical steps (power button, charging, battery removal, damage inspection) are classified manual by deterministic rules and never get a link.
* **Auto actions without a deeplink (target ≥ 90% not met).** Of 178 auto actions in all responses, 56 carry a catalog or placeholder link. 55 of the unlinked ones come from the warm server's rules fallback (it ran without a model, so its cache misses were extracted by the deterministic parser, which cannot tell a Settings change from an app or panel gesture); 67 come from model-extracted plans. In the official SIIS articles most auto steps happen outside Settings (Quick Settings panel, Quick Access panel, Data Transfer and Smart View apps, Camera Pro mode), where the catalog has no entry and no Settings screen can be identified, so no link is attached rather than a guessed one. Relabelling such actions as manual would raise the number but contradicts the category definition (`HARDENING_REPORT.md` H16).
* **Accuracy is not measured at scale.** With one reference sample and no gold labels for the 20 queries, step accuracy and deeplink relevance are reported only for the sample.
* **Multi-intent queries** are split only when each part maps to a known symptom family; each part is grounded and cached separately. A request-scoped `siis_response` that covers only one of the intents yields a plan for that intent alone; the uncovered intent is not mentioned (no hallucinated steps), and a single goal is never merged across unrelated intents.
* **Determinism.** The same input is answered identically from the cache. Without the cache, the model call (temperature 0, fixed seed) is not guaranteed to be byte-identical; §2 reports the measured agreement. Plans are cached only when produced by the model path (rules-fallback plans are served but never cached).
* **Cold-path latency depends on the remote model**: rate limiting (HTTP 429) and overload (503) trigger fail-over to the next model and a hedged duplicate request; the tokens of a cancelled hedge are not reported by the API, so the cost of a hedged request can be slightly under-counted.

