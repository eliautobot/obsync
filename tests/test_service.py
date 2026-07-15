from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from obsync.security import hash_token
from obsync.service import PipelinePausedError


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
