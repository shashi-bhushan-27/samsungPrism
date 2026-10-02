# Smart Guided Troubleshooting Engine

A FastAPI service that turns a vague phone or tablet complaint into a validated, grounded, one-tap troubleshooting
plan: canonical intent → grounded SIIS steps → one action per Settings screen → exact catalog deeplink →
safe ordering (critical last) → schema-validated JSON → semantic cache. Built for Samsung PRISM
*Theme 2 — Troubleshooting* (problem statement: `AUDIT.md` summarises it and every interpretation made).

The model is an **assistant, not the controller**. It reads messy language and extracts structure from the
supplied reference text. Code does the rest: retrieval, exact-screen resolution, parent-menu protection, catalog
URI selection, grouping, sequencing, URL scrubbing, validation, caching, cost and latency accounting.

> **Dataset.** The service runs on the **official starter data** in `data/official/` (picked automatically when
> `deeplinks.json` is present; `DATA_DIR` overrides it): 20 customer complaints (`input.txt` → `queries.json`),
> their SIIS answers (`siis_responses.json`, `{title, content}` objects), the 578-entry masked deeplink catalog
> (`voiceassist://`, including its own `dummy_positive` placeholder), `schema.py` and one reference sample.
> The synthetic development fixture used while building the system is kept in `data/dev_fixtures/`, and its
> earlier report in `artifacts/reports/dev_fixtures/`. The official data has no gold labels, so step accuracy
> is scored only against the reference sample.

## Team

**ResolveAI** (VITV) · PRISM GenAI Hackathon 2026, Theme 2: Troubleshooting

| Member | Email |
| :--- | :--- |
| Shashi Bhushan | shashibhushan.vijay2022@vitstudent.ac.in |
| Astha Doshi | astha.doshi2023@vitstudent.ac.in |
| Himangi Khanduri | himangi.khanduri2023@vitstudent.ac.in |

## Deliverables

| File | What it is |
| :--- | :--- |
| `results.jsonl` | One `POST /v1/troubleshoot` response body per query of `queries.json` (live model, full pipeline) |
| `metrics.md` | Appendix C report, rendered from the measured JSON in `artifacts/reports/` (no hand-typed numbers) |
| `HARDENING_REPORT.md` | Hostile evaluation: every issue found, reproduction, root cause, fix, regression test |
| `REQUIREMENTS_TRACEABILITY.md` | Requirement → implementation → test → status → evidence |
| `FINAL_REVIEW.md` | Production-readiness review: targets vs measured values, limitations, commands |
| `AUDIT.md` | Phase 0 audit of the problem statement, assets and interpretation decisions |
| `docs/VITV_ResolveAI_Submission.pptx` | Submission deck on the official template; every number from the reports |
| `docs/LangAI3.0_AI_Disclosure.docx` | Filled AI-usage disclosure form (date and sign-off left for the team representative) |
| Demo video | https://youtu.be/eRBRi4PisPU (32 s; file: `docs/ResolveAI_demo.mp4`) |
| `docs/demo.webm` | Earlier API walkthrough video (56 s, recorded on the synthetic fixture before the official data arrived): a real browser driving the live API through `/demo`: exact hit, paraphrase hit, live extraction with URL/prompt-injection scrubbing, out-of-scope fallback, 422 |
| `AI_DISCLOSURE.md` | How AI was used to build the project, which models run inside it, and which data is AI-generated |

## Results at a glance (official data, live model, real HTTP; full detail in `metrics.md`)

| Check | Target | Measured |
| :--- | :--- | :--- |
| Schema-valid outputs | ≥ 99% | 100% (262/262) |
| Goal / title / description rules | ≥ 95% | 100% (3,131 checks) |
| URL leaks | 0 | 0 |
| Deeplinks found verbatim in the catalog | 100% | 100% (85/85) |
| Auto actions with a valid deeplink | ≥ 90% | **31.5% — not met** (most official auto steps are outside Settings; see `metrics.md` §6) |
| P95 exact / unseen-paraphrase cache hit | ≤ 300 ms | 9.6 ms / 38.4 ms |
| P95 cold full pipeline | ≤ 8 s | 2.58 s |
| Unseen paraphrases served the correct plan | ≥ 80% | 81.25% (65/80; 1 wrong-plan hit) |
| Official queries with a grounded plan | — | 18/20 (2 `no_match`) |
| Reference sample step accuracy (0–3) | — | 1.0 |
| Edge cases / negatives | — | 17/18 / 0 false cache hits on 20 out-of-scope queries |
| Determinism (same input twice, cache off) | — | identical plan 6/6 |

Measured with the default provider, Google Gemini `gemini-3.1-flash-lite` (every cold request served by it). An
earlier run of the same code and data on Groq `openai/gpt-oss-120b` / `gpt-oss-20b` is archived in
`artifacts/reports/official_groq/`. See `FINAL_REVIEW.md` for what is and is not met.

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

## Reproducing the prototype

Everything below was run on a clean clone. Commands are given for Linux/macOS (bash) and Windows (PowerShell);
`make` targets are shortcuts for the same commands.

### 1. Requirements

| Need | Why | Notes |
| :--- | :--- | :--- |
| Python 3.11 or newer | runs the service, tests and scripts | `python --version` |
| Internet access **once** | `pip install` and the first download of the local embedding model (`BAAI/bge-small-en-v1.5`, about 130 MB, from Hugging Face) | after that, requests run without network except the optional model API |
| About 1.5 GB free disk | Python packages, embedding model, indexes | |
| An LLM API key (optional) | only for complaints that are **not** already in the cache | see step 6; Google Gemini (default) or Groq, both have free tiers |
| Docker (optional) | container run (step 9) | |

### 2. Get the code

```bash
git clone https://github.com/shashi-bhushan-27/samsungPrism.git
cd samsungPrism
git checkout PRISM_GENAI_HACKATHON_Y2026        # optional: the submitted version
```

### 3. Install

```bash
# Linux / macOS
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt          # or: make install
```

```powershell
# Windows (PowerShell)
py -3.11 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
```

### 4. Configure (optional)

```bash
cp .env.example .env          # Windows: copy .env.example .env
```

Leave `GEMINI_API_KEY` empty to run without a model (step 6), or put your key there. Never commit `.env`.
To use Groq instead, set `LLM_PROVIDER=groq` and `GROQ_API_KEY=...` (the Groq settings used for the archived run
are listed in `.env.example`).

### 5. Run the test suite (offline, no key needed)

```bash
python -m pytest -q          # or: make test
```

Expected: `391 passed, 1 xfailed`. The tests use a scripted fake model and a hashing embedder, so they need no
network and no key. The single expected failure (`xfail`) is a documented open issue (`HARDENING_REPORT.md`).

### 6. Start the service

```bash
python scripts/build_indexes.py                                  # optional: the server builds them on first start
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000      # or: make serve
```

The first start downloads the embedding model and builds the BM25 and dense indexes (about 30–60 s); later
starts take about 5 s. Then open:

* `http://localhost:8000/demo` — browser demo page (type a complaint, see the validated plan)
* `http://localhost:8000/health` — `{"status": "ok"}` when ready
* `http://localhost:8000/docs` — interactive OpenAPI page

On start-up the committed pre-warmed plans (`artifacts/cache/prewarm_plans.jsonl`, 18 of the 20 official
complaints) are loaded into the cache.

### 7. Send requests

```bash
# an official complaint: served from the cache (X-Cache: HIT-EXACT), no model call
curl -s -i http://localhost:8000/v1/troubleshoot -H 'content-type: application/json' \
  -d '{"query":"My smartphone'"'"'s screen is completely cracked, it'"'"'s a total crack and I can'"'"'t use the device."}'

# a paraphrase of it: semantic cache hit (X-Cache: HIT-SEMANTIC)
curl -s http://localhost:8000/v1/troubleshoot -H 'content-type: application/json' \
  -d '{"query":"my phone screen is totally shattered and unusable"}'

# a new complaint with its own SIIS reference text (cold path: model, or rules fallback without a key)
curl -s http://localhost:8000/v1/troubleshoot -H 'content-type: application/json' \
  -d '{"query":"my screen is too dim","siis_response":{"title":"Screen too dim","content":"Open Settings, tap Display, and then turn off Adaptive brightness."}}'
```

```powershell
# Windows (PowerShell)
$body = @{ query = "my phone screen is totally shattered and unusable" } | ConvertTo-Json
Invoke-RestMethod -Uri http://localhost:8000/v1/troubleshoot -Method Post -ContentType "application/json" -Body $body |
  ConvertTo-Json -Depth 12
```

`siis_response` may be plain text or an object `{"title", "content"}` as in the official `siis_responses.json`.
Every response is validated against `schema.py` and the business rules before it is returned.

### Running without an API key

No key is needed to run, test or demonstrate the prototype. Without a key (or with `LLM_PROVIDER=none`) the
service starts in **no-model mode**: `/health` returns `{"status": "ok", "mode": "no_model", "llm": "not_configured"}`
and the server log says so.

| Request | Without a key | With a key |
| :--- | :--- | :--- |
| One of the 20 official complaints (18 have a cached plan) | full validated plan from the cache, 0 model calls, $0 | same |
| A paraphrase of a cached complaint | semantic cache hit, same plan | same |
| A new complaint **with** its own `siis_response` text | grounded plan from the deterministic rules extractor (lower quality, not cached) | model extraction, validated and cached |
| A new complaint **without** SIIS text, not in the cache, that matches a knowledge-base article | HTTP 503 `extraction_unavailable` (nothing is invented) | model extraction from that article |
| An out-of-scope complaint (nothing in the knowledge base), e.g. Bluetooth headphones | `contexts: []` with `fallback: no_siis_context` | same |
| `pytest`, `/demo`, `/health`, Docker | work | work |

Only the structure-extraction step and the query variations use the model. Retrieval, deeplink mapping,
ordering, validation and the cache run locally. A configured key that is wrong or out of quota still makes
`/health` return 503, so a broken setup is visible.

### 8. Reproduce the reported numbers (`results.jsonl`, `metrics.md`)

These scripts call the live model and start real HTTP servers. Put the key in a file outside the repository
(for example `~/keys/gemini.env` containing `GEMINI_API_KEY=...`); `--env-file` values override the shell.

```bash
export ENV_FILE=~/keys/gemini.env            # Windows: $env:ENV_FILE="C:\keys\gemini.env"; use the python commands
make prewarm      # python scripts/prewarm_cache.py --env-file $ENV_FILE --pace 8 --retries 3 --fresh
                  #   → artifacts/cache/prewarm_plans.jsonl, artifacts/reports/prewarm_report.json   (~5 min)
make benchmark    # python scripts/run_benchmarks.py --env-file $ENV_FILE --pace-cold 8 --determinism-subset 6
                  #   → results.jsonl, artifacts/reports/benchmark.json, results_all.jsonl          (~10 min)
make stress       # python scripts/stress_test.py --env-file $ENV_FILE --skip-cold
                  #   → artifacts/reports/stress.json                                               (~10 min)
make metrics      # python scripts/render_metrics.py  → metrics.md (no model calls)
```

* About 150 model requests in total, within the Gemini free tier. Live models are not perfectly deterministic,
  so individual plans and the last digits of the metrics vary between runs; `metrics.md` states the models that
  actually served each request.
* Groq: add `LLM_PROVIDER=groq LLM_MODEL=openai/gpt-oss-120b LLM_FALLBACK_MODELS=openai/gpt-oss-20b
  LLM_ENRICHMENT_MODEL=openai/gpt-oss-20b LLM_MAX_OUTPUT_TOKENS=2048` and use `--pace 25 --pace-cold 25`
  (free tier: 8K tokens/minute and 200K tokens/day per model).
* Held-out paraphrases (`make paraphrases`) and threshold calibration (`DATA_DIR=data/dev_fixtures make calibrate`)
  are already committed; regenerate them only to repeat that part of the study.
* The synthetic development fixture can be used instead of the official data with `DATA_DIR=data/dev_fixtures`.
* The submission deck and the disclosure form are generated from the reports:
  `python scripts/fill_submission_deck.py <template.pptx> docs/VITV_ResolveAI_Submission.pptx . [screenshot.png]`
  and `python scripts/fill_disclosure_form.py <template.docx> docs/LangAI3.0_AI_Disclosure.docx`.

### 9. Docker

```bash
docker build -t samsung-prism-troubleshooter .                    # embedding model and indexes baked in
docker run --rm -p 8000:8000 samsung-prism-troubleshooter         # no-model mode
docker run --rm -p 8000:8000 --env-file .env samsung-prism-troubleshooter   # with your key
```

The image runs as a non-root user with a `HEALTHCHECK` on `/health`. If Docker Hub rate-limits the base image:
`docker build --build-arg BASE_IMAGE=mirror.gcr.io/library/python:3.11-slim -t samsung-prism-troubleshooter .`

### 10. Troubleshooting

| Symptom | Fix |
| :--- | :--- |
| `[WinError 10048]` / "address already in use" | another program uses port 8000: add `--port 8001` |
| First start is slow or fails offline | the embedding model downloads once; run with internet access, or set `EMBEDDING_CACHE_DIR` to a folder that already holds it |
| `/health` returns 503 with `"llm": false` | a key is configured but rejected or out of quota; fix the key or remove it to run in no-model mode |
| HTTP 503 `extraction_unavailable` | the complaint is not cached and no model is available; send `siis_response` text or configure a key |
| `cache_plans: 0` in `/health/details` | the pre-warmed plans did not match the catalog; re-clone (line endings are normalised by `.gitattributes`) or run `make prewarm` |
| HTTP 429 during the scripts | provider rate limit: raise `--pace` / `--pace-cold`, or wait for the daily quota to reset |

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

`GET /health` returns 200 when the catalog, vector indexes, embedding model and cache are ready, and the model
connection too when a model is configured (`{"status": "ok"}`); without a configured model it returns
`{"status": "ok", "mode": "no_model", "llm": "not_configured"}`. Otherwise it returns 503 with the failing
components. `GET /health/details`
shows the component checks, the dataset label, the number of cached plans and the model/embedding identifiers.

## Evaluation scripts (live model; write `artifacts/reports/*.json`, then `metrics.md`)

```bash
make prewarm        # build + validate plans for queries.json, export artifacts/cache/prewarm_plans.jsonl
make calibrate      # semantic threshold on the calibration split (no model calls; DATA_DIR=data/dev_fixtures)
make paraphrases    # fresh held-out paraphrases from a different model, after the threshold is frozen
make benchmark      # real HTTP: cold requests, reference sample, exact hits, paraphrases, negatives, edge cases
make ablation       # full-LLM vs hybrid BM25+dense vs pure-rules deeplink mapping (needs gold labels)
make stress         # concurrency 1/8/32/64 on hits, mixed load, identical burst
make hostile        # live hostile probes (URL/URI injection, parent menus, poisoning, no source/solution)
make metrics        # render metrics.md from the JSON reports
```

## Configuration

All settings are environment variables (see `.env.example`, with defaults in `app/core/config.py`). The main ones:

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `GEMINI_API_KEY` | — | Model access (optional: without it the service runs in no-model mode, see above) |
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
