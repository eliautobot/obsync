from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from obsync.agent import AgentConfig, AgentRuntime, AgentState
from obsync.llm import Analysis


def sync_text(
    client: TestClient,
    headers: dict[str, str],
    root_id: str,
    path: str,
    content: bytes,
    *,
    mtime: int = 100,
    previous_path: str = "",
    review_feedback: str = "",
    force_review: bool = False,
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
            "review_feedback": review_feedback,
            "force_review": str(force_review).lower(),
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
    assert client.get("/api/v1/admin/ai/activity/stream").status_code == 401


def test_ui_includes_guided_help_and_obsync_desktop(client: TestClient) -> None:
    index = client.get("/").text
    app_js = client.get("/assets/app.js").text
    styles = client.get("/assets/styles.css").text
    assert 'data-view="help"' in index
    assert 'popover="manual"' in index
    assert "renderHelp" in app_js
    assert "Download Obsync Desktop" in app_js
    assert "/api/v1/downloads/windows-desktop" in app_js
    download = client.get("/api/v1/downloads/windows-desktop", follow_redirects=False)
    assert download.status_code == 307
    assert "obsync-desktop-windows-x64.exe" in download.headers["location"]
    legacy = client.get("/api/v1/downloads/windows-companion", follow_redirects=False)
    assert legacy.headers["location"] == "/api/v1/downloads/windows-desktop"
    assert 'id="pipeline-toggle"' in index
    assert "Stop Global Sync" in index
    assert 'data-view="vault"' in index
    assert 'data-view="local-ai"' in index
    assert '<span id="version-label">v…</span>' in index
    assert '$("#version-label").textContent = `v${meta.version}`' in app_js
    assert "AI profiles" in app_js
    assert "Protected inference prompt" in app_js
    assert "Full document transfer" in app_js
    assert "Obsidian behaviors" in app_js
    assert "Start Index Sweep" in app_js
    assert "Start Maintenance Sweep" in app_js
    assert "Schedule Index Sweep" in app_js
    assert "Schedule Maintenance Sweep" in app_js
    assert "Allow AI Agent to apply all recommended changes" in app_js
    assert "Vault maintenance recommendations" in app_js
    assert "/api/v1/admin/vault/sweeps" in app_js
    assert "/api/v1/admin/vault/changes" in app_js
    assert "whole-vault index" in app_js
    assert "Please choose which Obsidian Vault your files will be synced to" in app_js
    assert "Run as administrator" in app_js
    assert "setInterval(liveRefresh, 3000)" in app_js
    assert 'new EventSource("/api/v1/admin/ai/activity/stream")' in app_js
    assert "state.aiEventSource.close()" in app_js
    assert 'window.addEventListener("pagehide", stopLiveUpdates)' in app_js
    assert 'if (state.view === "local-ai") updateAiActivity(activity)' in app_js
    assert "await refreshAiActivity();\n    } else if" not in app_js
    assert "ai-jump-latest" in app_js
    assert "state.aiFollow" in app_js
    assert "overviewSignature" in app_js
    assert "Current active file" in app_js
    assert "Stop inference" in app_js
    assert "Redo AI review" in app_js
    assert "Disregard" in app_js
    assert "captureScrollState" in app_js
    assert "document-table-panel" in app_js
    assert "Remove folder" in app_js
    assert "Keep that PowerShell window open" not in app_js
    assert ".help-tip" in styles
    assert ".ai-jump-latest" in styles
    assert "overflow-anchor: none" in styles
    assert "backdrop-filter: blur(3px)" not in styles
    assert ".sweep-progress" in styles
    assert ".diff-grid" in styles


def test_vault_sweep_settings_are_validated_and_saved(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.put(
        "/api/v1/admin/settings",
        headers=admin_headers,
        json={
            "vault_index_schedule_enabled": True,
            "vault_index_schedule_frequency": "weekly",
            "vault_index_schedule_time": "01:30",
            "vault_index_schedule_weekday": 5,
            "vault_index_schedule_interval_hours": 72,
            "vault_index_change_mode": "review",
            "vault_maintenance_schedule_enabled": True,
            "vault_maintenance_schedule_frequency": "monthly",
            "vault_maintenance_schedule_time": "03:15",
            "vault_maintenance_schedule_month_day": 12,
            "vault_maintenance_change_mode": "auto",
            "vault_schedule_timezone": "America/New_York",
            "vault_maintenance_categories": ["links", "tags"],
            "vault_link_minimum_score": 28,
            "vault_link_limit": 140,
            "existing_note_policy": "review",
        },
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["vault_index_schedule_enabled"] == "true"
    assert saved["vault_maintenance_change_mode"] == "auto"
    assert saved["vault_maintenance_categories"] == '["links", "tags"]'
    assert saved["vault_link_limit"] == "140"

    invalid = client.put(
        "/api/v1/admin/settings",
        headers=admin_headers,
        json={"vault_index_schedule_time": "25:99"},
    )
    assert invalid.status_code == 400
    invalid = client.put(
        "/api/v1/admin/settings",
        headers=admin_headers,
        json={"vault_maintenance_categories": ["delete-everything"]},
    )
    assert invalid.status_code == 400


def test_ai_profiles_are_copyable_editable_activatable_and_protected(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    profiles = client.get("/api/v1/admin/ai/profiles", headers=admin_headers).json()
    assert profiles["active_profile_id"] == "builtin-full-transfer"
    assert [item["id"] for item in profiles["items"][:2]] == [
        "builtin-full-transfer",
        "builtin-brief-summary",
    ]
    assert all(item["builtin"] for item in profiles["items"][:2])
    assert "Return exactly one JSON object" in profiles["protected_system_prompt"]
    assert "{{document_content}}" in profiles["prompt_placeholders"]

    copied = client.post(
        "/api/v1/admin/ai/profiles",
        headers=admin_headers,
        json={"source_profile_id": "builtin-full-transfer", "name": "Detailed legal files"},
    )
    assert copied.status_code == 200, copied.text
    custom = copied.json()
    assert custom["builtin"] is False
    assert custom["note_content_mode"] == "full"

    custom.update(
        {
            "description": "Keep complete legal records with custom connections.",
            "role_prompt": "Preserve every legal clause and connect matching clients.",
            "user_prompt_template": (
                "File {{source_path}} ({{mime_type}})\n{{candidate_notes}}\n"
                "{{document_content}}\nReviewer: {{review_feedback}}"
            ),
            "note_content_mode": "full-and-summary",
            "temperature": 0.25,
            "top_p": 0.8,
            "max_output_tokens": 6000,
            "input_char_limit": 750000,
            "candidate_limit": 150,
            "tag_limit": 14,
            "related_notes_limit": 12,
            "use_vault_context": True,
            "use_wikilinks": True,
            "use_tags": True,
            "use_properties": True,
            "organize_folders": False,
            "include_source_details": True,
        }
    )
    updated = client.put(
        f"/api/v1/admin/ai/profiles/{custom['id']}",
        headers=admin_headers,
        json=custom,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["max_output_tokens"] == 6000
    assert updated.json()["organize_folders"] is False

    activated = client.post(
        f"/api/v1/admin/ai/profiles/{custom['id']}/activate", headers=admin_headers
    )
    assert activated.status_code == 200
    assert activated.json()["active_profile_id"] == custom["id"]

    protected = client.put(
        "/api/v1/admin/ai/profiles/builtin-full-transfer",
        headers=admin_headers,
        json=custom,
    )
    assert protected.status_code == 400
    assert "cannot be edited" in protected.json()["detail"]
    missing_content = client.put(
        f"/api/v1/admin/ai/profiles/{custom['id']}",
        headers=admin_headers,
        json={**custom, "user_prompt_template": "No document placeholder"},
    )
    assert missing_content.status_code == 400
    assert "{{document_content}}" in missing_content.json()["detail"]

    deleted = client.delete(f"/api/v1/admin/ai/profiles/{custom['id']}", headers=admin_headers)
    assert deleted.status_code == 200
    assert deleted.json()["active_profile_id"] == "builtin-full-transfer"
    builtin_delete = client.delete(
        "/api/v1/admin/ai/profiles/builtin-brief-summary", headers=admin_headers
    )
    assert builtin_delete.status_code == 400


def test_exact_existing_note_is_adopted_then_updated_in_place(
    client: TestClient,
    admin_headers: dict[str, str],
    agent_headers: dict[str, str],
    registered_root: dict,
    app_settings,
) -> None:
    existing = app_settings.vault_path / "Accounts" / "Water Account.md"
    existing.parent.mkdir(parents=True)
    original = "Account number WTR-9911 active."
    existing.write_text(original, encoding="utf-8")

    first = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "Water Account.txt",
        original.encode(),
    )
    assert first.status_code == 200, first.text
    assert first.json()["result"] == "synced"
    assert first.json()["destination_path"] == "Accounts/Water Account.md"
    assert len(list(app_settings.vault_path.rglob("*.md"))) == 1
    adopted = existing.read_text(encoding="utf-8")
    assert "obsync_id:" in adopted
    assert "## Document content\n\nAccount number WTR-9911 active." in adopted
    assert "The original vault note below is preserved" in adopted
    assert adopted.rstrip().endswith(original)

    updated_source = b"Account number WTR-9911 active. Mailing address changed to 22 Bay Street."
    second = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "Water Account.txt",
        updated_source,
        mtime=200,
    )
    assert second.status_code == 200, second.text
    assert second.json()["result"] == "synced"
    assert second.json()["destination_path"] == "Accounts/Water Account.md"
    final = existing.read_text(encoding="utf-8")
    assert "Mailing address changed to 22 Bay Street." in final
    assert final.rstrip().endswith(original)
    assert len(list(app_settings.vault_path.rglob("*.md"))) == 1

    documents = client.get("/api/v1/admin/documents", headers=admin_headers).json()["items"]
    assert len(documents) == 1
    assert documents[0]["vault_adopted"] == 1
    assert documents[0]["comparison_status"] == "in-sync"


def test_ai_activity_event_stream_formats_events_and_closes_gateway_subscription(
    app, client: TestClient, admin_headers: dict[str, str], monkeypatch
) -> None:
    closed = False

    async def finite_activity_stream():
        nonlocal closed
        try:
            yield {
                "active": [],
                "last": None,
                "provider": "ollama",
                "model": "stream-model",
                "enabled": True,
                "revision": 42,
            }
            yield None
        finally:
            closed = True

    monkeypatch.setattr(app.state.service, "stream_ai_activity", finite_activity_stream)
    response = client.get("/api/v1/admin/ai/activity/stream", headers=admin_headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert "retry: 1000\n\n" in response.text
    assert "id: 42\nevent: ai-activity\ndata:" in response.text
    assert '"model":"stream-model"' in response.text
    assert ": keep-alive\n\n" in response.text
    assert closed is True


def test_pipeline_can_stop_cancel_pending_work_and_resume(
    client: TestClient,
    admin_headers: dict[str, str],
    agent_headers: dict[str, str],
    enrolled_agent: dict[str, str],
    registered_root: dict,
) -> None:
    queued = client.post(
        f"/api/v1/admin/agents/{enrolled_agent['agent_id']}/scan", headers=admin_headers
    ).json()
    stopped = client.post("/api/v1/admin/pipeline/stop", headers=admin_headers)
    assert stopped.status_code == 200
    assert stopped.json()["enabled"] is False
    assert stopped.json()["cancelled_commands"] == 1
    status = client.get("/api/v1/admin/pipeline", headers=admin_headers)
    assert status.json()["state"] == "stopped"
    command = client.get(f"/api/v1/admin/commands/{queued['id']}", headers=admin_headers).json()
    assert command["status"] == "cancelled"
    assert command["result"] == "Stopped by user"
    client.post(
        f"/api/v1/agent/commands/{queued['id']}/complete",
        headers=agent_headers,
        json={"ok": True, "result": "late result"},
    )
    command = client.get(f"/api/v1/admin/commands/{queued['id']}", headers=admin_headers).json()
    assert command["status"] == "cancelled"

    heartbeat = client.post("/api/v1/agent/heartbeat", headers=agent_headers, json={})
    assert heartbeat.json()["sync_enabled"] is False
    blocked = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "blocked.txt",
        b"This must not be processed while stopped.",
    )
    assert blocked.status_code == 409
    assert "Global syncing is stopped" in blocked.json()["detail"]

    started = client.post("/api/v1/admin/pipeline/start", headers=admin_headers)
    assert started.status_code == 200
    assert started.json()["enabled"] is True
    assert started.json()["reconciliations"] == 1
    synced = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "resumed.txt",
        b"Processing resumes safely.",
    )
    assert synced.status_code == 200
    assert synced.json()["result"] == "synced"


def test_each_folder_can_pause_stop_and_reconcile_independently(
    client: TestClient,
    admin_headers: dict[str, str],
    agent_headers: dict[str, str],
    registered_root: dict,
) -> None:
    queued = client.post(f"/api/v1/admin/roots/{registered_root['id']}/scan", headers=admin_headers)
    assert queued.status_code == 200

    paused = client.post(
        f"/api/v1/admin/roots/{registered_root['id']}/state",
        headers=admin_headers,
        json={"sync_state": "paused"},
    )
    assert paused.status_code == 200
    assert paused.json()["sync_state"] == "paused"
    assert paused.json()["cancelled_commands"] == 1
    blocked = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "paused.txt",
        b"This folder alone is paused.",
    )
    assert blocked.status_code == 409
    assert "paused" in blocked.json()["detail"]

    stopped = client.post(
        f"/api/v1/admin/roots/{registered_root['id']}/state",
        headers=admin_headers,
        json={"sync_state": "stopped"},
    )
    assert stopped.status_code == 200
    assert stopped.json()["sync_state"] == "stopped"

    started = client.post(
        f"/api/v1/admin/roots/{registered_root['id']}/state",
        headers=admin_headers,
        json={"sync_state": "running"},
    )
    assert started.status_code == 200
    assert started.json()["sync_state"] == "running"
    resumed = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "resumed-folder.txt",
        b"Only this folder resumed.",
    )
    assert resumed.status_code == 200
    commands = client.get("/api/v1/agent/commands", headers=agent_headers).json()["items"]
    state_commands = [item for item in commands if item["command"] == "set_root_state"]
    assert [item["payload"]["sync_state"] for item in state_commands] == [
        "paused",
        "stopped",
        "running",
    ]


def test_existing_vault_title_is_held_for_duplicate_review(
    client: TestClient,
    admin_headers: dict[str, str],
    agent_headers: dict[str, str],
    registered_root: dict,
    app_settings,
) -> None:
    existing = app_settings.vault_path / "Reference" / "12_Laws_and_Rules.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("# Laws and Rules\n\nExisting curated information.\n", encoding="utf-8")
    content = b"New source material about laws and rules."

    found = inventory(
        client,
        agent_headers,
        registered_root["id"],
        "duplicate-scan",
        {"Laws and Rules.txt": content},
    )
    assert found.status_code == 200
    assert found.json()["counts"] == {"possible-duplicate": 1}
    document = client.get(
        "/api/v1/admin/documents?comparison_status=possible-duplicate",
        headers=admin_headers,
    ).json()["items"][0]
    assert document["status"] == "duplicate-review"
    assert document["duplicate_path"] == "Reference/12_Laws_and_Rules.md"

    held = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "Laws and Rules.txt",
        content,
    )
    assert held.status_code == 200
    assert held.json()["result"] == "possible-duplicate"
    assert list(app_settings.vault_path.rglob("*.md")) == [existing]

    allowed = client.post(
        f"/api/v1/admin/documents/{document['id']}/allow-duplicate",
        headers=admin_headers,
    )
    assert allowed.status_code == 200
    created = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "Laws and Rules.txt",
        content,
    )
    assert created.status_code == 200
    assert created.json()["result"] == "synced"
    assert len(list(app_settings.vault_path.rglob("*.md"))) == 2


def test_removing_folder_keeps_source_and_obsidian_note(
    client: TestClient,
    admin_headers: dict[str, str],
    agent_headers: dict[str, str],
    registered_root: dict,
    app_settings,
    tmp_path: Path,
) -> None:
    source = tmp_path / "Computer Folder" / "record.txt"
    source.parent.mkdir()
    source.write_text("Original computer file", encoding="utf-8")
    synced = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "record.txt",
        source.read_bytes(),
    ).json()
    note = app_settings.vault_path / synced["destination_path"]
    assert source.is_file() and note.is_file()

    removed = client.delete(f"/api/v1/admin/roots/{registered_root['id']}", headers=admin_headers)
    assert removed.status_code == 200
    assert removed.json()["removed_documents"] == 1
    assert source.read_text(encoding="utf-8") == "Original computer file"
    assert note.is_file()
    assert client.get("/api/v1/admin/roots", headers=admin_headers).json()["items"] == []
    assert client.get("/api/v1/admin/documents", headers=admin_headers).json()["total"] == 0
    commands = client.get("/api/v1/agent/commands", headers=agent_headers).json()["items"]
    assert commands[-1]["command"] == "remove_root"
    assert commands[-1]["payload"]["root_key"] == registered_root["root_key"]
    missing = client.delete(f"/api/v1/admin/roots/{registered_root['id']}", headers=admin_headers)
    assert missing.status_code == 404


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


def test_enrollment_retry_with_same_client_credential_is_idempotent(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    enrollment = client.post(
        "/api/v1/admin/enrollments", headers=admin_headers, json={"label": "Retry PC"}
    ).json()
    credential = "agent_" + "r" * 48
    payload = {
        "code": enrollment["code"],
        "name": "Retry PC",
        "hostname": "retry-pc",
        "os_name": "Windows",
        "agent_token": credential,
    }
    first = client.post("/api/v1/agents/register", json=payload)
    second = client.post("/api/v1/agents/register", json=payload)
    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    payload["agent_token"] = "agent_" + "x" * 48
    rejected = client.post("/api/v1/agents/register", json=payload)
    assert rejected.status_code == 400


def test_admin_disconnects_computer_and_revokes_device(
    client: TestClient,
    admin_headers: dict[str, str],
    enrolled_agent: dict[str, str],
    agent_headers: dict[str, str],
    registered_root: dict,
) -> None:
    response = client.delete(
        f"/api/v1/admin/agents/{enrolled_agent['agent_id']}", headers=admin_headers
    )
    assert response.status_code == 200
    assert response.json()["removed_roots"] == 1
    assert response.json()["removed_documents"] == 0
    assert client.get("/api/v1/admin/agents", headers=admin_headers).json()["items"] == []
    assert client.post("/api/v1/agent/heartbeat", headers=agent_headers, json={}).status_code == 401
    assert client.get("/api/v1/admin/roots", headers=admin_headers).json()["items"] == []


def test_active_vault_writer_must_be_changed_before_disconnect(
    client: TestClient,
    admin_headers: dict[str, str],
    enrolled_agent: dict[str, str],
) -> None:
    heartbeat = client.post(
        "/api/v1/agent/heartbeat",
        headers={"Authorization": f"Bearer {enrolled_agent['agent_token']}"},
        json={"vault_path": "C:\\Vault", "vault_ready": True},
    )
    assert heartbeat.status_code == 200
    settings = client.put(
        "/api/v1/admin/settings",
        headers=admin_headers,
        json={"vault_mode": "agent", "vault_agent_id": enrolled_agent["agent_id"]},
    )
    assert settings.status_code == 200
    response = client.delete(
        f"/api/v1/admin/agents/{enrolled_agent['agent_id']}", headers=admin_headers
    )
    assert response.status_code == 409
    assert "active vault writer" in response.json()["detail"]


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


def test_complete_review_controls_store_feedback_and_queue_forced_ai_review(
    client: TestClient,
    admin_headers: dict[str, str],
    agent_headers: dict[str, str],
    registered_root: dict,
    app,
    monkeypatch,
) -> None:
    first = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "review-controls.txt",
        b"A permit renewal record that needs reviewer guidance.",
    ).json()
    document_id = first["id"]

    approved = client.post(f"/api/v1/admin/documents/{document_id}/approve", headers=admin_headers)
    assert approved.status_code == 200
    approved_row = app.state.service.db.query_one(
        "SELECT * FROM documents WHERE id = ?", (document_id,)
    )
    assert approved_row["needs_review"] == 0
    assert approved_row["review_resolution"] == "approved"

    disregarded = client.post(
        f"/api/v1/admin/documents/{document_id}/disregard", headers=admin_headers
    )
    assert disregarded.status_code == 200
    ignored_row = app.state.service.db.query_one(
        "SELECT * FROM documents WHERE id = ?", (document_id,)
    )
    assert ignored_row["status"] == "ignored"
    assert ignored_row["comparison_status"] == "ignored"
    assert ignored_row["needs_review"] == 0
    ignored_again = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "review-controls.txt",
        b"A permit renewal record that needs reviewer guidance.",
        mtime=101,
    )
    assert ignored_again.status_code == 200
    assert ignored_again.json()["result"] == "ignored"
    assert ignored_again.json()["status"] == "ignored"

    app.state.service.update_settings(
        {
            "llm_enabled": True,
            "llm_provider": "ollama",
            "llm_base_url": "http://model:11434",
            "llm_model": "review-model",
        }
    )
    feedback = "Use the permit-renewal tag and the Licenses category."
    redone = client.post(
        f"/api/v1/admin/documents/{document_id}/redo-review",
        headers=admin_headers,
        json={"feedback": feedback},
    )
    assert redone.status_code == 200
    assert redone.json()["command"]["command"] == "resync"
    queued = client.get(
        f"/api/v1/admin/commands/{redone.json()['command']['id']}", headers=admin_headers
    ).json()
    assert queued["payload"]["force_review"] is True
    assert queued["payload"]["review_feedback"] == feedback
    review_row = app.state.service.db.query_one(
        "SELECT * FROM documents WHERE id = ?", (document_id,)
    )
    assert review_row["status"] == "review-queued"
    assert review_row["review_feedback"] == feedback

    seen: dict[str, str] = {}

    async def analyze_again(*_args, **kwargs):
        seen["feedback"] = kwargs.get("review_feedback", "")
        return Analysis(
            title="Permit Renewal",
            summary="Reviewed with administrator feedback.",
            category="Licenses",
            document_type="report",
            tags=["permit-renewal"],
            confidence=0.95,
            provider="ollama",
            model="review-model",
        )

    monkeypatch.setattr("obsync.service.LLMAnalyzer.analyze", analyze_again)
    # A vault inventory may refresh the row to in-sync before the desktop consumes
    # the re-review command. force_review must still bypass the unchanged shortcut.
    app.state.service.db.execute(
        "UPDATE documents SET comparison_status = 'in-sync' WHERE id = ?", (document_id,)
    )
    forced = sync_text(
        client,
        agent_headers,
        registered_root["id"],
        "review-controls.txt",
        b"A permit renewal record that needs reviewer guidance.",
        review_feedback=feedback,
        force_review=True,
    )
    assert forced.status_code == 200
    assert forced.json()["result"] == "synced"
    assert seen["feedback"] == feedback
    assert forced.json()["category"] == "Licenses"
    assert forced.json()["tags"] == ["permit-renewal"]


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
