"""Groq provider mechanics with a mocked transport (no network)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.llm.base import LLMError
from app.llm.groq import GroqProvider, to_strict_json_schema
from app.llm.prompts import EXTRACTION_SCHEMA

OK = {"model": "m1", "choices": [{"message": {"content": "{\"ok\": true}"}, "finish_reason": "stop"}],
      "usage": {"prompt_tokens": 100, "completion_tokens": 50, "completion_tokens_details": {"reasoning_tokens": 20}}}


def provider(handler, **kw) -> GroqProvider:
    p = GroqProvider(api_key="test-key", model="m1", fallback_models=kw.pop("fallbacks", ["m2"]),
                     base_url="https://example.invalid/openai/v1", timeout_s=5, attempts_per_model=1, seed=7,
                     max_output_tokens=64, price_lookup=lambda m: (0.15, 0.60), **kw)
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p


def test_strict_schema_closes_objects_and_makes_optional_fields_nullable():
    s = to_strict_json_schema(EXTRACTION_SCHEMA)
    assert s["type"] == "object" and s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"])
    action = s["properties"]["goals"]["items"]["properties"]["actions"]["items"]
    assert action["additionalProperties"] is False and set(action["required"]) == set(action["properties"])
    assert action["properties"]["target_screen"]["type"] == ["string", "null"]  # optional in the source schema
    assert action["properties"]["category"]["enum"] == ["auto", "manual", "critical"]
    assert action["properties"]["steps"] == {"type": "array", "items": {"type": "string"}}


def test_request_shape_and_reasoning_tokens_billed_once():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=OK)

    p = provider(handler)
    r = asyncio.run(p.generate_structured(system="s", prompt="p", schema=EXTRACTION_SCHEMA, hedge=False))
    assert seen["response_format"]["type"] == "json_schema" and seen["response_format"]["json_schema"]["strict"]
    assert seen["seed"] == 7 and seen["temperature"] == 0.0
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.thinking_tokens) == (100, 30, 20)
    assert p.estimate_cost(r.usage, "m1") == pytest.approx((100 * 0.15 + 50 * 0.60) / 1e6)


def test_rate_limited_model_fails_over():
    def handler(request):
        if json.loads(request.content)["model"] == "m1":
            return httpx.Response(429, json={"error": {"message": "Rate limit reached for model m1 (TPM)"}})
        return httpx.Response(200, json={**OK, "model": "m2"})

    r = asyncio.run(provider(handler).generate_structured(system="s", prompt="p", hedge=False))
    assert r.model == "m2" and [a["status"] for a in r.attempts] == [429, 200]
    assert "TPM" in r.attempts[0]["quota"]


def test_auth_error_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    with pytest.raises(LLMError) as exc:
        asyncio.run(provider(handler).generate_structured(system="s", prompt="p", hedge=False))
    assert exc.value.code == "llm_http_401" and len(calls) == 1
