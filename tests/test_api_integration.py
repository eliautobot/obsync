from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from obsync.agent import AgentConfig, AgentRuntime, AgentState


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


def inventory(
    client: TestClient,
    headers: dict[str, str],
    root_id: str,
    scan_id: str,
    files: dict[str, bytes],
):
    return client.post(
        "/api/v1/agent/inventory",
        headers=headers,
        json={
            "root_id": root_id,
            "scan_id": scan_id,
            "items": [
                {
                    "source_path": path,
                    "source_mtime_ns": index + 1,
                    "source_size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for index, (path, content) in enumerate(files.items())
            ],
            "complete": True,
        },
    )


def test_health_ui_and_admin_auth(client: TestClient, admin_headers: dict[str, str]) -> None:
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["vault_ready"] is True
    assert "Sign in to Obsync" in client.get("/").text
    assert client.get("/api/v1/admin/overview").status_code == 200
    client.cookies.clear()
    assert client.get("/api/v1/admin/overview").status_code == 401


def test_ui_includes_guided_help_and_windows_companion(client: TestClient) -> None:
    index = client.get("/").text
    app_js = client.get("/assets/app.js").text
    styles = client.get("/assets/styles.css").text
    assert 'data-view="help"' in index
    assert 'popover="manual"' in index
    assert "renderHelp" in app_js
    assert "Download Windows Companion" in app_js
    assert "obsync-companion-windows-x64.exe" in app_js
    assert "Keep that PowerShell window open" not in app_js
    assert ".help-tip" in styles
    assert "backdrop-filter: blur(3px)" not in styles


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
    registered = client.post("/api/v1/agents/register", json=payload)
    assert registered.status_code == 200
    status = client.get(
        f"/api/v1/admin/enrollments/{enrollment['id']}", headers=admin_headers
    ).json()
    assert status["connected"] is True
    assert status["agent"]["id"] == registered.json()["agent_id"]
    second = client.post("/api/v1/agents/register", json=payload)
    assert second.status_code == 400
    assert "already used" in second.json()["detail"]


@pytest.mark.asyncio
async def test_desktop_agent_can_write_the_obsidian_vault(
    app,
    client: TestClient,
    admin_headers: dict[str, str],
    enrolled_agent: dict[str, str],
    agent_headers: dict[str, str],
    registered_root: dict,
    tmp_path: Path,
) -> None:
    desktop_vault = tmp_path / "windows-vault"
    desktop_vault.mkdir()
    heartbeat = client.post(
        "/api/v1/agent/heartbeat",
        headers=agent_headers,
        json={
            "agent_version": "0.4.0",
            "vault_path": str(desktop_vault),
            "vault_ready": True,
            "vault_error": "",
        },
    )
    assert heartbeat.status_code == 200
    settings = client.put(
        "/api/v1/admin/settings",
        headers=admin_headers,
        json={"vault_mode": "agent", "vault_agent_id": enrolled_agent["agent_id"]},
    )
    assert settings.status_code == 200
    assert settings.json()["vault_mode"] == "agent"

    synced = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "Projects/remote-note.txt",
        b"A note written to the Windows Obsidian vault.",
    )
    assert synced.status_code == 200, synced.text
    assert synced.json()["result"] == "queued"
    assert synced.json()["status"] == "pending-write"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {enrolled_agent['agent_token']}"},
    ) as async_client:
        runtime = AgentRuntime(
            AgentConfig(
                server_url="http://testserver",
                agent_id=enrolled_agent["agent_id"],
                agent_token=enrolled_agent["agent_token"],
                name="Test PC",
                vault_path=str(desktop_vault),
            ),
            state=AgentState(tmp_path / "remote-agent-state.db"),
            client=async_client,
        )
        await runtime.process_commands_once()

        note = next(desktop_vault.rglob("*.md"))
        assert "written to the Windows Obsidian vault" in note.read_text(encoding="utf-8")
        document = client.get("/api/v1/admin/documents", headers=admin_headers).json()["items"][0]
        assert document["status"] == "synced"

        missing = client.post(
            "/api/v1/agent/documents/missing",
            headers=agent_headers,
            json={
                "root_id": registered_root["id"],
                "source_path": "Projects/remote-note.txt",
            },
        )
        assert missing.status_code == 200
        await runtime.process_commands_once()
        assert "obsync_status: source-missing" in note.read_text(encoding="utf-8")

    server = client.get("/api/v1/admin/server", headers=admin_headers).json()
    assert server["name"] == "Obsync server"
    assert (
        client.get("/api/v1/admin/overview", headers=admin_headers).json()["vault"]["mode"]
        == "agent"
    )


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


def test_inventory_compares_new_synced_modified_and_missing_states(
    client: TestClient,
    admin_headers: dict[str, str],
    agent_headers: dict[str, str],
    registered_root: dict,
    app_settings,
) -> None:
    original = b"Initial project record"
    discovered = inventory(
        client,
        agent_headers,
        registered_root["id"],
        "scan-new",
        {"Projects/record.txt": original},
    )
    assert discovered.status_code == 200, discovered.text
    assert discovered.json()["counts"] == {"new": 1}
    document = client.get(
        f"/api/v1/admin/documents?root_id={registered_root['id']}", headers=admin_headers
    ).json()["items"][0]
    assert document["comparison_status"] == "new"

    synced = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "Projects/record.txt",
        original,
    ).json()
    assert synced["comparison_status"] == "in-sync"
    note = app_settings.vault_path / synced["destination_path"]
    assert note.is_file()

    matching = inventory(
        client,
        agent_headers,
        registered_root["id"],
        "scan-matching",
        {"Projects/record.txt": original},
    ).json()
    assert matching["counts"] == {"in-sync": 1}

    changed = b"Revised project record"
    modified = inventory(
        client,
        agent_headers,
        registered_root["id"],
        "scan-modified",
        {"Projects/record.txt": changed},
    ).json()
    assert modified["counts"] == {"modified": 1}

    sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "Projects/record.txt",
        changed,
        mtime=200,
    )
    note.unlink()
    missing_note = inventory(
        client,
        agent_headers,
        registered_root["id"],
        "scan-note-missing",
        {"Projects/record.txt": changed},
    ).json()
    assert missing_note["counts"] == {"vault-missing": 1}

    missing_source = inventory(
        client,
        agent_headers,
        registered_root["id"],
        "scan-source-missing",
        {},
    ).json()
    assert missing_source["counts"] == {"source-missing": 1}


def test_inventory_adopts_existing_managed_note_without_overlap(
    client: TestClient,
    admin_headers: dict[str, str],
    agent_headers: dict[str, str],
    registered_root: dict,
    app_settings,
) -> None:
    from obsync.llm import Analysis
    from obsync.markdown import render_markdown

    content = b"Already represented in Obsidian"
    source_hash = hashlib.sha256(content).hexdigest()
    existing_path = app_settings.vault_path / "Existing" / "record.md"
    existing_path.parent.mkdir(parents=True)
    existing_path.write_text(
        render_markdown(
            document_id="historic-id",
            source_path="Projects/existing.txt",
            source_name="existing.txt",
            source_hash=source_hash,
            source_size=len(content),
            source_mtime_ns=1,
            machine_name="Test PC",
            root_name="Projects",
            mime_type="text/plain",
            extractor="text",
            extracted_text=content.decode(),
            extraction_warning="",
            truncated=False,
            analysis=Analysis(
                title="Existing Record",
                summary="Existing record",
                category="Documents",
                document_type="note",
                tags=["existing"],
                confidence=0.9,
            ),
        ),
        encoding="utf-8",
    )

    result = inventory(
        client,
        agent_headers,
        registered_root["id"],
        "adopt-existing",
        {"Projects/existing.txt": content},
    ).json()
    assert result["counts"] == {"in-sync": 1}
    document = client.get(
        f"/api/v1/admin/documents?root_id={registered_root['id']}", headers=admin_headers
    ).json()["items"][0]
    assert document["destination_path"] == "Existing/record.md"
    assert document["source_hash"] == source_hash
    assert len(list(app_settings.vault_path.rglob("*.md"))) == 1


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
