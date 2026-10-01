"""HTTP endpoints: POST /v1/troubleshoot and GET /health (PDF §5)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from pathlib import Path

from fastapi.responses import HTMLResponse, JSONResponse

from app.api.errors import ApiError
from app.api.schemas import TroubleshootRequest
from app.core.logging import log_event

router = APIRouter()
log = logging.getLogger("app.requests")


def _components(request: Request):
    comps = getattr(request.app.state, "components", None)
    if comps is None:
        raise ApiError(503, "service_unavailable", "The service is still initialising or failed to start.")
    return comps


@router.post("/v1/troubleshoot")
async def troubleshoot(body: TroubleshootRequest, request: Request):
    comps = _components(request)
    s = comps.settings
    if not body.query.strip():
        raise ApiError(422, "empty_query", "query must not be empty.")
    if len(body.query) > s.max_query_chars:
        raise ApiError(422, "query_too_long", f"query exceeds {s.max_query_chars} characters.")
    if body.siis_response is not None and len(body.siis_response) > s.max_siis_chars:
        raise ApiError(422, "siis_response_too_long", f"siis_response exceeds {s.max_siis_chars} characters.")
    rid = request.state.request_id
    result = await comps.service.troubleshoot(body.query, body.siis_response, request_id=rid)
    tel = result.telemetry
    headers = {
        "X-Cache": ("HIT-" + str(tel.get("cache_kind")).upper()) if tel.get("cache_hit") else "MISS",
        "X-Latency-Ms": f"{tel.get('latency_ms', 0):.2f}",
        "X-Model-Calls": str(tel.get("model_calls", 0)),
        "X-Tokens-Input": str(tel.get("tokens", {}).get("input", 0)),
        "X-Tokens-Output": str(tel.get("tokens", {}).get("output", 0) + tel.get("tokens", {}).get("thinking", 0)),
        "X-Cost-USD": "unavailable" if tel.get("cost_usd") is None else f"{tel['cost_usd']:.8f}",
    }
    log_event(
        log,
        "troubleshoot",
        request_id=rid,
        cache_hit=tel.get("cache_hit"),
        cache_kind=tel.get("cache_kind"),
        latency_ms=tel.get("latency_ms"),
        model=tel.get("model"),
        model_calls=tel.get("model_calls"),
        tokens=tel.get("tokens"),
        cost_usd=tel.get("cost_usd"),
        stages=tel.get("stages"),
        deeplinks=tel.get("deeplinks"),
        fallback=tel.get("fallback"),
        validation=tel.get("validation", {}).get("ok", True),
        candidates=[len(a["mapping"]["candidates"]) for a in tel.get("actions", [])],
    )
    return JSONResponse(result.body, status_code=result.status_code, headers=headers)


def health_components(request: Request) -> tuple[bool, dict[str, Any]]:
    state = request.app.state
    comps = getattr(state, "components", None)
    if comps is None:
        return False, {"initialised": False, "error": getattr(state, "init_error", None)}
    s = comps.settings
    llm_ok = getattr(state, "llm_ok", None)
    last = getattr(comps.llm, "last_ok", None)
    checks = {
        "catalog": len(comps.registry) > 0,
        "vector_indexes": len(comps.catalog_index) == len(comps.catalog_index.docs) > 0
        and comps.catalog_index.dense.matrix.shape[0] == len(comps.catalog_index.docs),
        "embedding_model": comps.embedder.dim > 0,
        "cache": bool(comps.cache.ready),
        "llm": bool(llm_ok) and last is not False,
    }
    required = ["catalog", "vector_indexes", "embedding_model", "cache"] + (["llm"] if s.health_require_llm else [])
    ok = all(checks[k] for k in required)
    return ok, {"checks": checks, "required": required, "dataset": s.dataset_label,
                "cache_plans": len(comps.cache), "llm_model": s.llm_model, "embedding": comps.embedder.signature}


@router.get("/health")
async def health(request: Request):
    ok, details = health_components(request)
    if ok:
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "unavailable", "components": details}, status_code=503)


@router.get("/health/details")
async def health_details(request: Request):
    ok, details = health_components(request)
    return JSONResponse({"status": "ok" if ok else "unavailable", **details}, status_code=200 if ok else 503)


_DEMO_PAGE = Path(__file__).resolve().parents[1] / "static" / "demo.html"


@router.get("/demo", include_in_schema=False)
async def demo() -> HTMLResponse:
    """Small static page that calls POST /v1/troubleshoot and renders the plan (for demos)."""
    return HTMLResponse(_DEMO_PAGE.read_text(encoding="utf-8"))
