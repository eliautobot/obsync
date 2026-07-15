from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from obsync.agent import AgentConfig, AgentRuntime, AgentState, RootConfig, SyncPausedError


@pytest.mark.asyncio
async def test_agent_scans_changes_and_missing_files(
    app,
    client,
    admin_headers,
    enrolled_agent,
    app_settings,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "first.txt").write_text("First note", encoding="utf-8")
    (source / "ignore.tmp").write_text("ignore", encoding="utf-8")
    root = RootConfig(
        root_key="agent-root",
        name="Agent Source",
        path=str(source),
        exclude_patterns=["**/*.tmp"],
    )
    config = AgentConfig(
        server_url="http://testserver",
        agent_id=enrolled_agent["agent_id"],
        agent_token=enrolled_agent["agent_token"],
        name="Test PC",
        settle_seconds=0.01,
        roots=[root],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {enrolled_agent['agent_token']}"},
    ) as async_client:
        runtime = AgentRuntime(
            config,
            state=AgentState(tmp_path / "agent-state.db"),
            client=async_client,
        )
        first = await runtime.scan_all()
        assert first["Agent Source"]["synced"] == 1
        assert first["Agent Source"]["files"] == 1

        second = await runtime.scan_all()
        assert second["Agent Source"]["synced"] == 0
        assert second["Agent Source"]["unchanged"] == 1

        (source / "first.txt").write_text("First note, updated", encoding="utf-8")
        third = await runtime.scan_all()
        assert third["Agent Source"]["synced"] == 1

        (source / "first.txt").unlink()
        fourth = await runtime.scan_all()
        assert fourth["Agent Source"]["files"] == 0

    docs = client.get("/api/v1/admin/documents", headers=admin_headers).json()["items"]
    assert len(docs) == 1
    assert docs[0]["missing"] == 1
    note = next(app_settings.vault_path.rglob("*.md")).read_text(encoding="utf-8")
    assert "source-missing" in note


@pytest.mark.asyncio
async def test_inventory_compare_and_sync_pending_lifecycle(
    app,
    client,
    admin_headers,
    enrolled_agent,
    app_settings,
    tmp_path: Path,
) -> None:
    source = tmp_path / "inventory-source"
    source.mkdir()
    source_file = source / "record.txt"
    source_file.write_text("Initial inventory record", encoding="utf-8")
    root = RootConfig(
        root_key="inventory-root",
        name="Inventory",
        path=str(source),
        destination="Knowledge",
    )
    config = AgentConfig(
        server_url="http://testserver",
        agent_id=enrolled_agent["agent_id"],
        agent_token=enrolled_agent["agent_token"],
        name="Test PC",
        settle_seconds=0.01,
        roots=[root],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {enrolled_agent['agent_token']}"},
    ) as async_client:
        runtime = AgentRuntime(
            config,
            state=AgentState(tmp_path / "inventory-state.db"),
            client=async_client,
        )
        await runtime.register_roots()
        first = await runtime.inventory_root(root)
        assert first["counts"] == {"new": 1}
        synced = await runtime.sync_pending_root(root)
        assert synced == {"synced": 1, "missing": 0, "errors": 0, "files": 1}
        matching = await runtime.inventory_root(root)
        assert matching["counts"] == {"in-sync": 1}

        source_file.write_text("Modified inventory record", encoding="utf-8")
        modified = await runtime.inventory_root(root)
        assert modified["counts"] == {"modified": 1}
        assert (await runtime.sync_pending_root(root))["synced"] == 1

        note = next(app_settings.vault_path.rglob("*.md"))
        note.unlink()
        absent = await runtime.inventory_root(root)
        assert absent["counts"] == {"vault-missing": 1}
        assert (await runtime.sync_pending_root(root))["synced"] == 1
        assert next(app_settings.vault_path.rglob("*.md")).is_file()

        source_file.unlink()
        removed = await runtime.inventory_root(root)
        assert removed["counts"] == {"source-missing": 1}
        pending = await runtime.sync_pending_root(root)
        assert pending["missing"] == 1

    document = client.get("/api/v1/admin/documents", headers=admin_headers).json()["items"][0]
    assert document["comparison_status"] == "source-missing"


def test_agent_config_round_trip_and_duplicate_root(tmp_path: Path) -> None:
    source = tmp_path / "folder"
    source.mkdir()
    config_path = tmp_path / "agent.yml"
    config = AgentConfig(server_url="http://server", agent_token="secret", name="Laptop")
    config.add_root(source, name="Docs", destination="Second Brain")
    config.save(config_path)
    loaded = AgentConfig.load(config_path)
    assert loaded.name == "Laptop"
    assert loaded.roots[0].name == "Docs"
    assert loaded.roots[0].destination == "Second Brain"
    with pytest.raises(ValueError, match="already watched"):
        loaded.add_root(source)

    vault = tmp_path / "vault"
    vault.mkdir()
    assert loaded.set_vault(vault) == vault.resolve()
    loaded.save(config_path)
    assert AgentConfig.load(config_path).vault_path == str(vault.resolve())
    with pytest.raises(ValueError, match="does not exist"):
        loaded.set_vault(tmp_path / "missing-vault")


@pytest.mark.asyncio
async def test_remote_vault_picker_command_updates_agent_and_server(
    app,
    client,
    admin_headers,
    enrolled_agent,
    tmp_path: Path,
    monkeypatch,
) -> None:
    selected_vault = tmp_path / "selected-vault"
    selected_vault.mkdir()
    config_path = tmp_path / "agent.yml"
    config = AgentConfig(
        server_url="http://testserver",
        agent_id=enrolled_agent["agent_id"],
        agent_token=enrolled_agent["agent_token"],
        name="Test PC",
    )
    config.save(config_path)
    monkeypatch.setattr("obsync.agent.choose_directory", lambda *_args: selected_vault)
    queued = client.post(
        f"/api/v1/admin/agents/{enrolled_agent['agent_id']}/select-vault",
        headers=admin_headers,
    )
    assert queued.status_code == 200

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {enrolled_agent['agent_token']}"},
    ) as async_client:
        runtime = AgentRuntime(config, client=async_client, config_path=config_path)
        await runtime.process_commands_once()

    assert AgentConfig.load(config_path).vault_path == str(selected_vault.resolve())
    agent = client.get("/api/v1/admin/agents", headers=admin_headers).json()["items"][0]
    assert agent["vault_ready"] == 1
    assert agent["vault_path"] == str(selected_vault.resolve())


@pytest.mark.asyncio
async def test_remote_folder_picker_registers_and_inventories_source(
    app,
    client,
    admin_headers,
    enrolled_agent,
    tmp_path: Path,
    monkeypatch,
) -> None:
    selected_source = tmp_path / "selected-source"
    selected_source.mkdir()
    (selected_source / "first.txt").write_text("A newly discovered file", encoding="utf-8")
    config_path = tmp_path / "agent.yml"
    config = AgentConfig(
        server_url="http://testserver",
        agent_id=enrolled_agent["agent_id"],
        agent_token=enrolled_agent["agent_token"],
        name="Test PC",
        settle_seconds=0.01,
    )
    config.save(config_path)
    monkeypatch.setattr("obsync.agent.choose_directory", lambda *_args: selected_source)
    queued = client.post(
        f"/api/v1/admin/agents/{enrolled_agent['agent_id']}/select-source",
        headers=admin_headers,
        json={"name": "Selected files", "destination": "Imported"},
    )
    assert queued.status_code == 200

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {enrolled_agent['agent_token']}"},
    ) as async_client:
        runtime = AgentRuntime(config, client=async_client, config_path=config_path)
        await runtime.process_commands_once()

    loaded = AgentConfig.load(config_path)
    assert len(loaded.roots) == 1
    assert loaded.roots[0].path == str(selected_source.resolve())
    roots = client.get("/api/v1/admin/roots", headers=admin_headers).json()["items"]
    assert roots[0]["name"] == "Selected files"
    assert roots[0]["new_count"] == 1
    command = client.get(
        f"/api/v1/admin/commands/{queued.json()['id']}", headers=admin_headers
    ).json()
    assert command["status"] == "completed"


@pytest.mark.asyncio
async def test_remote_folder_removal_forgets_local_watch_but_keeps_files(
    app,
    client,
    admin_headers,
    enrolled_agent,
    registered_root,
    tmp_path: Path,
) -> None:
    source = tmp_path / "kept-source"
    source.mkdir()
    source_file = source / "kept.txt"
    source_file.write_text("Keep this original", encoding="utf-8")
    config_path = tmp_path / "agent.yml"
    config = AgentConfig(
        server_url="http://testserver",
        agent_id=enrolled_agent["agent_id"],
        agent_token=enrolled_agent["agent_token"],
        name="Test PC",
        roots=[
            RootConfig(
                root_key=registered_root["root_key"],
                name=registered_root["name"],
                path=str(source),
            )
        ],
    )
    config.save(config_path)
    state = AgentState(tmp_path / "agent-state.db")
    state.mark_synced(registered_root["root_key"], "kept.txt", 1, 18, "a" * 64)

    removed = client.delete(f"/api/v1/admin/roots/{registered_root['id']}", headers=admin_headers)
    assert removed.status_code == 200

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {enrolled_agent['agent_token']}"},
    ) as async_client:
        runtime = AgentRuntime(
            config,
            client=async_client,
            config_path=config_path,
            state=state,
        )
        await runtime.process_commands_once()

    assert AgentConfig.load(config_path).roots == []
    assert state.all_for_root(registered_root["root_key"]) == []
    assert source_file.read_text(encoding="utf-8") == "Keep this original"
    command = client.get(
        f"/api/v1/admin/commands/{removed.json()['command_id']}", headers=admin_headers
    ).json()
    assert command["status"] == "completed"


@pytest.mark.asyncio
async def test_offline_folder_removal_is_reconciled_when_agent_reconnects(
    app,
    client,
    admin_headers,
    enrolled_agent,
    registered_root,
    tmp_path: Path,
) -> None:
    source = tmp_path / "offline-source"
    source.mkdir()
    config_path = tmp_path / "agent.yml"
    root = RootConfig(
        root_key=registered_root["root_key"],
        name=registered_root["name"],
        path=str(source),
    )
    config = AgentConfig(
        server_url="http://testserver",
        agent_id=enrolled_agent["agent_id"],
        agent_token=enrolled_agent["agent_token"],
        name="Test PC",
        roots=[root],
    )
    config.save(config_path)
    state = AgentState(tmp_path / "agent-state.db")
    state.mark_synced(root.root_key, "old.txt", 1, 3, "b" * 64)

    removed = client.delete(f"/api/v1/admin/roots/{registered_root['id']}", headers=admin_headers)
    assert removed.status_code == 200

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {enrolled_agent['agent_token']}"},
    ) as async_client:
        runtime = AgentRuntime(
            config,
            client=async_client,
            config_path=config_path,
            state=state,
        )
        await runtime.register_roots()
        assert runtime.config.roots == []
        assert runtime._root_ids == {}
        await runtime.process_commands_once()

    assert AgentConfig.load(config_path).roots == []
    assert state.all_for_root(root.root_key) == []
    command = client.get(
        f"/api/v1/admin/commands/{removed.json()['command_id']}", headers=admin_headers
    ).json()
    assert command["status"] == "completed"


@pytest.mark.asyncio
async def test_agent_heartbeat_obeys_global_pipeline_state(
    app,
    client,
    admin_headers,
    enrolled_agent,
    tmp_path: Path,
) -> None:
    source = tmp_path / "paused-source"
    source.mkdir()
    root = RootConfig(root_key="paused-root", name="Paused", path=str(source))
    config = AgentConfig(
        server_url="http://testserver",
        agent_id=enrolled_agent["agent_id"],
        agent_token=enrolled_agent["agent_token"],
        name="Test PC",
        roots=[root],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {enrolled_agent['agent_token']}"},
    ) as async_client:
        runtime = AgentRuntime(config, client=async_client)
        client.post("/api/v1/admin/pipeline/stop", headers=admin_headers)
        assert (await runtime.heartbeat_once())["sync_enabled"] is False
        with pytest.raises(SyncPausedError, match="stopped"):
            await runtime.inventory_root(root)

        client.post("/api/v1/admin/pipeline/start", headers=admin_headers)
        assert (await runtime.heartbeat_once())["sync_enabled"] is True


@pytest.mark.asyncio
async def test_desktop_vault_audit_reports_matching_modified_and_missing_notes(
    app,
    client,
    admin_headers,
    enrolled_agent,
    agent_headers,
    registered_root,
    tmp_path: Path,
) -> None:
    from obsync.llm import Analysis
    from obsync.markdown import render_markdown

    desktop_vault = tmp_path / "desktop-vault"
    desktop_vault.mkdir()
    matching_content = "matching"
    matching_hash = hashlib.sha256(matching_content.encode()).hexdigest()
    note = desktop_vault / "Imported" / "matching.md"
    note.parent.mkdir()
    note.write_text(
        render_markdown(
            document_id="historic",
            source_path="matching.txt",
            source_name="matching.txt",
            source_hash=matching_hash,
            source_size=len(matching_content),
            source_mtime_ns=1,
            machine_name="Test PC",
            root_name="Projects",
            mime_type="text/plain",
            extractor="text",
            extracted_text=matching_content,
            extraction_warning="",
            truncated=False,
            analysis=Analysis(
                title="Matching",
                summary="Matching",
                category="Documents",
                document_type="note",
                tags=["matching"],
                confidence=0.9,
            ),
        ),
        encoding="utf-8",
    )
    client.post(
        "/api/v1/agent/heartbeat",
        headers=agent_headers,
        json={"vault_path": str(desktop_vault), "vault_ready": True},
    )
    client.put(
        "/api/v1/admin/settings",
        headers=admin_headers,
        json={"vault_mode": "agent", "vault_agent_id": enrolled_agent["agent_id"]},
    )
    response = client.post(
        "/api/v1/agent/inventory",
        headers=agent_headers,
        json={
            "root_id": registered_root["id"],
            "scan_id": "remote-audit",
            "complete": True,
            "items": [
                {
                    "source_path": "matching.txt",
                    "source_mtime_ns": 1,
                    "source_size": len(matching_content),
                    "sha256": matching_hash,
                },
                {
                    "source_path": "new.txt",
                    "source_mtime_ns": 1,
                    "source_size": 3,
                    "sha256": hashlib.sha256(b"new").hexdigest(),
                },
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["audit_commands"]

    config = AgentConfig(
        server_url="http://testserver",
        agent_id=enrolled_agent["agent_id"],
        agent_token=enrolled_agent["agent_token"],
        name="Test PC",
        vault_path=str(desktop_vault),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {enrolled_agent['agent_token']}"},
    ) as async_client:
        await AgentRuntime(config, client=async_client).process_commands_once()

    docs = client.get("/api/v1/admin/documents", headers=admin_headers).json()["items"]
    states = {item["source_path"]: item["comparison_status"] for item in docs}
    assert states == {"matching.txt": "in-sync", "new.txt": "new"}
    matching = next(item for item in docs if item["source_path"] == "matching.txt")
    assert matching["destination_path"] == "Imported/matching.md"
