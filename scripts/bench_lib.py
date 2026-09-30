"""Benchmark helpers: launch a real uvicorn server, timed HTTP calls, percentiles, resource sampling."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
import numpy as np

import _common

ROOT = _common.ROOT


def percentiles(values: list[float]) -> dict[str, Optional[float]]:
    if not values:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "mean": None, "max": None}
    a = np.asarray(values, dtype=float)
    return {
        "n": int(a.size),
        "p50": round(float(np.percentile(a, 50)), 2),
        "p95": round(float(np.percentile(a, 95)), 2),
        "p99": round(float(np.percentile(a, 99)), 2),
        "mean": round(float(a.mean()), 2),
        "max": round(float(a.max()), 2),
    }


@dataclass
class Server:
    port: int
    env: dict[str, str]
    proc: Optional[subprocess.Popen] = None
    log_path: Optional[Path] = None
    startup_s: float = 0.0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout_s: float = 180.0, require_healthy: bool = True) -> "Server":
        env = dict(os.environ)
        env.update(self.env)
        self.log_path = ROOT / "artifacts" / "tmp" / f"server_{self.port}.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.log_path.open("w")
        t0 = time.perf_counter()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port),
             "--workers", "1", "--log-level", "warning"],
            cwd=str(ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT,
        )
        with httpx.Client(timeout=5.0, trust_env=False) as c:
            while time.perf_counter() - t0 < timeout_s:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"server exited early, see {self.log_path}")
                try:
                    r = c.get(f"{self.url}/health")
                    if r.status_code == 200 or (not require_healthy and r.status_code == 503):
                        self.startup_s = time.perf_counter() - t0
                        return self
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
        raise TimeoutError(f"server not healthy after {timeout_s}s, see {self.log_path}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def rss_mb(self) -> Optional[float]:
        try:
            for line in Path(f"/proc/{self.proc.pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
        except Exception:
            return None
        return None

    def cpu_seconds(self) -> Optional[float]:
        try:
            fields = Path(f"/proc/{self.proc.pid}/stat").read_text().rsplit(")", 1)[1].split()
            ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
            return (int(fields[11]) + int(fields[12])) / ticks
        except Exception:
            return None


@dataclass
class Call:
    status: int
    client_ms: float
    server_ms: Optional[float]
    headers: dict[str, str]
    body: Any
    error: Optional[str] = None
    tags: dict[str, Any] = field(default_factory=dict)
    query: str = ""

    @property
    def cache(self) -> str:
        return self.headers.get("x-cache", "")

    def telemetry(self) -> dict[str, Any]:
        h = self.headers
        cost = h.get("x-cost-usd")
        return {
            "status": self.status, "client_ms": round(self.client_ms, 3), "server_ms": self.server_ms,
            "cache": h.get("x-cache"), "model_calls": int(h.get("x-model-calls", 0) or 0),
            "tokens_in": int(h.get("x-tokens-input", 0) or 0), "tokens_out": int(h.get("x-tokens-output", 0) or 0),
            "cost_usd": None if cost in (None, "unavailable") else float(cost), "error": self.error, **self.tags,
        }


def post(client: httpx.Client, base: str, query: str, siis: Optional[str] = None, **tags) -> Call:
    payload: dict[str, Any] = {"query": query}
    if siis is not None:
        payload["siis_response"] = siis
    t0 = time.perf_counter()
    try:
        r = client.post(f"{base}/v1/troubleshoot", json=payload)
    except httpx.HTTPError as exc:
        return Call(0, (time.perf_counter() - t0) * 1000, None, {}, None, type(exc).__name__, tags, query)
    ms = (time.perf_counter() - t0) * 1000
    try:
        body = r.json()
    except Exception:
        body = r.text
    sm = r.headers.get("x-latency-ms")
    return Call(r.status_code, ms, float(sm) if sm else None, {k.lower(): v for k, v in r.headers.items()}, body,
                None, tags, query)


def dump_jsonl(path: Path, rows: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
