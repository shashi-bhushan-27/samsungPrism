# Final Production-Readiness Review

**Verdict: not "production ready".** Measured on the official data, one mandatory target is clearly not met:
auto actions carrying a valid deeplink (31.5% against ≥ 90%). Cache precision is close but not perfect (1
wrong-plan hit on 80 unseen paraphrases). Every other measured target is met.

The numbers below come from `metrics.md`, rendered from `artifacts/reports/*.json` (official dataset, run
2026-10-02) with the default provider, Google Gemini `gemini-3.1-flash-lite`, which served every cold request.
The pre-warmed plans were produced with the same model. An earlier run on Groq (`openai/gpt-oss-120b`, partly
served by `gpt-oss-20b` after its daily quota ran out) is archived in `artifacts/reports/official_groq/`.

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

## Measured metrics: targets vs actual (official data, Gemini)

| Target | Actual | Status |
| :--- | :--- | :--- |
| Schema-valid lines ≥ 99% | 100% (262/262) | Met |
| Goal / title / description rule compliance ≥ 95% | 100% (3,131 checks); all business rules 100% (25,774 checks) | Met |
| Absolute URL leaks = 0 | 0 | Met |
| Deeplink catalog validity = 100% | 100% (85/85 URIs) | Met |
| Auto actions with a valid actionable deeplink ≥ 90% | 31.5% (29 catalog + 27 placeholder of 178) | **Not met** |
| Cache hit P95 ≤ 300 ms (exact) | 9.6 ms (N = 36) | Met |
| Cache hit P95 ≤ 300 ms (unseen paraphrase) | 38.4 ms (N = 66) | Met |
| Cold full pipeline P95 ≤ 8 s | 2.58 s (N = 26, 0 over 8 s) | Met |
| Cache hit inference cost $0.00 | $0.00 (170 hits, 0 model calls) | Met |
| Cold cost tracked | $0.00118 per query (P95 $0.00179) | Met |
| Semantic hit rate ≥ 80% on unseen paraphrases | 81.25% correct-plan hits (65/80, held-out round 3) | Met |
| No wrong-plan hits (cache precision) | **1/80** | **Not met** |
| Official queries with a grounded plan | 18/20 (2 `no_match`) | reported |
| Reference sample (step accuracy 0–3) | 1.0 (expected: back up data + repair service; produced: two repair-service actions) | reported |
| Deterministic execution | identical plan structure and deeplinks 6/6 with the cache off | Met |
| Edge cases | 17/18 (E04 multi-intent → `no_match`) | **Partial** |
| Negatives (out of scope) | 20/20 `no_siis_context`, 0 cache hits | Met |
| Load | 0 unexpected errors, 0 contract failures up to 64 concurrent requests and a 200-request burst | Met |
| Hostile probes | 19/19 live on the synthetic fixture; 14 attack classes offline (390 offline tests) | Met (not re-run live on official data) |

## Failed or partial targets

1. **Auto actions without a deeplink.** Most auto steps in the official SIIS articles happen outside Settings
   (Quick Settings and Quick Access panels, the Data Transfer and Smart View apps, Camera Pro mode). The catalog has
   no entry for them and no Settings screen can be identified, so no link is attached instead of a guessed one.
   Relabelling them as manual was tried and rejected (H16).
2. **One wrong-plan cache hit** among 80 unseen paraphrases (the 20 official complaints are near neighbours).
3. **Edge case E04** (two unrelated complaints in one message) returned `no_match`.
4. **Accuracy at scale.** The official data has one reference sample and no gold labels.

## Known limitations

See `metrics.md` §6 and `HARDENING_REPORT.md` (H15–H19 cover the official data). In brief: knowledge limited
to the 20 official screen/display articles; non-English complaints are not translated; one worker saturates at
about 16–100 req/s for exact hits and about 19–29 req/s for semantic hits, depending on the host and on cache misses; free-tier quotas bound live throughput.

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

The configuration used for the reported numbers:

```bash
# default provider (reported numbers): LLM_PROVIDER=gemini, GEMINI_API_KEY=...  (LLM_MODEL=gemini-3.1-flash-lite)
# archived Groq run: LLM_PROVIDER=groq LLM_MODEL=openai/gpt-oss-120b LLM_FALLBACK_MODELS=openai/gpt-oss-20b \
#   LLM_ENRICHMENT_MODEL=openai/gpt-oss-20b LLM_MAX_OUTPUT_TOKENS=2048
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
