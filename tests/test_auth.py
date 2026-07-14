from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from obsync.api import create_app
from obsync.config import Settings

PASSWORD = "correct horse battery staple"


def make_client(
    tmp_path: Path,
    *,
    legacy_token: str = "",
    trusted_local: bool = True,
) -> tuple[TestClient, object]:
    settings = Settings(
        data_dir=tmp_path / "data",
        vault_path=tmp_path / "vault",
        admin_token=legacy_token,
    )
    app = create_app(settings)
    client_ip = "127.0.0.1" if trusted_local else "203.0.113.10"
    base_url = "http://localhost:7769" if trusted_local else "http://obsync.example:7769"
    return TestClient(app, client=(client_ip, 50000), base_url=base_url), app


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
        "account_registered": False,
        "temporary_admin_available": True,
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


def test_legacy_install_must_be_secured_locally_then_token_is_disabled(tmp_path: Path) -> None:
    remote, _app = make_client(
        tmp_path,
        legacy_token="old-admin-token",
        trusted_local=False,
    )
    status = remote.get("/api/v1/auth/status").json()
    assert status["setup_required"] is True
    assert status["legacy_migration_required"] is False
    assert (
        remote.get(
            "/api/v1/admin/overview",
            headers={"Authorization": "Bearer old-admin-token"},
        ).status_code
        == 200
    )
    blocked = remote.post(
        "/api/v1/auth/setup",
        json={
            "username": "admin",
            "password": PASSWORD,
            "legacy_token": "old-admin-token",
        },
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == (
        "Create the administrator account from the Obsync server itself"
    )

    local_settings = Settings(
        data_dir=tmp_path / "data",
        vault_path=tmp_path / "vault",
        admin_token="old-admin-token",
    )
    local = TestClient(
        create_app(local_settings),
        client=("127.0.0.1", 50000),
        base_url="http://localhost:7769",
    )
    setup_account(local)
    local.cookies.clear()
    assert (
        local.get(
            "/api/v1/admin/overview",
            headers={"Authorization": "Bearer old-admin-token"},
        ).status_code
        == 401
    )


def test_temporary_admin_is_local_only_and_passwordless(tmp_path: Path) -> None:
    local, _app = make_client(tmp_path)
    status = local.get("/api/v1/auth/status").json()
    assert status["temporary_admin_available"] is True
    session = local.get("/api/v1/admin/session")
    assert session.status_code == 200
    assert session.json() == {
        "authenticated": True,
        "username": "Admin",
        "legacy": False,
        "temporary": True,
        "account_registered": False,
    }
    login = local.post(
        "/api/v1/auth/login",
        json={"username": "Admin", "password": "", "remember": False},
    )
    assert login.status_code == 200
    assert login.json()["temporary"] is True
    assert not local.cookies.get("obsync_session")

    remote, _remote_app = make_client(tmp_path / "remote", trusted_local=False)
    remote_status = remote.get("/api/v1/auth/status").json()
    assert remote_status["temporary_admin_available"] is False
    assert remote.get("/api/v1/admin/session").status_code == 401
    assert (
        remote.post(
            "/api/v1/auth/login",
            json={"username": "Admin", "password": "", "remember": False},
        ).status_code
        == 409
    )
    assert (
        remote.post(
            "/api/v1/auth/setup",
            json={"username": "owner", "password": PASSWORD},
        ).status_code
        == 403
    )


def test_temporary_admin_rejects_cross_site_writes_and_host_spoofing(tmp_path: Path) -> None:
    local, _app = make_client(tmp_path)
    hostile_headers = {
        "Origin": "https://evil.example",
        "Sec-Fetch-Site": "cross-site",
    }
    assert (
        local.post("/api/v1/admin/enrollments", headers=hostile_headers, json={}).status_code == 403
    )
    assert (
        local.post(
            "/api/v1/auth/setup",
            headers=hostile_headers,
            json={"username": "owner", "password": PASSWORD},
        ).status_code
        == 403
    )

    remote, _remote_app = make_client(tmp_path / "remote", trusted_local=False)
    assert (
        remote.get(
            "/api/v1/admin/session",
            headers={"Host": "203.0.113.10:7769"},
        ).status_code
        == 401
    )

    proxied_settings = Settings(
        data_dir=tmp_path / "proxied-data",
        vault_path=tmp_path / "proxied-vault",
        admin_token="",
    )
    proxied = TestClient(
        create_app(proxied_settings),
        client=("127.0.0.1", 50000),
        base_url="https://obsync.example",
    )
    assert proxied.get("/api/v1/auth/status").json()["temporary_admin_available"] is False
    assert proxied.get("/api/v1/admin/session").status_code == 401


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


def test_account_settings_change_username_and_password(tmp_path: Path) -> None:
    client, _app = make_client(tmp_path)
    csrf = setup_account(client)
    wrong = client.put(
        "/api/v1/admin/account",
        headers={"X-CSRF-Token": csrf},
        json={
            "username": "owner",
            "current_password": "wrong password",
            "new_password": "a new secure password",
        },
    )
    assert wrong.status_code == 401

    updated = client.put(
        "/api/v1/admin/account",
        headers={"X-CSRF-Token": csrf},
        json={
            "username": "owner",
            "current_password": PASSWORD,
            "new_password": "a new secure password",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["username"] == "owner"
    assert client.get("/api/v1/admin/session").json()["username"] == "owner"

    client.cookies.clear()
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": PASSWORD, "remember": False},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/login",
            json={
                "username": "owner",
                "password": "a new secure password",
                "remember": False,
            },
        ).status_code
        == 200
    )
