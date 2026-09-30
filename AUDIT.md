# Phase 0 — Repository & Problem Audit

Audit date: 2026-09-30. Source of truth: *Theme 2 Troubleshooting — Smart Guided
Troubleshooting Engine* (9-page PDF, image-only; every page was rendered and read).

## 1. Available input files

| Asset (per PDF §3) | Status in this repository | Action taken |
| :--- | :--- | :--- |
| `schema.py` | **Not supplied as a file**; reproduced verbatim in PDF Appendix A | Transcribed verbatim to `/schema.py` (unmodified contract) |
| `queries.json` | **Missing** (GitHub repo had zero commits; upload contained only the PDF; not in connected Drive) | Loader is format-tolerant; synthetic dev fixture provided |
| `siis_responses.json` | **Missing** | Same |
| `deeplinks.json` (~575 masked URIs) | **Missing** | Same |
| `bixby://dummy_positive` | Documented rule only (PDF §3) | Implemented as reserved constant with documented usage rule |
| `samples/` (5 reference pairs) | **Missing** | Worked example (Appendix B) used as sample #1 of the dev fixture |
| README / Docker / tests / code | None (empty repository) | Built from scratch |

The user confirmed (2026-09-30) to proceed with **clearly labelled synthetic dev
fixtures** (`data/dev_fixtures/`, every file marked `NOT OFFICIAL`). Official
files are dropped into `data/official/` later; one command rebuilds indexes and
regenerates `results.jsonl` / `metrics.md` (see README).

## 2. Schemas and structures (from the PDF)

* `queries.json` — canonical user queries across **Battery, Display, Camera, Performance**.
* `siis_responses.json` — pre-cleaned customer-care reference text (no web URLs or images).
* `deeplinks.json` — ~575 masked in-app Settings deeplinks `bixby://masked/act/...`;
  metadata: `description`, `message`, `qna_description`, control types, toggle validation rules.
* `samples/` — five complete input→output pairs.
* Exact field names/nesting of the three JSON files are **not specified** in the PDF, so the
  loaders accept several shapes (list of records, dict keyed by id/URI, alternate key
  spellings) and report field coverage at load time instead of guessing silently.

## 3. Existing application architecture

None — the repository was empty (no commits, no branches on the remote).

## 4. API contract (PDF §5 + Appendix B)

* `POST /v1/troubleshoot` — body `{"query": str, "siis_response": str (optional)}`.
  If `siis_response` is omitted → semantic lookup against pre-warmed cache entries.
* Response body (Appendix B, also one line of `results.jsonl`):
  `{"query", "query_variations"[8-10], "response": ContextDeeplinkResponse, "meta": {"latency_ms", "cache_hit", "model", "cost_usd"}}`
* `GET /health` — HTTP 200 `{"status": "ok"}` only when the caching layer, model
  connections and vector indexes are fully initialised.
* Pure JSON — never markdown-wrapped, never conversational preamble (PDF §4.2.4).

## 5. Exact schema contract (Appendix A → `/schema.py`)

`BaseDeeplink{deeplink}` → `Deeplink{description, message="", classes=None, originalType=None}`;
`Condition{greater,equal,less}`; `ResultTypes{boolean,integer,str,float}`;
`actionCategory{auto,manual,critical}`; `ValidationDeepLink{key, resultType, condition, value}`;
`StepGroup{steps, validationDeeplink=None, actionableDeeplink=None}`;
`Action{actionName, description, stepGroups, category=manual}`;
`Goal{goal, title, actions, score}`; `ContextDeeplinkResponse{contexts=[]}`.

Field rules (PDF §4.1) — enforced by code in `app/validation/`, **not** by editing `schema.py`:

| Field | Rule |
| :--- | :--- |
| goal | `Follow these steps to perform this <Topic> Troubleshooting` (or `<Topic> Configuration`) |
| title | **2 to 3 words**, sentence case |
| score | float in [0.0, 1.0] |
| actionName | Title Case; exactly one physical screen or feature |
| description | exactly 5–7 words, starts with `It will` |
| steps | imperative UI steps, one physical interaction each, no URLs |
| category | `auto` (deeplink-reachable config), `critical` (restart/reset/update/safe mode — ordered last), `manual` (physical; never an actionable deeplink) |
| actionableDeeplink | verbatim catalog URI (or `bixby://dummy_positive` for an un-indexed valid Settings screen) |
| query_variations | 8–10 distinct paraphrases across registers |

## 6. Existing dependencies / 7. reusable components

None existed. Chosen stack: Python 3.11, FastAPI, Pydantic v2, httpx, NumPy,
fastembed (ONNX, local embeddings), pytest. Gemini (key supplied by the user,
never committed) is the LLM provider behind a swappable `LLMProvider` interface.

## 8. Missing components

Everything (pipeline, retrieval, cache, API, tests, evaluation, Docker) plus the
official datasets (see §1).

## 9. Evaluation requirements (PDF §6 + Appendix C)

Schema-valid lines ≥ 99 %, rule compliance ≥ 95 %, URL leaks = 0, deeplink catalog
validity 100 %, auto actions with valid actionable deeplink ≥ 90 %, step accuracy
0–3, deeplink relevance 0–2, P95 ≤ 300 ms for exact and paraphrase cache hits,
cold P95 ≤ 8 s, semantic paraphrase hit rate ≥ 80 %, tracked cost per query,
three-way ablation (full-LLM mapping vs hybrid BM25+dense vs pure rules), edge cases.

## 10. Conflicts / discrepancies and resolutions

| # | Discrepancy | Resolution |
| :--- | :--- | :--- |
| D1 | Task prompt says title 2–10 words; PDF §4.1 says **2 to 3 words** | Enforce 2–3 (satisfies both); PDF is the contract |
| D2 | Task prompt lists "manual" after critical ops yet requires "critical last"; PDF: critical "must be ordered last" | Critical actions always form the suffix; manual actions are placed immediately before them |
| D3 | `schema.py` carries no field constraints (score range, word counts, …) | Constraints enforced by `app/validation`, `schema.py` left byte-identical to Appendix A |
| D4 | PDF §4.2.3 wants `contexts: []` "with fallback metadata (`"fallback": "no_match"`)", but `ContextDeeplinkResponse` has only `contexts` | `fallback` is emitted inside the envelope `meta` object (`no_match` / `no_siis_context`) |
| D5 | Worked-example step "Optionally toggle on Gesture hint…" begins with an adverb | Imperative validator accepts a leading optional adverbial / conditional clause |
| D6 | Worked example's dummy-positive `description`/`message` are not catalog text | Generated deterministically from the grounded navigation path and passed through the URL gate |
| D7 | Worked example reports `model` on a cache hit | `meta.model` = model that produced the plan; `cost_usd` = 0.0 on hits (no call made) |
| D8 | Task prompt: critical actions carry a deeplink only when the catalog supplies it | Followed: `dummy_positive` is used only for `auto` actions |
| D9 | Worked example uses `gpt-4o-mini` | Provider-agnostic design; Gemini used because that key was supplied |
| D10 | Official datasets absent | Synthetic, labelled dev fixtures; every metric in `metrics.md` states which dataset produced it |
