from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from obsync.api import create_app
from obsync.config import Settings


@pytest.fixture
def app_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        vault_path=tmp_path / "vault",
        admin_token="test-admin-token",
        max_upload_mb=5,
        max_extract_chars=50_000,
    )


@pytest.fixture
def app(app_settings: Settings):
    return create_app(app_settings)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-admin-token"}


@pytest.fixture
def enrolled_agent(client: TestClient, admin_headers: dict[str, str]) -> dict[str, str]:
    enrollment = client.post(
        "/api/v1/admin/enrollments",
        headers=admin_headers,
        json={"label": "Test PC"},
    ).json()
    response = client.post(
        "/api/v1/agents/register",
        json={
            "code": enrollment["code"],
            "name": "Test PC",
            "hostname": "test-pc",
            "os_name": "Linux",
            "os_version": "test",
            "agent_version": "0.1.0",
        },
    )
    assert response.status_code == 200
    return response.json()


@pytest.fixture
def agent_headers(enrolled_agent: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {enrolled_agent['agent_token']}"}


@pytest.fixture
def registered_root(client: TestClient, agent_headers: dict[str, str]) -> dict:
    response = client.post(
        "/api/v1/agent/roots",
        headers=agent_headers,
        json={
            "root_key": "root-1",
            "name": "Projects",
            "path": "/source/projects",
            "destination": "Imported Knowledge",
            "include_patterns": ["**/*"],
            "exclude_patterns": ["**/*.tmp"],
            "enabled": True,
        },
    )
    assert response.status_code == 200
    return response.json()
