# Final Production-Readiness Review

**Verdict: not "production ready".** One mandatory target is only partly met: the semantic cache serves 4 wrong
plans on 128 unseen paraphrases. All evidence was measured on a synthetic fixture, because the official datasets
were not supplied. Every other target was measured and met.

The numbers below come from `metrics.md`, which is rendered from `artifacts/reports/*.json`. The live runs
(2026-10-01) used the Groq API: `openai/gpt-oss-120b` for extraction and `openai/gpt-oss-20b` for query
variations. The shipped default provider is Gemini; its free-tier daily quota was exhausted on the evaluation day.

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

## Measured metrics: targets vs actual

| Target | Actual | Status |
| :--- | :--- | :--- |
| Schema-valid lines ≥ 99% | 100% (447/447) | Met |
| Goal / title / description rule compliance ≥ 95% | 100% (5,325 checks); all business rules 100% (44,463 checks) | Met |
| Absolute URL leaks = 0 | 0 | Met |
| Deeplink catalog validity = 100% | 100% (1,024/1,024 URIs) | Met |
| Auto actions with a valid actionable deeplink ≥ 90% | 91.5% (86.6% real catalog links, the rest `dummy_positive`) | Met |
| Cache hit P95 ≤ 300 ms (exact) | 10.0 ms (N = 64) | Met |
| Cache hit P95 ≤ 300 ms (unseen paraphrase) | 24.2 ms (N = 116) | Met |
| Cold full pipeline P95 ≤ 8 s | 2.55 s (N = 40, all model-served) | Met |
| Cache hit inference cost $0.00 | $0.00 (334 hits, 0 model calls) | Met |
| Cold cost tracked | $0.000527 per query (P95 $0.000723) | Met |
| Semantic hit rate ≥ 80% on unseen paraphrases | 87.5% correct-plan hits (112/128, fresh round 2) | Met |
| No wrong-plan hits (cache precision) | **4/128 wrong-plan hits** | **Not met** |
| Step accuracy (0–3) | 2.902 | reported |
| Deeplink relevance (0–2) | 1.896 (63/67 exact, 1 parent, 3 unmatched actions) | reported |
| Deterministic execution | Cached repeats identical. The live model gives an identical plan structure for 5 of 8 repeated inputs | **Partial** |
| Hostile probes | 19/19 live; 14 attack classes offline | Met |
| Edge cases | 18/18 | Met |
| Load | 0 errors, 0 contract failures. P95 ≤ 300 ms up to 8 concurrent requests per worker | Met at ≤ 8 per worker |

## Failed or partial targets

1. **Wrong-plan cache hits (4/128).** "Froze, won't respond to touch or buttons" is subsumed into the
   touchscreen concept. "Phone is completely full" is not recognised as storage. A symptom-less "battery life is
   a joke" matched a sibling battery plan. The issue is tracked as a strict xfail; any fix must be measured on a
   fresh held-out round.
2. **Live-model determinism.** Temperature 0 and a fixed seed do not give byte-identical output from the
   provider. The cache makes repeat answers identical.
3. **Official data.** Not supplied; all accuracy numbers are on a self-authored fixture and are optimistic.

## Known limitations

See `metrics.md` §6 and `HARDENING_REPORT.md`. In brief:

* **Domain coverage.** Only the four fixture domains are covered.
* **Language.** Non-English complaints are not translated.
* **Throughput.** One worker saturates at about 230 req/s for exact hits and about 44 req/s for semantic hits;
  scale out with workers.
* **Provider quotas.** Free-tier quotas bound live throughput (Gemini 500 requests/day per model; Groq 8K
  tokens/min and 200K tokens/day per model).

## Reproducibility

```bash
make install && make indexes && make test           # offline: 361 tests pass + 1 documented strict xfail
cp .env.example .env                                 # set GEMINI_API_KEY or GROQ_API_KEY (+ LLM_PROVIDER)
make serve                                           # API on :8000, demo page on /demo
make prewarm calibrate                               # build the cache, choose the threshold (calibration split only)
make paraphrases                                     # fresh held-out paraphrases after the threshold is frozen
make benchmark ablation stress hostile metrics       # live evaluation → artifacts/reports → metrics.md
make docker-build && make docker-run ENV_FILE=.env   # container healthy in ~5 s
```

The Groq configuration used for the reported numbers:

```bash
LLM_PROVIDER=groq LLM_MODEL=openai/gpt-oss-120b LLM_FALLBACK_MODELS=openai/gpt-oss-20b,qwen/qwen3.8-27b \
LLM_ENRICHMENT_MODEL=openai/gpt-oss-20b
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
