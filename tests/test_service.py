from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from obsync.config import Settings
from obsync.db import Database
from obsync.security import hash_token
from obsync.service import ObsyncService, PipelinePausedError


def test_expired_enrollment_is_rejected(app) -> None:
    service = app.state.service
    enrollment = service.create_enrollment("Expired")
    service.db.execute(
        "UPDATE enrollments SET expires_at = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), enrollment["id"]),
    )
    with pytest.raises(ValueError, match="expired"):
        service.register_agent(enrollment["code"], {"name": "Late PC"})


def test_agent_authentication_and_offline_status(app) -> None:
    service = app.state.service
    enrollment = service.create_enrollment("PC")
    result = service.register_agent(
        enrollment["code"],
        {"name": "PC", "hostname": "pc", "os_name": "Linux"},
    )
    assert service.authenticate_agent(result["agent_token"])["id"] == result["agent_id"]
    assert service.authenticate_agent("wrong") is None
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    service.db.execute("UPDATE agents SET last_seen_at = ? WHERE id = ?", (old, result["agent_id"]))
    assert service.list_agents()[0]["status"] == "offline"


def test_disabled_agent_cannot_authenticate(app) -> None:
    service = app.state.service
    token = "agent_token"
    now = datetime.now(UTC).isoformat()
    service.db.execute(
        """
        INSERT INTO agents(id, name, hostname, os_name, token_hash, enabled,
                           last_seen_at, created_at, updated_at)
        VALUES ('disabled', 'Disabled', 'disabled', 'Linux', ?, 0, ?, ?, ?)
        """,
        (hash_token(token), now, now, now),
    )
    assert service.authenticate_agent(token) is None


def test_concurrent_registration_retries_create_one_computer(app) -> None:
    service = app.state.service
    enrollment = service.create_enrollment("Concurrent PC")
    credential = "agent_" + "c" * 48

    def register(_attempt: int):
        return service.register_agent(
            enrollment["code"],
            {
                "name": "Concurrent PC",
                "hostname": "concurrent-pc",
                "os_name": "Windows",
                "agent_token": credential,
            },
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(register, range(40)))
    assert len({result["agent_id"] for result in results}) == 1
    assert len(service.list_agents()) == 1


def test_repeated_pair_disconnect_cycles_leave_no_stale_computers(app) -> None:
    service = app.state.service
    for index in range(30):
        enrollment = service.create_enrollment(f"Stress PC {index}")
        result = service.register_agent(
            enrollment["code"],
            {
                "name": f"Stress PC {index}",
                "agent_token": f"agent_{index:02d}_" + "s" * 40,
            },
        )
        disconnected = service.disconnect_agent(result["agent_id"])
        assert disconnected["ok"] is True
    assert service.list_agents() == []
    assert service.db.query_one("SELECT count(*) AS count FROM roots")["count"] == 0
    assert service.db.query_one("SELECT count(*) AS count FROM documents")["count"] == 0
    assert service.db.query_one("SELECT count(*) AS count FROM enrollments")["count"] == 0


@pytest.mark.asyncio
async def test_ai_activity_stream_pushes_each_inference_update_and_cleans_up(app) -> None:
    service = app.state.service
    stream = service.stream_ai_activity(keepalive_seconds=1)
    initial = await anext(stream)
    assert initial is not None
    assert service.ai_activity_subscriber_count == 1

    task = asyncio.current_task()
    assert task is not None
    service._start_activity(
        "stream-document",
        root_id="stream-root",
        source_path="live/model-output.txt",
        agent_name="Test PC",
        root_name="Live Files",
        task=task,
    )
    service._processing_activity["stream-document"]["used_ai"] = True
    service._processing_activity["stream-document"]["ai_active"] = True
    service._notify_ai_activity()
    started = await asyncio.wait_for(anext(stream), timeout=1)
    assert started is not None
    assert started["revision"] > initial["revision"]

    service._update_activity(
        "stream-document",
        "reasoning",
        "Checking the vault index now.",
        phase="inference",
        phase_label="Local AI is reviewing the file",
    )
    reasoning = await asyncio.wait_for(anext(stream), timeout=1)
    assert reasoning is not None
    assert reasoning["active"][0]["events"][-1]["message"] == "Checking the vault index now."
    assert reasoning["active"][0]["phase"] == "inference"

    await stream.aclose()
    service._processing_activity.pop("stream-document", None)
    assert service.ai_activity_subscriber_count == 0


@pytest.mark.asyncio
async def test_gateway_stream_disconnect_stress_leaves_zero_subscribers(app) -> None:
    service = app.state.service

    for _cycle in range(50):
        stream = service.stream_ai_activity(keepalive_seconds=0.01)
        assert await anext(stream) is not None
        assert service.ai_activity_subscriber_count == 1
        await stream.aclose()
        assert service.ai_activity_subscriber_count == 0

    streams = [service.stream_ai_activity(keepalive_seconds=0.01) for _ in range(25)]
    await asyncio.gather(*(anext(stream) for stream in streams))
    assert service.ai_activity_subscriber_count == 25
    await asyncio.gather(*(stream.aclose() for stream in streams))
    assert service.ai_activity_subscriber_count == 0


@pytest.mark.asyncio
async def test_ai_activity_stream_keepalive_also_cleans_up(app) -> None:
    service = app.state.service
    stream = service.stream_ai_activity(keepalive_seconds=0.001)
    assert await anext(stream) is not None
    assert await asyncio.wait_for(anext(stream), timeout=1) is None
    await stream.aclose()
    assert service.ai_activity_subscriber_count == 0


@pytest.mark.asyncio
async def test_incomplete_llm_settings_report_disabled(app) -> None:
    result = await app.state.service.test_llm()
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_stopping_pipeline_cancels_active_ai_processing(app, tmp_path, monkeypatch) -> None:
    service = app.state.service
    enrollment = service.create_enrollment("AI PC")
    registered = service.register_agent(
        enrollment["code"],
        {"name": "AI PC", "hostname": "ai-pc", "os_name": "Windows"},
    )
    agent = service.db.query_one("SELECT * FROM agents WHERE id = ?", (registered["agent_id"],))
    root = service.upsert_root(
        registered["agent_id"],
        {
            "root_key": "ai-root",
            "name": "AI Files",
            "path": str(tmp_path),
            "destination": "Obsync",
        },
    )
    staged = tmp_path / "active.txt"
    staged.write_text("Wait for a deliberately slow AI review", encoding="utf-8")
    started = asyncio.Event()

    async def wait_forever(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("obsync.service.LLMAnalyzer.analyze", wait_forever)
    task = asyncio.create_task(
        service.process_file(
            agent=agent,
            root_id=root["id"],
            source_path="active.txt",
            source_mtime_ns=1,
            source_size=staged.stat().st_size,
            staged_file=staged,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    stopped = service.pause_pipeline()
    assert stopped["active_jobs"] == 1
    with pytest.raises(PipelinePausedError, match="stopped"):
        await task
    document = service.db.query_one("SELECT * FROM documents WHERE source_path = 'active.txt'")
    assert document["status"] == "paused"
    assert document["error"] == "Stopped by user"
    assert list(service.settings.vault_path.rglob("*.md")) == []


@pytest.mark.asyncio
async def test_stopping_only_active_inference_keeps_global_sync_running(
    app, tmp_path, monkeypatch
) -> None:
    service = app.state.service
    service.update_settings(
        {
            "llm_enabled": True,
            "llm_provider": "ollama",
            "llm_base_url": "http://model:11434",
            "llm_model": "slow-model",
        }
    )
    enrollment = service.create_enrollment("Inference PC")
    registered = service.register_agent(
        enrollment["code"],
        {"name": "Inference PC", "hostname": "inference-pc", "os_name": "Windows"},
    )
    agent = service.db.query_one("SELECT * FROM agents WHERE id = ?", (registered["agent_id"],))
    root = service.upsert_root(
        registered["agent_id"],
        {
            "root_key": "inference-root",
            "name": "Inference Files",
            "path": str(tmp_path),
            "destination": "Obsync",
        },
    )
    staged = tmp_path / "live-inference.txt"
    staged.write_text("Wait for the local model", encoding="utf-8")
    started = asyncio.Event()

    async def wait_forever(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("obsync.service.LLMAnalyzer.analyze", wait_forever)
    task = asyncio.create_task(
        service.process_file(
            agent=agent,
            root_id=root["id"],
            source_path="live-inference.txt",
            source_mtime_ns=1,
            source_size=staged.stat().st_size,
            staged_file=staged,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)

    activity = service.ai_activity()
    assert activity["active"][0]["source_name"] == "live-inference.txt"
    assert activity["active"][0]["phase"] == "inference"
    assert service.overview()["active_work"][0]["source_name"] == "live-inference.txt"
    stopped = service.stop_inference(activity["active"][0]["document_id"])
    assert stopped["stopped"] == 1
    assert service.pipeline_enabled() is True

    with pytest.raises(PipelinePausedError, match="AI inference"):
        await task
    document = service.db.query_one(
        "SELECT * FROM documents WHERE source_path = 'live-inference.txt'"
    )
    assert document["status"] == "ai-stopped"
    assert document["needs_review"] == 1
    assert document["error"] == "AI inference stopped by user"
    assert service.ai_activity()["last"]["outcome"] == "ai-stopped"


def test_stopping_pipeline_cancels_pending_desktop_note_write(app) -> None:
    service = app.state.service
    enrollment = service.create_enrollment("Vault PC")
    registered = service.register_agent(enrollment["code"], {"name": "Vault PC"})
    queued = service.queue_command(
        registered["agent_id"],
        "write_note",
        {"document_id": "document-waiting-for-desktop"},
    )

    stopped = service.pause_pipeline()

    assert stopped["cancelled_commands"] == 1
    command = service.db.query_one("SELECT * FROM commands WHERE id = ?", (queued["id"],))
    assert command is not None
    assert command["status"] == "cancelled"
    assert command["result"] == "Stopped by user"


def test_first_run_requires_explicit_vault_choice_before_global_sync(tmp_path) -> None:
    service = ObsyncService(
        Settings(
            data_dir=tmp_path / "data",
            vault_path=tmp_path / "vault",
            admin_token="",
        )
    )
    assert service.pipeline_status()["enabled"] is False
    assert service.settings_for_ui()["vault_confirmed"] == "false"
    with pytest.raises(ValueError, match="Choose and save an Obsidian Vault"):
        service.resume_pipeline()

    saved = service.update_settings({"vault_mode": "local"})
    assert saved["vault_confirmed"] == "true"
    started = service.resume_pipeline()
    assert started["enabled"] is True
    assert started["reconciliations"] == 0


def test_upgrade_stops_existing_pipeline_until_vault_is_reconfirmed(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        vault_path=tmp_path / "vault",
        admin_token="",
    )
    settings.prepare()
    database = Database(settings.database_path)
    database.initialize()
    database.set_settings({"sync_enabled": ("true", False)})

    service = ObsyncService(settings, db=database)
    assert service.pipeline_status()["enabled"] is False
    assert service.settings_for_ui()["vault_confirmed"] == "false"


def test_vault_and_ai_settings_validate_unsafe_or_invalid_choices(app) -> None:
    service = app.state.service
    with pytest.raises(ValueError, match="Vault mode"):
        service.update_settings({"vault_mode": "unknown"})
    with pytest.raises(ValueError, match="connected computer"):
        service.update_settings({"vault_mode": "agent", "vault_agent_id": "missing"})
    with pytest.raises(ValueError, match="Duplicate policy"):
        service.update_settings({"duplicate_policy": "overwrite"})
    with pytest.raises(ValueError, match="8,000"):
        service.update_settings({"llm_instructions": "x" * 8001})

    enrollment = service.create_enrollment("Bad vault PC")
    registered = service.register_agent(enrollment["code"], {"name": "Bad vault PC"})
    service.heartbeat(
        registered["agent_id"],
        vault_path=r"C:\Notes\.obsidian",
        vault_ready=True,
    )
    with pytest.raises(ValueError, match="hidden .obsidian"):
        service.update_settings({"vault_mode": "agent", "vault_agent_id": registered["agent_id"]})

    saved = service.update_settings(
        {
            "llm_enabled": True,
            "llm_vault_context": False,
            "llm_api_key": "configured",
            "llm_instructions": "Use concise titles.",
        }
    )
    assert saved["llm_enabled"] == "true"
    assert saved["llm_vault_context"] == "false"
    assert saved["llm_instructions"] == "Use concise titles."
