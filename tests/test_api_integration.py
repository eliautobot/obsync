from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient


def sync_text(
    client: TestClient,
    headers: dict[str, str],
    root_id: str,
    path: str,
    content: bytes,
    *,
    mtime: int = 100,
    previous_path: str = "",
):
    return client.post(
        "/api/v1/agent/documents/sync",
        headers=headers,
        data={
            "root_id": root_id,
            "source_path": path,
            "source_mtime_ns": str(mtime),
            "source_size": str(len(content)),
            "sha256": hashlib.sha256(content).hexdigest(),
            "previous_path": previous_path,
        },
        files={"file": (Path(path).name, content, "text/plain")},
    )


def test_health_ui_and_admin_auth(client: TestClient, admin_headers: dict[str, str]) -> None:
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["vault_ready"] is True
    assert "Sign in to Obsync" in client.get("/").text
    assert client.get("/api/v1/admin/overview").status_code == 200
    client.cookies.clear()
    assert client.get("/api/v1/admin/overview").status_code == 401


def test_enrollment_is_single_use(client: TestClient, admin_headers: dict[str, str]) -> None:
    enrollment = client.post(
        "/api/v1/admin/enrollments", headers=admin_headers, json={"label": "Laptop"}
    ).json()
    payload = {
        "code": enrollment["code"],
        "name": "Laptop",
        "hostname": "laptop",
        "os_name": "Windows",
    }
    assert client.post("/api/v1/agents/register", json=payload).status_code == 200
    second = client.post("/api/v1/agents/register", json=payload)
    assert second.status_code == 400
    assert "already used" in second.json()["detail"]


def test_full_sync_update_missing_and_rename(
    client: TestClient,
    admin_headers: dict[str, str],
    agent_headers: dict[str, str],
    registered_root: dict,
    app_settings,
) -> None:
    first = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "Clients/Acme/contract.txt",
        b"Initial agreement for Acme.",
    )
    assert first.status_code == 200, first.text
    first_data = first.json()
    assert first_data["result"] == "synced"
    note_path = app_settings.vault_path / first_data["destination_path"]
    assert note_path.exists()
    assert "Initial agreement for Acme" in note_path.read_text(encoding="utf-8")

    note_path.write_text(
        note_path.read_text(encoding="utf-8") + "Manual owner note.\n", encoding="utf-8"
    )
    update = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "Clients/Acme/contract.txt",
        b"Revised agreement for Acme.",
        mtime=200,
    )
    assert update.status_code == 200
    assert update.json()["id"] == first_data["id"]
    updated_note = note_path.read_text(encoding="utf-8")
    assert "Revised agreement" in updated_note
    assert "Initial agreement" not in updated_note
    assert "Manual owner note." in updated_note

    missing = client.post(
        "/api/v1/agent/documents/missing",
        headers=agent_headers,
        json={"root_id": registered_root["id"], "source_path": "Clients/Acme/contract.txt"},
    )
    assert missing.status_code == 200
    assert note_path.exists()
    assert "obsync_status: source-missing" in note_path.read_text(encoding="utf-8")

    renamed = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "Clients/Acme/signed-contract.txt",
        b"Revised agreement for Acme.",
        mtime=300,
        previous_path="Clients/Acme/contract.txt",
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["id"] == first_data["id"]
    docs = client.get("/api/v1/admin/documents", headers=admin_headers).json()
    assert docs["total"] == 1
    assert docs["items"][0]["source_path"] == "Clients/Acme/signed-contract.txt"


def test_bad_hash_and_traversal_are_rejected(
    client: TestClient, agent_headers: dict[str, str], registered_root: dict
) -> None:
    content = b"hello"
    bad_hash = client.post(
        "/api/v1/agent/documents/sync",
        headers=agent_headers,
        data={
            "root_id": registered_root["id"],
            "source_path": "hello.txt",
            "source_mtime_ns": "1",
            "source_size": str(len(content)),
            "sha256": "0" * 64,
        },
        files={"file": ("hello.txt", content, "text/plain")},
    )
    assert bad_hash.status_code == 400
    traversal = sync_text(
        client, agent_headers, registered_root["id"], "../../outside.txt", content
    )
    assert traversal.status_code == 400


def test_settings_secret_redaction_and_command_queue(
    client: TestClient,
    admin_headers: dict[str, str],
    agent_headers: dict[str, str],
    enrolled_agent: dict[str, str],
) -> None:
    response = client.put(
        "/api/v1/admin/settings",
        headers=admin_headers,
        json={
            "llm_enabled": True,
            "llm_provider": "lmstudio",
            "llm_base_url": "http://model:1234",
            "llm_model": "local-model",
            "llm_api_key": "private-key",
            "review_threshold": "0.7",
        },
    )
    assert response.status_code == 200
    assert response.json()["llm_api_key"] == "configured"
    assert "private-key" not in response.text

    queued = client.post(
        f"/api/v1/admin/agents/{enrolled_agent['agent_id']}/scan", headers=admin_headers
    )
    assert queued.status_code == 200
    commands = client.get("/api/v1/agent/commands", headers=agent_headers).json()["items"]
    assert len(commands) == 1
    assert commands[0]["command"] == "scan"
    completed = client.post(
        f"/api/v1/agent/commands/{commands[0]['id']}/complete",
        headers=agent_headers,
        json={"ok": True, "result": "done"},
    )
    assert completed.status_code == 200
    assert client.get("/api/v1/agent/commands", headers=agent_headers).json()["items"] == []


def test_admin_listing_review_and_event_routes(
    client: TestClient,
    admin_headers: dict[str, str],
    agent_headers: dict[str, str],
    registered_root: dict,
) -> None:
    synced = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "meeting.txt",
        b"Meeting notes about project delivery.",
    ).json()
    assert client.get("/api/v1/admin/agents", headers=admin_headers).json()["items"]
    assert client.get("/api/v1/admin/roots", headers=admin_headers).json()["items"]
    events = client.get("/api/v1/admin/events", headers=admin_headers).json()["items"]
    assert any(event["event_type"] == "document.synced" for event in events)
    review = client.get("/api/v1/admin/documents?review=true", headers=admin_headers).json()
    assert review["total"] == 1
    assert (
        client.post(
            f"/api/v1/admin/documents/{synced['id']}/approve", headers=admin_headers
        ).status_code
        == 200
    )
    assert (
        client.get("/api/v1/admin/documents?review=true", headers=admin_headers).json()["total"]
        == 0
    )
    retry = client.post(f"/api/v1/admin/documents/{synced['id']}/retry", headers=admin_headers)
    assert retry.status_code == 200
    assert retry.json()["command"] == "resync"


def test_invalid_agent_and_root_are_rejected(
    client: TestClient, admin_headers: dict[str, str], agent_headers: dict[str, str]
) -> None:
    assert client.get("/api/v1/agent/commands").status_code == 401
    bad_root = client.post(
        "/api/v1/agent/roots",
        headers=agent_headers,
        json={
            "root_key": "bad",
            "name": "Bad",
            "path": "/source",
            "destination": "../../outside",
        },
    )
    assert bad_root.status_code == 400
    unknown_scan = client.post(
        "/api/v1/admin/agents/unknown/scan",
        headers=admin_headers,
    )
    assert unknown_scan.status_code == 400
