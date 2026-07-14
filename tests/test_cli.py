from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from obsync.agent import AgentConfig
from obsync.cli import (
    _admin_reset,
    _agent_add,
    _agent_list,
    _load_legacy_admin_token,
    build_parser,
)
from obsync.config import Settings


def test_legacy_admin_token_is_loaded_but_not_generated(tmp_path: Path) -> None:
    settings = Settings(tmp_path / "data", tmp_path / "vault", "")
    settings.prepare()
    assert _load_legacy_admin_token(settings) == ""
    token_file = settings.data_dir / "admin-token.txt"
    token_file.write_text("old-token\n", encoding="utf-8")
    assert _load_legacy_admin_token(settings) == "old-token"


def test_existing_environment_admin_token_is_kept(tmp_path: Path) -> None:
    settings = Settings(tmp_path / "data", tmp_path / "vault", "provided")
    assert _load_legacy_admin_token(settings) == "provided"


def test_settings_loads_pre_upgrade_token_file(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "admin-token.txt").write_text("old-generated-token\n", encoding="utf-8")
    monkeypatch.setenv("OBSYNC_DATA_DIR", str(data_dir))
    monkeypatch.delenv("OBSYNC_ADMIN_TOKEN", raising=False)
    assert Settings.from_env().admin_token == "old-generated-token"


def test_cli_parser_has_server_and_agent_commands() -> None:
    parser = build_parser()
    server = parser.parse_args(["server", "--port", "9000"])
    assert server.port == 9000
    pair = parser.parse_args(
        ["agent", "pair", "--server", "http://localhost", "--code", "AAAA-BBBB-CCCC"]
    )
    assert pair.server == "http://localhost"
    assert pair.agent_command == "pair"
    reset = parser.parse_args(["admin", "reset-password", "--username", "owner"])
    assert reset.username == "owner"


def test_admin_reset_command_creates_account(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("OBSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OBSYNC_VAULT_PATH", str(tmp_path / "vault"))
    answers = iter(["new secure password", "new secure password"])
    monkeypatch.setattr("obsync.cli.getpass.getpass", lambda _prompt: next(answers))
    assert _admin_reset(argparse.Namespace(username="owner")) == 0
    assert "credentials updated for owner" in capsys.readouterr().out


def test_agent_add_and_list_commands(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source"
    source.mkdir()
    config_path = tmp_path / "agent.yml"
    AgentConfig(server_url="http://server", name="PC").save(config_path)
    add_args = argparse.Namespace(
        config=str(config_path),
        path=str(source),
        name="Work",
        destination="Knowledge",
    )
    assert _agent_add(add_args) == 0
    assert "Watching" in capsys.readouterr().out
    assert _agent_list(argparse.Namespace(config=str(config_path))) == 0
    output = capsys.readouterr().out
    assert "Server: http://server" in output
    assert "Work:" in output


def test_agent_add_rejects_missing_folder(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yml"
    AgentConfig().save(config_path)
    with pytest.raises(ValueError, match="does not exist"):
        _agent_add(
            argparse.Namespace(
                config=str(config_path),
                path=str(tmp_path / "missing"),
                name="",
                destination="Obsync",
            )
        )
