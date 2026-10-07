"""The two Phase 1 endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from cashmatch.api.app import create_app
from cashmatch.db.session import get_db


@pytest.fixture
def client(engine: Engine) -> TestClient:
    """An app whose database dependency points at the SQLite test engine."""
    app = create_app()
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        db: Session = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_db_round_trips_a_query(client: TestClient) -> None:
    response = client.get("/health/db")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "reachable"}


def test_unknown_route_uses_the_uniform_error_shape(client: TestClient) -> None:
    response = client.get("/no-such-endpoint")
    assert response.status_code == 404
    assert "error" in response.json()
    assert set(response.json()["error"]) >= {"code", "message"}


def test_documentation_is_served_under_the_api_prefix(client: TestClient) -> None:
    """Docs must live under /api, because that is the only path nginx proxies.

    In production nginx serves the single-page UI on every path except /api,
    and the SPA's try_files fallback answers unknown paths with index.html
    rather than a 404. So a docs page mounted at FastAPI's default /docs is
    not merely unreachable -- it returns a page, with HTTP 200, and nothing
    anywhere reports a problem. This test is here because that is exactly
    how it shipped once.
    """
    for path in ("/api/docs", "/api/redoc", "/api/openapi.json"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"

    assert client.get("/api/openapi.json").json()["info"]["title"] == "CashMatch"


def test_the_default_docs_paths_are_not_mounted(client: TestClient) -> None:
    """The flip side: nothing should answer on the root-level defaults.

    If one of these starts returning 200 again, the prefix has drifted and
    production is serving a docs page the proxy cannot reach.
    """
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, f"{path} is still mounted"
