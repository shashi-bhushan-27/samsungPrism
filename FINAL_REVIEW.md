# Final Production-Readiness Review

**Verdict: not "production ready".** Measured on the official data, two mandatory targets are not met:
auto actions carrying a valid deeplink (24.5% against ≥ 90%), and cache precision (4 wrong-plan hits on 80 unseen
paraphrases, 3 of them for queries that have no cached plan of their own). Every other measured target is met.

The numbers below come from `metrics.md`, rendered from `artifacts/reports/*.json` (official dataset, run
2026-10-01). Models: Groq `openai/gpt-oss-120b` for extraction and `openai/gpt-oss-20b` for variations and
fail-over. The free-tier daily token quota ran out during the final run, so 18 of 26 cold requests were served by
the fail-over model; the previous run with every cold request on `gpt-oss-120b` is archived in
`artifacts/reports/official_run2/`.

## Architecture summary

```
complaint (+ optional SIIS text)
  → [0] query enrichment: normalise, typo-fix (domain words), symptom concepts, canonical key
  → [3] cache: exact key → semantic (bge-small, threshold 0.77, concept/qualifier/domain gates, re-validation)
        hit → validated plan, no model call, $0
  → source: request SIIS text, else BM25+dense KB retrieval (concept-gated), else no_siis_context
  → [1] LLM extraction (strict JSON schema) → grounding check → fragment merge / bundle split → categories
  → [2] hybrid BM25+dense retrieval → exact-screen resolver (parent-menu protection) → catalog URI → sequencing
  → repair → gates (schema.py, business rules, URL scan, catalog integrity) → cache write → JSON response
```

The model reads language and extracts structure from the reference text. Code enforces every hard rule.

## Implemented components

* **API.** FastAPI service with `POST /v1/troubleshoot`, `GET /health`, `/health/details` and the `/demo` page.
* **Validation.** Schema and business-rule validators, a URL sanitiser and a catalog-integrity gate.
* **Catalog and knowledge base.** Format-tolerant loaders and a catalog registry; an SIIS knowledge index.
* **Retrieval.** BM25, dense (bge-small, persisted) and hybrid retrieval; a UI-path parser and the
  exact-target-screen resolver.
* **Extraction.** Grounded LLM extraction with bounded repair and a deterministic rules fallback.
* **Plan building.** Action grouping, splitting and merging; category rules and critical-last sequencing.
* **Cache.** Exact and semantic plan cache with SQLite persistence, versioned invalidation and pre-warm
  export/import.
* **LLM providers.** Gemini and Groq, with fail-over, hedging, backpressure and quota logging.
* **Accounting.** Cost and latency instrumentation: meters and response headers.
* **Tooling.** Benchmark, ablation, stress, hostile-probe, calibration and paraphrase-generation scripts; a
  Dockerfile.

## Measured metrics: targets vs actual (official data)

| Target | Actual | Status |
| :--- | :--- | :--- |
| Schema-valid lines ≥ 99% | 100% (262/262) | Met |
| Goal / title / description rule compliance ≥ 95% | 100% (3,171 checks); all business rules 100% (23,899 checks) | Met |
| Absolute URL leaks = 0 | 0 | Met |
| Deeplink catalog validity = 100% | 100% (83/83 URIs) | Met |
| Auto actions with a valid actionable deeplink ≥ 90% | 24.5% (27 catalog + 9 placeholder of 147) | **Not met** |
| Cache hit P95 ≤ 300 ms (exact) | 9.8 ms (N = 36) | Met |
| Cache hit P95 ≤ 300 ms (unseen paraphrase) | 29.9 ms (N = 71) | Met |
| Cold full pipeline P95 ≤ 8 s | 2.17 s (N = 26, 0 over 8 s) | Met |
| Cache hit inference cost $0.00 | $0.00 (179 hits, 0 model calls) | Met |
| Cold cost tracked | $0.000370 per query (P95 $0.000456) | Met |
| Semantic hit rate ≥ 80% on unseen paraphrases | 83.75% correct-plan hits (67/80, fresh round 3) | Met |
| No wrong-plan hits (cache precision) | **4/80** (3 for queries without a cached plan) | **Not met** |
| Official queries with a grounded plan | 18/20 (2 `no_match`) | reported |
| Reference sample (step accuracy 0–3) | 0.0 in the final run (fail-over model answered `no_match`); 1.0 in the all-120b run | **Partial** |
| Deterministic execution | cached repeats identical; live model identical plan structure 4/6 | **Partial** |
| Edge cases | 16/18 (E03, E04 `no_match` from the fail-over model; 18/18 in the all-120b run) | **Partial** |
| Negatives (out of scope) | 20/20 `no_siis_context`, 0 cache hits | Met |
| Load | 0 unexpected errors, 0 contract failures up to 64 concurrent requests and a 200-request burst | Met |
| Hostile probes | 19/19 live on the synthetic fixture; 14 attack classes offline (390 offline tests) | Met (not re-run live on official data) |

## Failed or partial targets

1. **Auto actions without a deeplink.** Most auto steps in the official SIIS articles happen outside Settings
   (Quick Settings and Quick Access panels, the Data Transfer and Smart View apps, Camera Pro mode). The catalog has
   no entry for them and no Settings screen can be identified, so no link is attached instead of a guessed one.
   Relabelling them as manual was tried and rejected (H16).
2. **Wrong-plan cache hits.** Two official queries have no cached plan (their SIIS article does not address the
   complaint), so paraphrases of them can only match a sibling plan.
3. **Model quota.** The free-tier daily quota (200K tokens/day per model) bounds how much can be measured on the
   primary model in one day; the final run fell back to the smaller model for 18 of 26 cold requests.
4. **Accuracy at scale.** The official data has one reference sample and no gold labels.

## Known limitations

See `metrics.md` §6 and `HARDENING_REPORT.md` (H15–H19 cover the official data). In brief: knowledge limited
to the 20 official screen/display articles; non-English complaints are not translated; one worker saturates at
about 100 req/s for exact hits and about 29 req/s for semantic hits; free-tier quotas bound live throughput.

## Reproducibility

```bash
make install && make indexes && make test           # offline: 390 tests pass + 1 documented strict xfail
cp .env.example .env                                 # set GEMINI_API_KEY or GROQ_API_KEY (+ LLM_PROVIDER)
make serve                                           # API on :8000, demo page on /demo
make prewarm calibrate                               # build the cache, choose the threshold (calibration split only)
make paraphrases                                     # fresh held-out paraphrases after the threshold is frozen
make benchmark ablation stress hostile metrics       # live evaluation → artifacts/reports → metrics.md
make docker-build && make docker-run ENV_FILE=.env   # container healthy in ~5 s
```

The Groq configuration used for the reported numbers:

```bash
LLM_PROVIDER=groq LLM_MODEL=openai/gpt-oss-120b LLM_FALLBACK_MODELS=openai/gpt-oss-20b \
LLM_ENRICHMENT_MODEL=openai/gpt-oss-20b LLM_MAX_OUTPUT_TOKENS=2048
```

## Model, provider and configuration requirements

* **LLM provider.**
  * Gemini needs `GEMINI_API_KEY`.
  * Groq needs `GROQ_API_KEY`.
  * Without a key, the service serves cache hits and grounded rules fallbacks, and `/health` reports the model
    as not ready.
* **Embeddings.** `BAAI/bge-small-en-v1.5` runs locally through fastembed and is baked into the Docker image.
* **Configuration.** Every setting is an environment variable: `.env.example`, `app/core/config.py`.
* **Secrets.** Read only from the environment; never logged or committed.
