"""FastAPI application factory."""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from cashmatch import __version__
from cashmatch.api.errors import error_body, register_error_handlers
from cashmatch.api.routers import router
from cashmatch.config import get_settings
from cashmatch.db.session import get_db

DESCRIPTION = """\
Match incoming B2B bank payments to the open invoices they settle.

Every amount in this API is an **integer count of paise**, with a formatted
string beside it for display. A JSON number for money invites a float on the
other side, and a balance that is one paise wrong is impossible to explain to
a customer.

Every decision carries its explanation: which signals fired, what they
scored, and a sentence a reviewer can act on.
"""


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    app = FastAPI(
        title="CashMatch",
        version=__version__,
        description=DESCRIPTION,
        # Everything the API serves lives under /api, documentation included.
        # In production nginx proxies /api to this app and serves the
        # single-page UI on every other path -- so FastAPI's default /docs is
        # unreachable there, and the SPA quietly answers with index.html
        # instead of 404ing, which is a gap nothing complains about.
        # Keeping one prefix also means the UI never has to avoid a path name.
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        openapi_tags=[
            {"name": "metrics", "description": "Dashboard figures."},
            {"name": "results", "description": "The review queue."},
            {"name": "review", "description": "Approve, reject or reassign an item."},
            {"name": "reference", "description": "Customers and open invoices."},
            {"name": "uploads", "description": "Bank statements and remittance advice."},
            {"name": "pipeline", "description": "Run extraction and matching."},
            {"name": "health", "description": "Liveness and readiness."},
        ],
    )

    # The review UI is served from a different origin in development. Locked
    # to localhost rather than "*" so this is not a habit that ships.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_error_handlers(app)
    app.include_router(router)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        """Liveness: the process is up and serving."""
        return {"status": "ok", "service": "cashmatch", "version": __version__}

    @app.get("/health/db", tags=["health"])
    def health_db(session: Session = Depends(get_db)) -> JSONResponse:
        """Readiness: a real query round-trips to the database."""
        try:
            session.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            return JSONResponse(
                status_code=503,
                content=error_body(
                    "database_unavailable",
                    "CashMatch could not reach its database.",
                    hint=(
                        "Is Postgres running? Try `docker compose up -d db` and check "
                        f"that DATABASE_URL points at it. Driver said: {exc.__class__.__name__}"
                    ),
                ),
            )
        return JSONResponse(status_code=200, content={"status": "ok", "database": "reachable"})

    return app


app = create_app()
