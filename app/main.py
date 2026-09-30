"""FastAPI application factory.

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request

from app.api.errors import error_body, install_error_handlers
from app.api.routes import router
from app.core.config import Settings, get_settings
from app.core.container import Components, build_components
from app.core.logging import configure_logging

log = logging.getLogger(__name__)
MAX_BODY_BYTES = 1_000_000


def _warm_up(comps: Components) -> None:
    """Exercise lazy code paths (regexes, pydantic schemas, ONNX session) before serving."""
    intent = comps.enricher.analyze("my phone battery dies very fast")
    vec = comps.embedder.embed([intent.normalized_query])[0]
    comps.cache.lookup_semantic(intent, vec, None, count_miss=False)
    comps.cache.stats["misses"] = 0
    from app.validation.business_rules import validate_envelope

    validate_envelope({"query": "x", "query_variations": [], "response": {"contexts": []},
                       "meta": {"latency_ms": 0, "cache_hit": False, "model": "x", "cost_usd": 0.0}}, comps.registry)


def create_app(settings: Optional[Settings] = None, components: Optional[Components] = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.components = None
        app.state.llm_ok = None
        app.state.init_error = None
        comps = components
        try:
            if comps is None:
                comps = await asyncio.to_thread(build_components, settings)
            await asyncio.to_thread(_warm_up, comps)
            app.state.llm_ok = await comps.llm.probe() if settings.llm_startup_probe else True
            app.state.components = comps
            log.info("service ready dataset=%s plans=%d llm_ok=%s", settings.dataset_label, len(comps.cache),
                     app.state.llm_ok)
        except Exception as exc:  # the process stays up; /health reports the failure
            log.exception("initialisation failed")
            app.state.init_error = type(exc).__name__
        yield
        if comps is not None:
            try:
                await comps.llm.aclose()
                comps.cache.store.close()
            except Exception:
                pass

    app = FastAPI(
        title="Smart Guided Troubleshooting Engine",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid[:64]
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            from fastapi.responses import JSONResponse

            return JSONResponse(error_body("payload_too_large", "Request body is too large.", request.state.request_id), 413)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    install_error_handlers(app)
    app.include_router(router)
    return app


app = create_app()
