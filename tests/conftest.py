from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from obsync.api import create_app
from obsync.config import Settings
from obsync.llm import LLMAnalyzer
from obsync.vault_intelligence import link_target, search_terms, strip_maintenance_block


@pytest.fixture
def adaptive_ai(monkeypatch: pytest.MonkeyPatch):
    """Deterministic fake model for service tests; production has no static link fallback."""

    async def learn(_self, notes, *, feedback=None):
        return {
            "vault_summary": "Test vault model learned from its indexed notes.",
            "organization_principles": ["Use concrete cross-record facts."],
            "note_patterns": [],
            "relationship_guidance": ["Require evidence in both records."],
            "negative_relationship_guidance": ["A shared type alone is not a relationship."],
            "folder_guidance": [],
            "confidence": 0.9,
            "provider": "ollama",
            "model": "test-model",
            "note_count": len(notes),
        }

    async def adjudicate(
        _self,
        source_note,
        candidates,
        *,
        vault_model,
        minimum_confidence,
        maximum_links,
        feedback=None,
    ):
        source_content = strip_maintenance_block(str(source_note.get("content", "")))
        source_terms = search_terms(f"{source_note.get('title', '')} {source_content}")
        source_entities = {str(item).casefold() for item in source_note.get("entities", [])}
        relationships = []
        for candidate in candidates:
            title = str(candidate.get("title", ""))
            candidate_content = strip_maintenance_block(str(candidate.get("content", "")))
            candidate_entities = {str(item).casefold() for item in candidate.get("entities", [])}
            title_mentioned = len(title) >= 4 and title.casefold() in source_content.casefold()
            source_title_mentioned = (
                len(str(source_note.get("title", ""))) >= 4
                and str(source_note.get("title", "")).casefold() in candidate_content.casefold()
            )
            shared_ids = {
                item
                for item in source_entities & candidate_entities
                if ":" in item and any(character.isdigit() for character in item)
            }
            shared_terms = source_terms & search_terms(candidate_content)
            if not (title_mentioned or source_title_mentioned or shared_ids):
                continue
            target = link_target(candidate)
            source_fact = title or next(iter(shared_terms), "named record")
            relationships.append(
                {
                    "target": target,
                    "relationship": "Both records describe the same named record or party",
                    "evidence": [
                        f"SOURCE: {source_fact} appears in the source",
                        f"TARGET: {candidate.get('path', target)} contains the matching fact",
                    ],
                    "confidence": 0.92,
                }
            )
            if len(relationships) >= maximum_links:
                break
        return {
            "source_category": "",
            "source_role": "",
            "summary": "Test model selected only specifically supported relationships.",
            "suggested_tags": [],
            "relationships": relationships,
        }

    monkeypatch.setattr(LLMAnalyzer, "learn_vault_model", learn)
    monkeypatch.setattr(LLMAnalyzer, "adjudicate_relationships", adjudicate)

    def configure(service) -> None:
        service.db.set_settings(
            {
                "llm_enabled": ("true", False),
                "llm_provider": ("ollama", False),
                "llm_base_url": ("http://test-model", False),
                "llm_model": ("test-model", False),
            }
        )

    return configure


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
    application = create_app(app_settings)
    # Most integration tests exercise syncing rather than first-run vault onboarding.
    application.state.service.db.set_settings(
        {
            "vault_confirmed": ("true", False),
            "sync_enabled": ("true", False),
        }
    )
    return application


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(
        app,
        client=("127.0.0.1", 50000),
        base_url="http://localhost:7769",
    )


@pytest.fixture
def admin_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/setup",
        json={
            "username": "admin",
            "password": "correct horse battery staple",
            "legacy_token": "test-admin-token",
            "remember": False,
        },
    )
    assert response.status_code == 200, response.text
    csrf_token = client.cookies.get("obsync_csrf")
    assert csrf_token
    return {"X-CSRF-Token": csrf_token}


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
