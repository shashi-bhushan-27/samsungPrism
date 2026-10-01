"""Groq provider (OpenAI-compatible chat completions) with strict JSON-schema outputs.

Same contract as the Gemini provider: bounded retries per model, fail-over to the next model on
429/5xx/404, optional request hedging for tail latency, per-process concurrency limit, and usage
reported per call so the orchestrator can bill it at the serving model's published rate.
"""

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


def to_strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Gemini-style OpenAPI subset ("OBJECT", "STRING", …) → strict JSON Schema.

    Strict mode requires every property to be listed in `required` and closed objects, so a property
    that was optional becomes nullable ({"type": [t, "null"]}) instead of omittable.
    """

    def conv(node: dict[str, Any], optional: bool = False) -> dict[str, Any]:
        t = str(node.get("type", "STRING")).lower()
        out: dict[str, Any] = {}
        if t == "object":
            props = node.get("properties", {})
            required = set(node.get("required", props.keys()))
            out = {"type": "object", "additionalProperties": False,
                   "properties": {k: conv(v, k not in required) for k, v in props.items()},
                   "required": list(props.keys())}
        elif t == "array":
            out = {"type": "array", "items": conv(node.get("items", {"type": "STRING"}))}
        else:
            out = {"type": t}
            if "enum" in node:
                out["enum"] = list(node["enum"])
        if node.get("description"):
            out["description"] = node["description"]
        if optional:
            out["type"] = [out["type"], "null"]
            if "enum" in out:
                out["enum"] = out["enum"] + [None]
        return out

    return conv(schema)


class GroqProvider(LLMProvider):
    name = "groq"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        fallback_models: list[str],
        base_url: str,
        timeout_s: float,
        attempts_per_model: int,
        seed: int,
        max_output_tokens: int,
        price_lookup,
        reasoning_effort: str = "low",
        hedge_after_s: float = 0.0,
        max_concurrency: int = 0,
    ):
        if not api_key:
            raise LLMError("GROQ_API_KEY is not set", code="llm_not_configured")
        self._key = api_key
        self._model = model
        self._fallbacks = [m for m in fallback_models if m and m != model]
        self._base = base_url.rstrip("/")
        self._attempts = max(1, attempts_per_model)
        self._seed = seed
        self._max_out = max_output_tokens
        self._price = price_lookup
        self._effort = reasoning_effort
        self._hedge_after = max(0.0, hedge_after_s)
        self._slots = asyncio.Semaphore(max_concurrency) if max_concurrency > 0 else None
        self._no_effort: set[str] = set()
        self._no_strict: set[str] = set()
        self.hedges = 0
        self.queued = 0
        self.last_ok: Optional[bool] = None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        )

    def model_name(self) -> str:
        return self._model

    def _body(self, model, system, prompt, schema, temperature, max_out) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "temperature": temperature,
            "seed": self._seed,
            "max_completion_tokens": max_out,
        }
        if schema is not None:
            if model in self._no_strict:
                body["response_format"] = {"type": "json_object"}
            else:
                body["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "result", "strict": True, "schema": to_strict_json_schema(schema)}}
        if self._effort and model not in self._no_effort and model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = self._effort
            body["include_reasoning"] = False
        return body

    @contextlib.asynccontextmanager
    async def _slot(self):
        if self._slots is None:
            yield
            return
        if self._slots.locked():
            self.queued += 1
        async with self._slots:
            yield

    async def generate_structured(self, *, system, prompt, schema=None, model=None, temperature=0.0,
                                  max_output_tokens=None, hedge: bool = True) -> LLMResponse:
        kwargs = dict(system=system, prompt=prompt, schema=schema, model=model, temperature=temperature,
                      max_output_tokens=max_output_tokens)
        if not hedge or self._hedge_after <= 0:
            return await self._chain(**kwargs)
        tasks: set[asyncio.Task] = set()
        try:
            first = asyncio.create_task(self._chain(**kwargs))
            tasks.add(first)
            done, _ = await asyncio.wait({first}, timeout=self._hedge_after)
            if done:
                return first.result()
            self.hedges += 1
            second = asyncio.create_task(self._chain(**kwargs))
            tasks.add(second)
            pending, error = {first, second}, None
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t.exception() is None:
                        resp = t.result()
                        resp.attempts.append({"hedged": True})
                        return resp
                    error = t.exception()
            raise error  # type: ignore[misc]
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

    async def _chain(self, *, system, prompt, schema, model, temperature, max_output_tokens) -> LLMResponse:
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
                        r = await self._client.post(f"{self._base}/chat/completions", json=body,
                                                    headers={"Authorization": f"Bearer {self._key}"})
                except httpx.TimeoutException:
                    attempts.append({"model": m, "status": "timeout", "ms": round((time.perf_counter() - t0) * 1000)})
                    last_error = LLMError(f"{m} timed out", code="llm_timeout", retryable=True)
                    break
                except httpx.HTTPError as exc:
                    attempts.append({"model": m, "status": "network_error", "ms": round((time.perf_counter() - t0) * 1000)})
                    last_error = LLMError(f"{m} network error: {type(exc).__name__}", code="llm_network", retryable=True)
                    await asyncio.sleep(0.3 * tries)
                    continue
                attempts.append({"model": m, "status": r.status_code, "ms": round((time.perf_counter() - t0) * 1000)})
                if r.status_code == 200:
                    if len(attempts) > 1:
                        log.warning("llm call recovered after failed attempts", extra={"event": {"attempts": attempts}})
                    return self._parse(r.json(), m, started, attempts)
                err = _error(r)
                if r.status_code == 400:
                    code = err.get("code", "")
                    if "reasoning" in err.get("message", "").lower() and m not in self._no_effort:
                        self._no_effort.add(m)
                        tries -= 1
                        continue
                    if code == "json_validate_failed" or "json_schema" in err.get("message", ""):
                        # Constrained decoding failed or is unsupported: one more try, then JSON mode.
                        if "json_schema" in err.get("message", "") and m not in self._no_strict:
                            self._no_strict.add(m)
                            tries -= 1
                        last_error = LLMError(f"{m} produced invalid JSON (400)", code="llm_invalid_output",
                                              retryable=True)
                        continue
                    last_error = LLMError(f"{m} rejected the request (400)", code="llm_http_400")
                    break
                if r.status_code in (401, 403):
                    self.last_ok = False
                    raise LLMError(f"{m} authentication/permission error ({r.status_code})",
                                   code=f"llm_http_{r.status_code}")
                if r.status_code in RETRYABLE_STATUS:
                    if r.status_code == 429:
                        attempts[-1]["quota"] = (err.get("message") or "")[:120]
                    last_error = LLMError(f"{m} unavailable ({r.status_code})", code="llm_unavailable", retryable=True)
                    if tries < self._attempts:
                        await asyncio.sleep(min(2.0, 0.5 * tries))
                    continue
                last_error = LLMError(f"{m} error {r.status_code}", code=f"llm_http_{r.status_code}")
                break
        self.last_ok = False
        log.warning("llm call failed on every model", extra={"event": {"attempts": attempts}})
        err = last_error or LLMError("all models failed", code="llm_unavailable")
        err.attempts = attempts  # type: ignore[attr-defined]
        raise err

    def _parse(self, data: dict, model: str, started: float, attempts: list[dict]) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("no choices in response", code="llm_blocked")
        msg = choices[0].get("message") or {}
        u = data.get("usage") or {}
        completion = int(u.get("completion_tokens") or 0)
        reasoning = int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        usage = TokenUsage(
            input_tokens=int(u.get("prompt_tokens") or 0),
            output_tokens=max(0, completion - reasoning),
            thinking_tokens=reasoning,  # billed at the output rate (estimate_cost)
            calls=1,
        )
        self.last_ok = True
        return LLMResponse(text=msg.get("content") or "", model=data.get("model") or model, usage=usage,
                           latency_ms=(time.perf_counter() - started) * 1000,
                           finish_reason=choices[0].get("finish_reason"), attempts=attempts)

    def estimate_cost(self, usage: TokenUsage, model: Optional[str] = None) -> Optional[float]:
        if usage.calls == 0:
            return 0.0
        price = self._price(model or self._model)
        if price is None:
            return None
        pin, pout = price
        return (usage.input_tokens * pin + (usage.output_tokens + usage.thinking_tokens) * pout) / 1_000_000

    async def probe(self) -> bool:
        """Readiness: model metadata lookup (no tokens consumed)."""
        try:
            r = await self._client.get(f"{self._base}/models/{self._model}",
                                       headers={"Authorization": f"Bearer {self._key}"}, timeout=8.0)
            ok = r.status_code == 200
        except httpx.HTTPError:
            ok = False
        self.last_ok = ok
        return ok

    async def aclose(self) -> None:
        await self._client.aclose()


def _error(r: httpx.Response) -> dict[str, Any]:
    try:
        e = r.json().get("error") or {}
        return {"code": str(e.get("code") or ""), "message": str(e.get("message") or "")}
    except Exception:
        return {"code": "", "message": r.text[:200]}
