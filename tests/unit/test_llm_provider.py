"""Gemini provider mechanics with a mocked transport (no network): backpressure, fail-over, hedging."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.llm.base import LLMError
from app.llm.gemini import GeminiProvider

OK_BODY = {"candidates": [{"content": {"parts": [{"text": "{\"ok\": true}"}]}, "finishReason": "STOP"}],
           "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}, "modelVersion": "m1"}


def provider(handler, **kw) -> GeminiProvider:
    p = GeminiProvider(api_key="test-key", model="m1", fallback_models=kw.pop("fallbacks", ["m2"]),
                       base_url="https://example.invalid/v1beta", timeout_s=5, attempts_per_model=kw.pop("attempts", 1),
                       thinking_level="minimal", seed=7, max_output_tokens=64, price_lookup=lambda m: (0.25, 1.5), **kw)
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p


def test_concurrency_limit_queues_excess_calls():
    in_flight, peak = 0, 0

    async def run():
        nonlocal in_flight, peak

        async def handler(request):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1
            return httpx.Response(200, json=OK_BODY)

        p = provider(handler, max_concurrency=3)
        await asyncio.gather(*(p.generate_structured(system="s", prompt="p", hedge=False) for _ in range(10)))
        return p

    p = asyncio.run(run())
    assert peak == 3 and p.queued > 0


def test_rate_limited_model_fails_over_and_is_priced_at_the_serving_model():
    def handler(request):
        if "/models/m1:" in str(request.url):
            return httpx.Response(429, json={"error": "quota"})
        return httpx.Response(200, json={**OK_BODY, "modelVersion": "m2"})

    p = provider(handler)
    resp = asyncio.run(p.generate_structured(system="s", prompt="p", hedge=False))
    assert resp.model == "m2" and [a["status"] for a in resp.attempts] == [429, 200]
    assert json.loads(resp.text) == {"ok": True}


def test_every_model_failing_raises_a_typed_error():
    p = provider(lambda request: httpx.Response(503, json={}))
    with pytest.raises(LLMError) as exc:
        asyncio.run(p.generate_structured(system="s", prompt="p", hedge=False))
    assert exc.value.code == "llm_unavailable" and len(exc.value.attempts) == 2


def test_auth_errors_are_not_retried_on_other_models():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(403, json={})

    p = provider(handler)
    with pytest.raises(LLMError) as exc:
        asyncio.run(p.generate_structured(system="s", prompt="p", hedge=False))
    assert exc.value.code == "llm_http_403" and len(calls) == 1
