"""A single error shape for the whole API.

Clients -- including the Phase 7 React UI -- should never have to guess
whether an error body has ``detail``, ``message`` or ``error``. Everything
that goes wrong comes back as:

    {"error": {"code": "...", "message": "...", "hint": "..."}}

``message`` says what went wrong in plain language; ``hint`` says what to do
about it. Stack traces stay in the logs.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


def error_body(code: str, message: str, hint: str | None = None) -> dict[str, dict[str, str]]:
    payload = {"code": code, "message": message}
    if hint:
        payload["hint"] = hint
    return {"error": payload}


def register_error_handlers(app: FastAPI) -> None:
    """Attach the uniform handlers to an app instance."""

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body("http_error", str(exc.detail)),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'][1:])}: {err['msg']}" for err in exc.errors()
        )
        return JSONResponse(
            status_code=422,
            content=error_body(
                "invalid_request",
                f"The request body or query was not valid: {problems}",
                hint="Check field names, types and required values against /docs.",
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=error_body(
                "internal_error",
                "Something failed inside CashMatch while handling this request.",
                hint="Check the API logs for the full traceback.",
            ),
        )
