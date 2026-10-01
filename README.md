# Smart Guided Troubleshooting Engine

A FastAPI service that turns a vague Galaxy device complaint into a validated, grounded, one-tap troubleshooting
plan: canonical intent → grounded SIIS steps → one action per Settings screen → exact catalog deeplink →
safe ordering (critical last) → schema-validated JSON → semantic cache. Built for Samsung PRISM
*Theme 2 — Troubleshooting* (problem statement: `AUDIT.md` summarises it and every interpretation made).

The model is an **assistant, not the controller**. It reads messy language and extracts structure from the
supplied reference text. Code does the rest: retrieval, exact-screen resolution, parent-menu protection, catalog
URI selection, grouping, sequencing, URL scrubbing, validation, caching, cost and latency accounting.

> **Dataset.** The official starter assets (`queries.json`, `siis_responses.json`, `deeplinks.json` with ~575
> entries, `samples/`) were not available when this was built. Everything runs on a clearly labelled synthetic
> development fixture in `data/dev_fixtures/` (110 catalog entries, 32 queries over Battery / Display / Camera /
> Performance, 5 reference samples, gold labels). To use the official data, copy the files into `data/official/`
> (same names, `samples/` as a sub-folder). The loaders accept several layouts, and `DATA_DIR` overrides the choice.
> Then re-run `make indexes prewarm calibrate paraphrases evaluate`. Gold-label accuracy needs an `eval/gold.json`;
> without one, accuracy is reported as *not measured*.

## Deliverables

| File | What it is |
| :--- | :--- |
| `results.jsonl` | One `POST /v1/troubleshoot` response body per query of `queries.json` (live model, full pipeline) |
| `metrics.md` | Appendix C report, rendered from the measured JSON in `artifacts/reports/` (no hand-typed numbers) |
| `HARDENING_REPORT.md` | Hostile evaluation: every issue found, reproduction, root cause, fix, regression test |
| `REQUIREMENTS_TRACEABILITY.md` | Requirement → implementation → test → status → evidence |
| `FINAL_REVIEW.md` | Production-readiness review: targets vs measured values, limitations, commands |
| `AUDIT.md` | Phase 0 audit of the problem statement, assets and interpretation decisions |
| `docs/Smart_Troubleshooting_Engine.pptx` | Presentation (11 slides, every number generated from the reports) |
| `docs/demo.webm` | Demo video (56 s): a real browser driving the live API through `/demo`: exact hit, paraphrase hit, live extraction with URL/prompt-injection scrubbing, out-of-scope fallback, 422 |
| `AI_DISCLOSURE.md` | How AI was used to build the project, which models run inside it, and which data is AI-generated |

## Results at a glance (live model, real HTTP; full detail in `metrics.md`)

| Check | Target | Measured |
| :--- | :--- | :--- |
| Schema-valid outputs | ≥ 99% | 100% (447/447) |
| Goal / title / description rules | ≥ 95% | 100% |
| URL leaks | 0 | 0 |
| Deeplinks found verbatim in the catalog | 100% | 100% |
| Auto actions with a valid deeplink | ≥ 90% | 91.5% |
| P95 exact / unseen-paraphrase cache hit | ≤ 300 ms | 10 ms / 24 ms |
| P95 cold full pipeline | ≤ 8 s | 2.55 s |
| Unseen paraphrases served the correct plan | ≥ 80% | 87.5% (4/128 wrong-plan hits: known limitation) |
| Step accuracy / deeplink relevance | 0–3 / 0–2 | 2.902 / 1.896 |

Measured on the synthetic dev fixture with Groq `openai/gpt-oss-120b`; see `FINAL_REVIEW.md` for what is and is
not met.

## Pipeline

```
POST /v1/troubleshoot {query, siis_response?}
 ├─ [0] query enrichment      app/services/query_enrichment.py   normalise, typo-fix (domain words only), intent
 │                                                               families/qualifiers, canonical query, multi-intent split
 ├─ [3] cache lookup          app/cache/semantic.py              exact key → semantic (bge-small, calibrated threshold,
 │                                                               concept/qualifier/domain gates, scope isolation,
 │                                                               re-validation on every hit) → HIT: return, no model call
 ├─ source selection          app/services/siis_retrieval.py     request SIIS text, else BM25+dense KB retrieval,
 │                                                               else contexts: [] + fallback "no_siis_context"
 ├─ [1] structure extraction  app/services/structure_extraction.py  model (JSON schema) → coerce → bounded repair →
 │                                                               grounding check (app/services/grounding.py) →
 │                                                               fragment merge / bundle split (action_grouping.py);
 │                                                               nothing grounded → contexts: [] + "no_match"
 ├─ [2] deeplink mapping      app/services/deeplink_mapping.py   BM25 + dense hybrid retrieval → exact target-screen
 │                                                               resolver (app/retrieval/reranker.py): primary control,
 │                                                               parent-menu penalty, ambiguity fallback; URI always
 │                                                               copied from the catalog registry object
 ├─ sequencing                app/services/sequencing.py         settings → corrective → optimise → manual → critical
 │                                                               (critical ranked restart < safe mode < … < factory reset)
 ├─ repair + final gate       app/validation/*                   schema.py + business rules + URL scan + catalog gate
 └─ cache write               only validated, model-path plans (rules-fallback plans are served, never cached)
```

## Quick start

```bash
make install                                   # Python 3.11+; FastAPI, pydantic, httpx, numpy, fastembed
cp .env.example .env && $EDITOR .env           # set GEMINI_API_KEY, or LLM_PROVIDER=groq + GROQ_API_KEY (never commit keys)
make indexes                                   # BM25 + dense indexes → artifacts/indexes/
make test                                      # offline suite: unit, integration, regression, adversarial
make serve                                     # http://localhost:8000
                                               # demo UI: http://localhost:8000/demo
```

```bash
curl -s localhost:8000/health                  # {"status":"ok"} once catalog, indexes, cache and model are ready
curl -s localhost:8000/v1/troubleshoot -H 'content-type: application/json' \
     -d '{"query":"phone swipe gestures wrong direction after app install"}'
```

The committed pre-warmed plans (`artifacts/cache/prewarm_plans.jsonl`) are imported at start-up, so the queries of
`queries.json` and their paraphrases are served from the cache without a model call.

### Docker

```bash
make docker-build                              # python:3.11-slim; embedding model and indexes baked into the image
make docker-run ENV_FILE=.env                  # non-root, HEALTHCHECK on /health, port 8000
```

If Docker Hub rate-limits the base image, pass another registry:
`docker build --build-arg BASE_IMAGE=mirror.gcr.io/library/python:3.11-slim -t samsung-prism-troubleshooter .`

## API

`POST /v1/troubleshoot` takes `{"query": str, "siis_response": str | null}`. It returns HTTP 200 with the body
of PDF Appendix B: `query`, 8–10 `query_variations`, `response.contexts[]` (schema.py `Goal` objects) and `meta`
(`latency_ms`, `cache_hit`, `model`, `cost_usd`, and `fallback` when `contexts` is empty).

* The fallbacks are `no_siis_context` (nothing to ground on) and `no_match` (the reference text has no viable solution).
* Response headers: `X-Cache` (`HIT-EXACT` / `HIT-SEMANTIC` / `MISS`), `X-Latency-Ms`, `X-Model-Calls`,
  `X-Tokens-Input`, `X-Tokens-Output`, `X-Cost-USD`, `X-Request-ID`.
* Errors are JSON `{"error": {"code", "message", "request_id"}}`:
  * 422: `empty_query`, `query_too_long`, `siis_response_too_long`, `invalid_request`, `malformed_json`
  * 413: `payload_too_large` (body over 1 MB)
  * 502: `plan_validation_failed`
  * 503: `extraction_unavailable`, `retrieval_unavailable`, `service_unavailable`

`GET /health` returns `{"status": "ok"}` (200) only when the catalog, vector indexes, embedding model, cache
and model connection are all ready; otherwise it returns 503 with the failing components. `GET /health/details`
shows the component checks, the dataset label, the number of cached plans and the model/embedding identifiers.

## Evaluation (live model; writes `artifacts/reports/*.json`, then `metrics.md`)

```bash
export ENV_FILE=/path/to/gemini.env
make prewarm        # build + validate plans for queries.json, export artifacts/cache/prewarm_plans.jsonl
make calibrate      # semantic threshold on the calibration split (no model calls) → cache_calibration.json
make paraphrases    # fresh held-out paraphrases from a different model, after the threshold is frozen
make benchmark      # real HTTP: cold ×2 rounds, samples, exact hits, paraphrases, negatives, edge cases
make ablation       # full-LLM vs hybrid BM25+dense vs pure-rules deeplink mapping (end to end + mapping only)
make stress         # concurrency 1/8/32/64 on hits, mixed load, identical burst, concurrent cold requests
make hostile        # live hostile probes (URL/URI injection, parent menus, poisoning, no source/solution)
make metrics        # render metrics.md from the JSON reports
```

## Configuration

All settings are environment variables (see `.env.example`, with defaults in `app/core/config.py`). The main ones:

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `GEMINI_API_KEY` | — | Model access (required for cold requests; cache hits work without it) |
| `LLM_PROVIDER` | `gemini` | `gemini` or `groq` (OpenAI-compatible; `GROQ_API_KEY`, e.g. `openai/gpt-oss-120b`); `none` serves cache hits and grounded rules fallbacks only |
| `LLM_MODEL` / `LLM_FALLBACK_MODELS` | `gemini-3.1-flash-lite` / `gemini-3.5-flash-lite,gemini-3.6-flash` | Primary model and fail-over chain (429/5xx/404) |
| `LLM_THINKING_LEVEL`, `LLM_HEDGE_AFTER_S`, `LLM_MAX_CONCURRENCY` | `minimal`, `5.0`, `16` | Latency controls and backpressure |
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` | `fastembed` / `BAAI/bge-small-en-v1.5` | Local ONNX embeddings (no network at request time) |
| `SEMANTIC_CACHE_THRESHOLD` / `SEMANTIC_CACHE_THRESHOLD_NO_CONCEPT` | `0.77` / `0.93` | Calibrated by `make calibrate` |
| `CACHE_BACKEND` / `CACHE_PATH` | `sqlite` / `artifacts/cache/cache.sqlite` | Persistent cache (WAL); versioned by catalog fingerprint + pipeline version |
| `DEEPLINK_MAPPER` | `hybrid` | `hybrid`, `bm25`, `dense`, `rules`, `llm` (ablation) |
| `DATA_DIR` | auto | `data/official` when it holds `deeplinks.json`, else `data/dev_fixtures` |

## Layout

```
app/api            routes, request/response models, error envelope        app/main.py  app factory, lifespan
app/core           settings, constants (contract numbers), logging, DI    app/llm      provider abstraction, Gemini, prompts
app/catalog        format-tolerant loaders, catalog registry              app/cache    exact + semantic plan cache, stores
app/retrieval      text, BM25, embeddings, dense, hybrid, UI-path parser, exact target resolver
app/services       enrichment, SIIS retrieval, extraction, grounding, grouping, mapping, sequencing, orchestrator
app/validation     schema, business rules, URL safety, catalog gate, repair          app/evaluation  scoring
scripts/           index build, pre-warm, calibration, benchmarks, ablation, stress, hostile probes, metrics
tests/             unit · integration · regression (samples) · adversarial (attacks 1–14)
data/dev_fixtures  synthetic fixture (NOT OFFICIAL) + eval labels          schema.py  verbatim Appendix A contract
```
