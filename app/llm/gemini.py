"""Gemini REST provider (generateContent) with a bounded retry/fallback-model chain."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Optional

import httpx

from app.llm.base import LLMError, LLMProvider, LLMResponse
from app.models.internal import TokenUsage

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _quota_id(r: httpx.Response) -> Optional[str]:
    """Which quota a 429 hit (e.g. GenerateRequestsPerMinutePerProjectPerModel-FreeTier), for the logs."""
    try:
        for d in (r.json().get("error") or {}).get("details") or []:
            for v in d.get("violations") or []:
                if v.get("quotaId"):
                    return str(v["quotaId"])[:120]
    except Exception:
        return None
    return None


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        fallback_models: list[str],
        base_url: str,
        timeout_s: float,
        attempts_per_model: int,
        thinking_level: str,
        seed: int,
        max_output_tokens: int,
        price_lookup,
        hedge_after_s: float = 0.0,
        max_concurrency: int = 0,
    ):
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set", code="llm_not_configured")
        self._key = api_key
        self._model = model
        self._fallbacks = [m for m in fallback_models if m and m != model]
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        self._attempts = max(1, attempts_per_model)
        self._thinking_level = thinking_level
        self._seed = seed
        self._max_out = max_output_tokens
        self._price = price_lookup
        self._hedge_after = max(0.0, hedge_after_s)
        self.hedges = 0
        self._slots = asyncio.Semaphore(max_concurrency) if max_concurrency > 0 else None
        self.queued = 0  # calls that had to wait for a free slot (backpressure signal)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        )
        # Per-model thinking level ladder (minimal → low → none) and seed support, learnt from
        # 400 responses and remembered for the process lifetime.
        self._thinking: dict[str, Optional[str]] = {}
        self._no_thinking: set[str] = set()
        self._no_seed: set[str] = set()
        self.last_ok: Optional[bool] = None

    def model_name(self) -> str:
        return self._model

    def _body(self, model: str, system: str, prompt: str, schema: Optional[dict], temperature: float, max_out: int) -> dict:
        gen: dict[str, Any] = {"temperature": temperature, "maxOutputTokens": max_out, "candidateCount": 1}
        if model not in self._no_seed:
            gen["seed"] = self._seed
        if schema is not None:
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = schema
        level = self._thinking.get(model, self._thinking_level)
        if model not in self._no_thinking and level:
            if model.startswith("gemini-2.5"):
                gen["thinkingConfig"] = {"thinkingBudget": 0}
            else:
                gen["thinkingConfig"] = {"thinkingLevel": level}
        return {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen,
        }

    async def generate_structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: Optional[dict[str, Any]] = None,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_output_tokens: Optional[int] = None,
        hedge: bool = True,
    ) -> LLMResponse:
        """Bounded call with optional request hedging for tail latency.

        If the first request has not answered after `hedge_after_s`, an identical request is sent
        and the first successful answer wins (the other is cancelled). Tokens of a cancelled
        duplicate are not reported by the API, so hedged calls are counted in `self.hedges`.
        """
        kwargs = dict(system=system, prompt=prompt, schema=schema, model=model, temperature=temperature,
                      max_output_tokens=max_output_tokens)
        if not hedge or self._hedge_after <= 0:
            return await self._generate_chain(**kwargs)
        tasks: set[asyncio.Task] = set()
        try:
            first = asyncio.create_task(self._generate_chain(**kwargs))
            tasks.add(first)
            done, _ = await asyncio.wait({first}, timeout=self._hedge_after)
            if done:
                return first.result()
            self.hedges += 1
            second = asyncio.create_task(self._generate_chain(**kwargs))
            tasks.add(second)
            pending = {first, second}
            error: Optional[BaseException] = None
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t.exception() is None:
                        resp = t.result()
                        resp.attempts.append({"hedged": True})
                        return resp
                    error = t.exception()
            assert error is not None
            raise error
        finally:
            # Never leave duplicate requests running (also on caller cancellation).
            for t in tasks:
                if not t.done():
                    t.cancel()

    async def _generate_chain(
        self,
        *,
        system: str,
        prompt: str,
        schema: Optional[dict[str, Any]] = None,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_output_tokens: Optional[int] = None,
    ) -> LLMResponse:
        chain = [model or self._model] + [m for m in self._fallbacks if m != (model or self._model)]
        attempts: list[dict[str, Any]] = []
        started = time.perf_counter()
        last_error: Optional[LLMError] = None
        for m in chain:
            tries = 0
            while tries < self._attempts:
                tries += 1
                t0 = time.perf_counter()
                body = self._body(m, system, prompt, schema, temperature, max_output_tokens or self._max_out)
                try:
                    async with self._slot():
                        r = await self._client.post(
                            f"{self._base}/models/{m}:generateContent", json=body, headers={"x-goog-api-key": self._key}
                        )
                except httpx.TimeoutException:
                    attempts.append({"model": m, "status": "timeout", "ms": round((time.perf_counter() - t0) * 1000)})
                    last_error = LLMError(f"{m} timed out", code="llm_timeout", retryable=True)
                    break  # a timeout already consumed the budget: move to the next model
                except httpx.HTTPError as exc:
                    attempts.append({"model": m, "status": "network_error", "ms": round((time.perf_counter() - t0) * 1000)})
                    last_error = LLMError(f"{m} network error: {type(exc).__name__}", code="llm_network", retryable=True)
                    await asyncio.sleep(0.3 * tries)
                    continue
                ms = (time.perf_counter() - t0) * 1000
                attempts.append({"model": m, "status": r.status_code, "ms": round(ms)})
                if r.status_code == 429:
                    attempts[-1]["quota"] = _quota_id(r)
                if r.status_code == 200:
                    if len(attempts) > 1:  # operational signal: rate limits / overload / fail-over
                        log.warning("llm call recovered after failed attempts", extra={"event": {"attempts": attempts}})
                    return self._parse(r.json(), m, started, attempts)
                detail = r.text[:300].lower()
                if r.status_code == 400:
                    # Unsupported generation options differ per model: drop them once, then fail over.
                    if m not in self._no_thinking and ("thinking" in detail or "invalid argument" in detail):
                        current = self._thinking.get(m, self._thinking_level)
                        if current == "minimal":
                            self._thinking[m] = "low"  # e.g. "Thinking level MINIMAL is not supported"
                        else:
                            self._no_thinking.add(m)
                        tries -= 1
                        continue
                    if m not in self._no_seed and ("seed" in detail or "invalid argument" in detail):
                        self._no_seed.add(m)
                        tries -= 1
                        continue
                    last_error = LLMError(f"{m} rejected the request (400)", code="llm_http_400")
                    break
                if r.status_code in (401, 403):
                    self.last_ok = False
                    raise LLMError(f"{m} authentication/permission error ({r.status_code})", code=f"llm_http_{r.status_code}")
                if r.status_code in RETRYABLE_STATUS:
                    last_error = LLMError(f"{m} unavailable ({r.status_code})", code="llm_unavailable", retryable=True)
                    if tries < self._attempts:
                        await asyncio.sleep(0.4 * tries)
                    continue
                last_error = LLMError(f"{m} error {r.status_code}", code=f"llm_http_{r.status_code}")
                break  # 404 (model retired) and others: next model
        self.last_ok = False
        log.warning("llm call failed on every model", extra={"event": {"attempts": attempts}})
        err = last_error or LLMError("all models failed", code="llm_unavailable")
        err.attempts = attempts  # type: ignore[attr-defined]
        raise err

    @contextlib.asynccontextmanager
    async def _slot(self):
        if self._slots is None:
            yield
            return
        if self._slots.locked():
            self.queued += 1
        async with self._slots:
            yield

    def _parse(self, data: dict, model: str, started: float, attempts: list[dict]) -> LLMResponse:
        cands = data.get("candidates") or []
        if not cands:
            block = (data.get("promptFeedback") or {}).get("blockReason")
            raise LLMError(f"no candidates (block={block})", code="llm_blocked")
        cand = cands[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        um = data.get("usageMetadata") or {}
        usage = TokenUsage(
            input_tokens=int(um.get("promptTokenCount") or 0),
            output_tokens=int(um.get("candidatesTokenCount") or 0),
            thinking_tokens=int(um.get("thoughtsTokenCount") or 0),
            calls=1,
        )
        self.last_ok = True
        return LLMResponse(
            text=text,
            model=data.get("modelVersion") or model,
            usage=usage,
            latency_ms=(time.perf_counter() - started) * 1000,
            finish_reason=cand.get("finishReason"),
            attempts=attempts,
        )

    def estimate_cost(self, usage: TokenUsage, model: Optional[str] = None) -> Optional[float]:
        if usage.calls == 0:
            return 0.0
        price = self._price(model or self._model)
        if price is None:
            return None
        pin, pout = price
        return (usage.input_tokens * pin + (usage.output_tokens + usage.thinking_tokens) * pout) / 1_000_000

    async def probe(self) -> bool:
        """Cheap readiness check: model metadata lookup (no tokens consumed)."""
        try:
            r = await self._client.get(f"{self._base}/models/{self._model}", headers={"x-goog-api-key": self._key}, timeout=8.0)
            ok = r.status_code == 200
        except httpx.HTTPError:
            ok = False
        self.last_ok = ok
        return ok

    async def aclose(self) -> None:
        await self._client.aclose()
