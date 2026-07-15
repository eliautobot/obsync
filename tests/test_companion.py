from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from obsync.agent import AgentConfig
from obsync.companion import (
    TASK_NAME,
    background_companion_is_running,
    install_companion,
    install_startup_task,
    parse_pairing_details,
    register_url_protocol,
    scheduled_task_command,
    start_background_companion,
    stop_background_companion,
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
    assert calls[0][0][:2] == ["schtasks.exe", "/End"]
    assert calls[1][0][:2] == ["schtasks.exe", "/End"]
    args, kwargs = calls[2]
    assert args[:2] == ["schtasks.exe", "/Create"]
    assert args[args.index("/TN") + 1] == TASK_NAME
    assert args[args.index("/SC") + 1] == "ONLOGON"
    assert args[args.index("/RL") + 1] == "LIMITED"
    assert "--background" in args[args.index("/TR") + 1]
    assert kwargs["check"] is False
    assert calls[3][0][:2] == ["schtasks.exe", "/Query"]
    assert calls[4][0][:2] == ["schtasks.exe", "/Delete"]


def test_background_desktop_status_and_stop(monkeypatch) -> None:
    monkeypatch.setattr("obsync.companion.is_windows", lambda: True)
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        if "/Query" in args:
            return subprocess.CompletedProcess(args, 0, '"Obsync Desktop","N/A","Running"', "")
        return subprocess.CompletedProcess(args, 0, "SUCCESS", "")

    assert background_companion_is_running(run=fake_run) is True
    stop_background_companion(run=fake_run)
    assert calls[0][:2] == ["schtasks.exe", "/Query"]
    assert calls[1][:2] == ["schtasks.exe", "/End"]


def test_startup_task_reports_windows_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("obsync.companion.is_windows", lambda: True)

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(args, 1, "", "access denied")

    with pytest.raises(ValueError, match="access denied"):
        install_startup_task(tmp_path / "agent.exe", tmp_path / "agent.yml", run=fake_run)


def test_background_launch_is_hidden_and_detached(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("obsync.companion.is_windows", lambda: False)
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


def test_windows_background_starts_through_scheduled_task(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("obsync.companion.is_windows", lambda: True)
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "SUCCESS", "")

    start_background_companion(tmp_path / "agent.exe", tmp_path / "agent.yml", run=fake_run)
    assert calls[0][0] == ["schtasks.exe", "/Run", "/TN", TASK_NAME]


def test_pairing_details_round_trip() -> None:
    details = parse_pairing_details(
        '{"server":"http://server:7769/","code":"abcd-efgh-jkmn","name":"Office PC"}'
    )
    assert details == {
        "server": "http://server:7769",
        "code": "ABCD-EFGH-JKMN",
        "name": "Office PC",
    }
    with pytest.raises(ValueError, match="Copy the setup details"):
        parse_pairing_details("not json")


def test_windows_app_link_is_registered_for_current_user(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("obsync.companion.is_windows", lambda: True)
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "SUCCESS", "")

    executable = tmp_path / "Obsync Desktop.exe"
    register_url_protocol(executable, run=fake_run)
    assert len(calls) == 3
    assert all(call[:3] == ["reg.exe", "ADD", call[2]] for call in calls)
    assert calls[0][2] == r"HKCU\Software\Classes\obsync"
    assert calls[1][calls[1].index("/V") + 1] == "URL Protocol"
    assert str(executable) in calls[2][calls[2].index("/D") + 1]
    assert "%1" in calls[2][calls[2].index("/D") + 1]


@pytest.mark.asyncio
async def test_companion_install_requires_administrator(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("obsync.companion.is_windows", lambda: True)
    monkeypatch.setattr("obsync.companion.windows_is_admin", lambda: False)
    with pytest.raises(ValueError, match="Run as administrator"):
        await install_companion(
            server_url="http://server:7769",
            enrollment_code="AAAA-BBBB-CCCC",
            computer_name="Office PC",
            source_executable=tmp_path / "desktop.exe",
        )


@pytest.mark.asyncio
async def test_companion_pairs_copies_installs_and_starts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("obsync.companion.is_windows", lambda: True)
    monkeypatch.setattr("obsync.companion.windows_is_admin", lambda: True)
    source = tmp_path / "download" / "companion.exe"
    source.parent.mkdir()
    source.write_bytes(b"standalone companion")
    destination = tmp_path / "installed" / "Obsync Companion.exe"
    config_path = tmp_path / "config" / "agent.yml"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".obsidian").mkdir()
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
    monkeypatch.setattr("obsync.companion.register_url_protocol", lambda executable: None)
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


@pytest.mark.asyncio
async def test_companion_reuses_valid_pairing_and_repairs_startup(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("obsync.companion.is_windows", lambda: True)
    monkeypatch.setattr("obsync.companion.windows_is_admin", lambda: True)
    source = tmp_path / "download" / "companion.exe"
    source.parent.mkdir()
    source.write_bytes(b"companion")
    destination = tmp_path / "installed" / "Obsync Companion.exe"
    config_path = tmp_path / "config" / "agent.yml"
    AgentConfig(
        server_url="http://server:7769",
        agent_id="existing-agent",
        agent_token="agent_existing_token_value_long_enough",
        name="Old name",
    ).save(config_path)
    lifecycle = []

    async def valid(_config):
        return True

    async def should_not_pair(**_kwargs):
        raise AssertionError("A valid saved pairing must not consume the one-time code again")

    monkeypatch.setattr("obsync.companion.existing_pairing_is_valid", valid)
    monkeypatch.setattr("obsync.companion.pair_agent", should_not_pair)
    monkeypatch.setattr("obsync.companion.installed_companion_path", lambda: destination)
    monkeypatch.setattr("obsync.companion.register_url_protocol", lambda executable: None)
    monkeypatch.setattr(
        "obsync.companion.install_startup_task",
        lambda executable, config: lifecycle.append(("task", executable, config)),
    )
    monkeypatch.setattr(
        "obsync.companion.start_background_companion",
        lambda executable, config: lifecycle.append(("start", executable, config)),
    )

    result = await install_companion(
        server_url="http://server:7769",
        enrollment_code="ALREADY-USED-CODE",
        computer_name="Main PC",
        config_path=config_path,
        source_executable=source,
    )
    assert result.computer_name == "Main PC"
    assert AgentConfig.load(config_path).agent_id == "existing-agent"
    assert [item[0] for item in lifecycle] == ["task", "start"]
