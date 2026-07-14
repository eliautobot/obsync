from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from obsync.agent import AgentConfig
from obsync.cli import (
    _agent_add,
    _agent_list,
    _ensure_admin_token,
    build_parser,
)
from obsync.config import Settings


def test_admin_token_is_generated_then_reused(tmp_path: Path) -> None:
    settings = Settings(tmp_path / "data", tmp_path / "vault", "")
    settings.prepare()
    token, created = _ensure_admin_token(settings)
    assert created is True
    assert token.startswith("admin_")
    second_settings = Settings(settings.data_dir, settings.vault_path, "")
    second, second_created = _ensure_admin_token(second_settings)
    assert second == token
    assert second_created is False


def test_existing_environment_admin_token_is_kept(tmp_path: Path) -> None:
    settings = Settings(tmp_path / "data", tmp_path / "vault", "provided")
    assert _ensure_admin_token(settings) == ("provided", False)


def test_cli_parser_has_server_and_agent_commands() -> None:
    parser = build_parser()
    server = parser.parse_args(["server", "--port", "9000"])
    assert server.port == 9000
    pair = parser.parse_args(
        ["agent", "pair", "--server", "http://localhost", "--code", "AAAA-BBBB-CCCC"]
    )
    assert pair.server == "http://localhost"
    assert pair.agent_command == "pair"


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
