from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from obsync.agent import AgentConfig, AgentRuntime, AgentState, RootConfig


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
