"""Safe structured errors: stable codes, request id, never a stack trace."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.services.troubleshooting import ServiceError

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_body(code: str, message: str, request_id: str | None) -> dict:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def _rid(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse(error_body(exc.code, exc.message, _rid(request)), status_code=exc.status)

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError):
        return JSONResponse(error_body(exc.code, str(exc), _rid(request)), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errs = exc.errors()
        if any(e.get("type") == "json_invalid" for e in errs):
            return JSONResponse(error_body("malformed_json", "Request body is not valid JSON.", _rid(request)), 422)
        fields = sorted({".".join(str(p) for p in e.get("loc", ())[1:]) or "body" for e in errs})
        return JSONResponse(
            error_body("invalid_request", f"Invalid or missing field(s): {', '.join(fields)}", _rid(request)), 422
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(error_body(code, str(exc.detail), _rid(request)), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled error request_id=%s", _rid(request))
        return JSONResponse(error_body("internal_error", "An internal error occurred.", _rid(request)), 500)
