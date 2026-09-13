from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.settings import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "data",
        max_running=2,
        auto_summary=True,
        duration_research=30.0,
        duration_implement=30.0,
        duration_test=30.0,
        duration_generic=30.0,
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def create_project(client: TestClient, name: str = "demo", goal: str = "goal") -> dict:
    response = client.post("/api/projects", json={"name": name, "goal": goal})
    assert response.status_code == 200, response.text
    return response.json()
