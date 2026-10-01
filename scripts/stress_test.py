#!/usr/bin/env python3
"""End-to-end stress test over real HTTP (PDF §8 Phase 4) → artifacts/reports/stress.json.

A real uvicorn server (1 worker, pre-warmed cache, cache writes off so the workload cannot change the cache)
is driven by an async client at several concurrency levels:
  exact_hits        canonical queries (exact cache hits)
  semantic_hits     held-out LLM paraphrases (semantic cache path; the few misses run the pipeline)
  mixed             70% exact / 20% paraphrase / 5% out-of-scope (fast fallback) / 5% invalid (HTTP 422)
  same_query_burst  one identical query fired concurrently (thundering herd on a hit)
  cold_burst        unique queries with SIIS text on a cache-disabled server, fired concurrently
Every 200 body is re-validated (schema + business rules + URL scan). Startup time and RSS/CPU are sampled.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from collections import Counter

import httpx

import _common
from bench_lib import Server, percentiles
from app.catalog.loaders import link_queries_to_siis, load_catalog, parse_queries, parse_siis, read_json
from app.catalog.registry import CatalogRegistry
from app.core.config import Settings
from app.validation.business_rules import validate_envelope
from app.validation.url_safety import scan_payload


async def fire(base: str, payloads: list[dict], concurrency: int, registry: CatalogRegistry, expect_422: set[int]) -> dict:
    sem = asyncio.Semaphore(concurrency)
    lat, statuses, caches, bad_contract, leaks, model_calls = [], Counter(), Counter(), 0, 0, 0
    unexpected = []
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=120.0, limits=limits, trust_env=False) as client:

        async def one(i: int, payload: dict):
            nonlocal model_calls
            async with sem:
                t0 = time.perf_counter()
                try:
                    r = await client.post(f"{base}/v1/troubleshoot", json=payload)
                except httpx.HTTPError as exc:
                    statuses[f"client_error:{type(exc).__name__}"] += 1
                    return
                ms = (time.perf_counter() - t0) * 1000
            statuses[r.status_code] += 1
            caches[r.headers.get("x-cache", f"HTTP_{r.status_code}")] += 1
            model_calls += int(r.headers.get("x-model-calls", 0) or 0)
            if r.status_code == 200:
                lat.append(ms)
                raw.append(r.content)  # validated after the run so the client never stalls the event loop
            elif not (r.status_code == 422 and i in expect_422):
                unexpected.append({"status": r.status_code, "body": r.text[:200]})

        raw: list[bytes] = []
        t0 = time.perf_counter()
        await asyncio.gather(*(one(i, p) for i, p in enumerate(payloads)))
        wall = time.perf_counter() - t0
    for content in raw:
        body = json.loads(content)
        if not validate_envelope(body, registry).ok:
            bad_contract += 1
        leaks += len(scan_payload(body))
    n = len(payloads)
    return {"requests": n, "concurrency": concurrency, "wall_s": round(wall, 3), "throughput_rps": round(n / wall, 1),
            "latency_ms_200": percentiles(lat), "statuses": {str(k): v for k, v in statuses.items()},
            "cache": dict(caches), "unexpected_errors": len(unexpected), "unexpected_examples": unexpected[:3],
            "contract_failures": bad_contract, "url_leaks": leaks,
            "model_calls_per_request": round(model_calls / n, 4)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--port", type=int, default=8031)
    ap.add_argument("--port-cold", type=int, default=8032)
    ap.add_argument("--levels", default="1,8,32,64")
    ap.add_argument("--n", type=int, default=320)
    ap.add_argument("--cold-n", type=int, default=16)
    ap.add_argument("--cold-concurrency", type=int, default=8)
    ap.add_argument("--skip-cold", action="store_true")
    args = ap.parse_args()
    _common.load_env_file(args.env_file)
    s = Settings()
    data = s.resolved_data_dir
    entries, issues, fp = load_catalog(data / "deeplinks.json")
    registry = CatalogRegistry(entries, fingerprint=fp, issues=issues)
    queries, _ = parse_queries(read_json(data / "queries.json"))
    docs, _ = parse_siis(read_json(data / "siis_responses.json"))
    links, _ = link_queries_to_siis(queries, docs)
    doc_text = {d.id: d.text for d in docs}
    rounds = sorted((data / "eval").glob("paraphrases_llm_heldout*.json"))  # newest held-out round last
    para = json.loads(rounds[-1].read_text())["items"]
    negatives = json.loads((data / "eval" / "negatives.json").read_text())["unrelated"]
    rng = random.Random(7)
    levels = [int(x) for x in args.levels.split(",")]
    out: dict = {"dataset": s.dataset_label, "server": "uvicorn, 1 worker", "levels": levels, "requests_per_run": args.n}

    cache_path = s.artifacts_dir / "tmp" / "stress_cache.sqlite"
    for p in cache_path.parent.glob("stress_cache.sqlite*"):
        p.unlink()
    srv = Server(args.port, {"CACHE_PATH": str(cache_path), "CACHE_WRITE_ENABLED": "false", "LOG_LEVEL": "WARNING"})
    srv.start()
    out["startup_s"] = round(srv.startup_s, 2)
    with httpx.Client(timeout=30.0, trust_env=False) as c:
        t0 = time.perf_counter()
        r = c.post(f"{srv.url}/v1/troubleshoot", json={"query": queries[0].text})
        out["first_request_after_healthy"] = {"ms": round((time.perf_counter() - t0) * 1000, 2),
                                              "cache": r.headers.get("x-cache"), "status": r.status_code}
    out["rss_mb_idle"] = srv.rss_mb()
    cpu0 = srv.cpu_seconds()
    runs = {}

    def exact(n):
        return [{"query": rng.choice(queries).text} for _ in range(n)]

    def semantic(n):
        return [{"query": rng.choice(para)["text"]} for _ in range(n)]

    def mixed(n):
        payloads, bad = [], set()
        for i in range(n):
            x = rng.random()
            if x < 0.70:
                payloads.append({"query": rng.choice(queries).text})
            elif x < 0.90:
                payloads.append({"query": rng.choice(para)["text"]})
            elif x < 0.95:
                payloads.append({"query": rng.choice(negatives)})
            else:
                payloads.append({"query": "   "})
                bad.add(i)
        return payloads, bad

    for level in levels:
        runs[f"exact_hits@{level}"] = asyncio.run(fire(srv.url, exact(args.n), level, registry, set()))
        print(json.dumps({"run": f"exact_hits@{level}", **runs[f"exact_hits@{level}"]["latency_ms_200"]}), flush=True)
        runs[f"semantic_hits@{level}"] = asyncio.run(fire(srv.url, semantic(args.n), level, registry, set()))
        print(json.dumps({"run": f"semantic_hits@{level}", **runs[f"semantic_hits@{level}"]["latency_ms_200"]}), flush=True)
        payloads, bad = mixed(args.n)
        runs[f"mixed@{level}"] = asyncio.run(fire(srv.url, payloads, level, registry, bad))
        print(json.dumps({"run": f"mixed@{level}", **runs[f"mixed@{level}"]["latency_ms_200"]}), flush=True)
    burst = [{"query": queries[0].text}] * 200
    runs["same_query_burst@200"] = asyncio.run(fire(srv.url, burst, 200, registry, set()))
    out["rss_mb_after_load"] = srv.rss_mb()
    cpu1 = srv.cpu_seconds()
    out["server_cpu_s_during_load"] = round(cpu1 - cpu0, 2) if cpu0 is not None and cpu1 is not None else None
    srv.stop()

    if not args.skip_cold:
        cold = Server(args.port_cold, {"CACHE_READ_ENABLED": "false", "CACHE_WRITE_ENABLED": "false",
                                       "CACHE_BACKEND": "memory", "LOG_LEVEL": "WARNING"}).start()
        payloads = [{"query": q.text, "siis_response": doc_text[links[q.id]]} for q in queries[: args.cold_n]]
        runs[f"cold_burst@{args.cold_concurrency}"] = asyncio.run(
            fire(cold.url, payloads, args.cold_concurrency, registry, set()))
        out["cold_server_rss_mb"] = cold.rss_mb()
        cold.stop()
        out["cold_server_log_warnings"] = sum(1 for line in cold.log_path.read_text().splitlines()
                                              if '"level": "WARNING"' in line)
    out["runs"] = runs
    _common.write_json(s.artifacts_dir / "reports" / "stress.json", out)
    print(json.dumps({k: {"rps": v["throughput_rps"], "p50": v["latency_ms_200"]["p50"], "p95": v["latency_ms_200"]["p95"],
                          "p99": v["latency_ms_200"]["p99"], "unexpected": v["unexpected_errors"],
                          "contract_failures": v["contract_failures"], "cache": v["cache"]}
                      for k, v in runs.items()}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
