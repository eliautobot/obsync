from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from obsync.agent import AgentConfig
from obsync.companion import (
    TASK_NAME,
    install_companion,
    install_startup_task,
    scheduled_task_command,
    start_background_companion,
)


def test_scheduled_task_command_quotes_paths() -> None:
    command = scheduled_task_command(
        Path(r"C:\Users\Eli\App Data\Obsync Companion.exe"),
        Path(r"C:\Users\Eli\App Data\agent.yml"),
    )
    assert '"C:\\Users\\Eli\\App Data\\Obsync Companion.exe"' in command
    assert "--background" in command
    assert '"C:\\Users\\Eli\\App Data\\agent.yml"' in command


def test_startup_task_is_per_user_and_non_elevated(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("obsync.companion.is_windows", lambda: True)
    calls: list[tuple[list[str], dict]] = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "SUCCESS", "")

    executable = tmp_path / "Obsync Companion.exe"
    config = tmp_path / "agent.yml"
    install_startup_task(executable, config, run=fake_run)
    args, kwargs = calls[0]
    assert args[:2] == ["schtasks.exe", "/Create"]
    assert args[args.index("/TN") + 1] == TASK_NAME
    assert args[args.index("/SC") + 1] == "ONLOGON"
    assert args[args.index("/RL") + 1] == "LIMITED"
    assert "--background" in args[args.index("/TR") + 1]
    assert kwargs["check"] is False


def test_startup_task_reports_windows_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("obsync.companion.is_windows", lambda: True)

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(args, 1, "", "access denied")

    with pytest.raises(ValueError, match="access denied"):
        install_startup_task(tmp_path / "agent.exe", tmp_path / "agent.yml", run=fake_run)


def test_background_launch_is_hidden_and_detached(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return object()

    executable = tmp_path / "Obsync Companion.exe"
    config = tmp_path / "agent.yml"
    start_background_companion(executable, config, popen=fake_popen)
    args, kwargs = calls[0]
    assert args == [str(executable), "--background", "--config", str(config)]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL


@pytest.mark.asyncio
async def test_companion_pairs_copies_installs_and_starts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("obsync.companion.is_windows", lambda: True)
    source = tmp_path / "download" / "companion.exe"
    source.parent.mkdir()
    source.write_bytes(b"standalone companion")
    destination = tmp_path / "installed" / "Obsync Companion.exe"
    config_path = tmp_path / "config" / "agent.yml"
    vault = tmp_path / "vault"
    vault.mkdir()
    lifecycle: list[tuple[str, Path, Path]] = []

    async def fake_pair_agent(**kwargs):
        assert kwargs["server_url"] == "http://server:7769"
        assert kwargs["code"] == "AAAA-BBBB-CCCC"
        return AgentConfig(
            server_url=kwargs["server_url"],
            agent_id="agent-id",
            agent_token="agent-token",
            name=kwargs["name"],
        )

    monkeypatch.setattr("obsync.companion.pair_agent", fake_pair_agent)
    monkeypatch.setattr("obsync.companion.installed_companion_path", lambda: destination)
    monkeypatch.setattr(
        "obsync.companion.install_startup_task",
        lambda executable, config: lifecycle.append(("task", executable, config)),
    )
    monkeypatch.setattr(
        "obsync.companion.start_background_companion",
        lambda executable, config: lifecycle.append(("start", executable, config)),
    )

    result = await install_companion(
        server_url="http://server:7769/",
        enrollment_code="AAAA-BBBB-CCCC",
        computer_name="Office PC",
        vault_path=str(vault),
        config_path=config_path,
        source_executable=source,
    )

    assert destination.read_bytes() == b"standalone companion"
    saved = AgentConfig.load(config_path)
    assert saved.server_url == "http://server:7769"
    assert saved.name == "Office PC"
    assert saved.vault_path == str(vault.resolve())
    assert lifecycle == [
        ("task", destination, config_path),
        ("start", destination, config_path),
    ]
    assert result.executable == destination
