from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from obsync.security import hash_token


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


@pytest.mark.asyncio
async def test_incomplete_llm_settings_report_disabled(app) -> None:
    result = await app.state.service.test_llm()
    assert result["ok"] is False
