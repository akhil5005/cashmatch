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
