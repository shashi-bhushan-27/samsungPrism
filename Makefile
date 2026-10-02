# Reproducible commands. Secrets come from an env file (never committed): ENV_FILE=/path/to/gemini.env make benchmark
PY ?= python3
ENV_FILE ?= .env
PACE ?= 8
IMAGE ?= samsung-prism-troubleshooter
PORT ?= 8000

.PHONY: install fixtures indexes test test-unit test-integration test-regression test-adversarial serve \
        prewarm calibrate paraphrases benchmark ablation stress hostile metrics evaluate docker-build docker-run

install:
	$(PY) -m pip install -r requirements.txt -r requirements-dev.txt

fixtures:            ## regenerate the synthetic dev fixture (NOT official data) and self-check it
	$(PY) scripts/generate_dev_fixtures.py

indexes:             ## build BM25 + dense indexes for the catalog and the SIIS knowledge base
	$(PY) scripts/build_indexes.py

test:                ## every offline test (no network, scripted model)
	$(PY) -m pytest -q

test-unit:
	$(PY) -m pytest -q tests/unit

test-integration:
	$(PY) -m pytest -q tests/integration

test-regression:     ## reference samples reproduced by the pipeline
	$(PY) -m pytest -q tests/regression

test-adversarial:    ## hostile hardening suite (HARDENING_REPORT.md)
	$(PY) -m pytest -q tests/adversarial

serve:
	$(PY) -m uvicorn app.main:app --host 0.0.0.0 --port $(PORT) --workers 1

prewarm:             ## live model: build validated plans for queries.json, export artifacts/cache/prewarm_plans.jsonl
	$(PY) scripts/prewarm_cache.py --env-file $(ENV_FILE) --pace $(PACE) --retries 3 --fresh

calibrate:           ## choose the semantic-cache threshold on the calibration split (no model calls)
	$(PY) scripts/calibrate_cache.py

paraphrases:         ## live model: fresh held-out paraphrases (after the threshold is frozen)
	$(PY) scripts/generate_heldout_paraphrases.py --env-file $(ENV_FILE)

benchmark:           ## live model, real HTTP: results.jsonl + artifacts/reports/benchmark.json
	$(PY) scripts/run_benchmarks.py --env-file $(ENV_FILE) --pace-cold $(PACE) --determinism-subset 6

ablation:            ## live model: LLM vs hybrid vs rules deeplink mapping → artifacts/reports/ablation.json
	$(PY) scripts/run_ablation.py --env-file $(ENV_FILE)

stress:              ## real HTTP under concurrency → artifacts/reports/stress.json
	$(PY) scripts/stress_test.py --env-file $(ENV_FILE) --skip-cold

hostile:             ## live hostile probes (URL/URI injection, poisoning, no source/solution) → hostile.json
	$(PY) scripts/hostile_probe.py --env-file $(ENV_FILE)

metrics:             ## render metrics.md from the measured JSON reports
	$(PY) scripts/render_metrics.py

evaluate: benchmark ablation stress hostile metrics

docker-build:
	docker build -t $(IMAGE) .

docker-run:
	docker run --rm -p $(PORT):8000 $(if $(wildcard $(ENV_FILE)),--env-file $(ENV_FILE),) $(IMAGE)
