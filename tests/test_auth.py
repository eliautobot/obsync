from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from obsync.api import create_app
from obsync.config import Settings

PASSWORD = "correct horse battery staple"


def make_client(tmp_path: Path, *, legacy_token: str = "") -> tuple[TestClient, object]:
    settings = Settings(
        data_dir=tmp_path / "data",
        vault_path=tmp_path / "vault",
        admin_token=legacy_token,
    )
    app = create_app(settings)
    return TestClient(app), app


def setup_account(client: TestClient, *, legacy_token: str = "") -> str:
    response = client.post(
        "/api/v1/auth/setup",
        json={
            "username": "admin",
            "password": PASSWORD,
            "legacy_token": legacy_token,
            "remember": True,
        },
    )
    assert response.status_code == 200, response.text
    csrf_token = client.cookies.get("obsync_csrf")
    assert csrf_token
    return csrf_token


def test_first_run_setup_uses_hashed_credentials_and_secure_cookies(tmp_path: Path) -> None:
    client, app = make_client(tmp_path)
    assert client.get("/api/v1/auth/status").json() == {
        "setup_required": True,
        "legacy_migration_required": False,
    }
    response = client.post(
        "/api/v1/auth/setup",
        json={"username": "owner", "password": PASSWORD, "remember": True},
    )
    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert any("obsync_session=" in value and "HttpOnly" in value for value in cookies)
    assert all("SameSite=strict" in value for value in cookies)
    session_token = client.cookies.get("obsync_session")
    csrf_token = client.cookies.get("obsync_csrf")
    assert session_token and csrf_token

    user = app.state.service.db.query_one("SELECT * FROM admin_users")
    session = app.state.service.db.query_one("SELECT * FROM admin_sessions")
    assert user["username"] == "owner"
    assert user["password_hash"] != PASSWORD
    assert session["token_hash"] != session_token
    assert session["csrf_hash"] != csrf_token
    assert client.get("/api/v1/admin/session").json()["username"] == "owner"


def test_legacy_token_is_required_once_then_disabled(tmp_path: Path) -> None:
    client, _app = make_client(tmp_path, legacy_token="old-admin-token")
    status = client.get("/api/v1/auth/status").json()
    assert status["setup_required"] is True
    assert status["legacy_migration_required"] is True
    assert (
        client.get(
            "/api/v1/admin/overview",
            headers={"Authorization": "Bearer old-admin-token"},
        ).status_code
        == 200
    )
    wrong = client.post(
        "/api/v1/auth/setup",
        json={"username": "admin", "password": PASSWORD, "legacy_token": "wrong"},
    )
    assert wrong.status_code == 401
    setup_account(client, legacy_token="old-admin-token")
    client.cookies.clear()
    assert (
        client.get(
            "/api/v1/admin/overview",
            headers={"Authorization": "Bearer old-admin-token"},
        ).status_code
        == 401
    )


def test_login_csrf_logout_and_expired_session(tmp_path: Path) -> None:
    client, app = make_client(tmp_path)
    csrf_token = setup_account(client)
    assert client.post("/api/v1/admin/enrollments", json={}).status_code == 403
    assert (
        client.post(
            "/api/v1/admin/enrollments",
            headers={"X-CSRF-Token": csrf_token},
            json={},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": csrf_token},
        ).status_code
        == 200
    )
    assert client.get("/api/v1/admin/session").status_code == 401

    login = client.post(
        "/api/v1/auth/login",
        json={"username": "ADMIN", "password": PASSWORD, "remember": False},
    )
    assert login.status_code == 200
    session_token = client.cookies.get("obsync_session")
    app.state.service.db.execute(
        "UPDATE admin_sessions SET expires_at = ? WHERE token_hash = ?",
        (
            (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            app.state.service.db.query_one("SELECT token_hash FROM admin_sessions")["token_hash"],
        ),
    )
    assert session_token
    assert client.get("/api/v1/admin/session").status_code == 401


def test_login_rate_limit_and_generic_error(tmp_path: Path) -> None:
    client, _app = make_client(tmp_path)
    setup_account(client)
    client.cookies.clear()
    for _attempt in range(5):
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "definitely wrong", "remember": False},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Username or password is incorrect"
    blocked = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": PASSWORD, "remember": False},
    )
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "900"


def test_environment_credentials_can_bootstrap_setup(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        vault_path=tmp_path / "vault",
        admin_token="",
        admin_username="operator",
        admin_password=PASSWORD,
    )
    client = TestClient(create_app(settings))
    assert client.get("/api/v1/auth/status").json()["setup_required"] is False
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"username": "operator", "password": PASSWORD, "remember": False},
        ).status_code
        == 200
    )


def test_password_policy_and_duplicate_setup(tmp_path: Path) -> None:
    client, _app = make_client(tmp_path)
    weak = client.post(
        "/api/v1/auth/setup",
        json={"username": "admin", "password": "too-short"},
    )
    assert weak.status_code == 400
    setup_account(client)
    duplicate = client.post(
        "/api/v1/auth/setup",
        json={"username": "other", "password": PASSWORD},
    )
    assert duplicate.status_code == 400
