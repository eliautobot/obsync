from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import httpx
from platformdirs import user_data_path

from . import __version__
from .agent import AgentConfig, AgentRuntime, default_config_path, pair_agent
from .desktop import choose_directory

TASK_NAME = "Obsync Desktop"
LEGACY_TASK_NAME = "Obsync Companion"
DESKTOP_FILENAME = "Obsync-Desktop.exe"
COMPANION_FILENAME = DESKTOP_FILENAME


@dataclass(slots=True)
class CompanionInstall:
    executable: Path
    config_path: Path
    server_url: str
    computer_name: str
    vault_path: str


def is_windows() -> bool:
    return os.name == "nt"


def companion_data_dir() -> Path:
    return user_data_path("Obsync") / "desktop"


def installed_companion_path() -> Path:
    return companion_data_dir() / __version__ / COMPANION_FILENAME


def scheduled_task_command(executable: Path, config_path: Path) -> str:
    return subprocess.list2cmdline([str(executable), "--background", "--config", str(config_path)])


def parse_pairing_details(value: str) -> dict[str, str]:
    try:
        payload = json.loads(value.strip())
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(
            "Copy the setup details from Obsync, then try Paste setup details again"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError("The copied setup details are not valid")
    details = {
        "server": str(payload.get("server", "")).strip().rstrip("/"),
        "code": str(payload.get("code", "")).strip().upper(),
        "name": str(payload.get("name", "")).strip(),
    }
    if not details["server"].startswith(("http://", "https://")) or not details["code"]:
        raise ValueError("The copied setup details are incomplete")
    return details


@contextmanager
def setup_instance_lock(path: Path | None = None) -> Iterator[bool]:
    """Allow only one setup window so a one-time code cannot be submitted twice."""
    if not is_windows():
        yield True
        return
    import msvcrt

    lock_path = path or (companion_data_dir() / "setup.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle: BinaryIO = lock_path.open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
        else:
            try:
                yield True
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def _windows_creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def install_startup_task(
    executable: Path,
    config_path: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    if not is_windows():
        raise ValueError("Automatic Obsync Desktop startup is currently available on Windows")
    command = scheduled_task_command(executable, config_path)
    for task_name in (LEGACY_TASK_NAME, TASK_NAME):
        run(
            ["schtasks.exe", "/End", "/TN", task_name],
            check=False,
            capture_output=True,
            text=True,
            creationflags=_windows_creation_flags(),
        )
    result = run(
        [
            "schtasks.exe",
            "/Create",
            "/F",
            "/SC",
            "ONLOGON",
            "/TN",
            TASK_NAME,
            "/TR",
            command,
            "/RL",
            "LIMITED",
        ],
        check=False,
        capture_output=True,
        text=True,
        creationflags=_windows_creation_flags(),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Windows rejected the startup task").strip()
        raise ValueError(f"Could not enable automatic startup: {detail}")
    verification = run(
        ["schtasks.exe", "/Query", "/TN", TASK_NAME],
        check=False,
        capture_output=True,
        text=True,
        creationflags=_windows_creation_flags(),
    )
    if verification.returncode != 0:
        detail = (
            verification.stderr or verification.stdout or "Windows could not find the startup task"
        ).strip()
        raise ValueError(f"Could not verify automatic startup: {detail}")
    run(
        ["schtasks.exe", "/Delete", "/F", "/TN", LEGACY_TASK_NAME],
        check=False,
        capture_output=True,
        text=True,
        creationflags=_windows_creation_flags(),
    )


def start_background_companion(
    executable: Path,
    config_path: Path,
    *,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    if is_windows():
        result = run(
            ["schtasks.exe", "/Run", "/TN", TASK_NAME],
            check=False,
            capture_output=True,
            text=True,
            creationflags=_windows_creation_flags(),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "Windows could not start Obsync").strip()
            raise ValueError(f"Could not start the Obsync background app: {detail}")
        return
    flags = (
        int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        | int(getattr(subprocess, "DETACHED_PROCESS", 0))
        | int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    )
    popen(
        [str(executable), "--background", "--config", str(config_path)],
        cwd=str(executable.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )


def stop_background_companion(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    if not is_windows():
        raise ValueError("Desktop background controls are currently available on Windows")
    result = run(
        ["schtasks.exe", "/End", "/TN", TASK_NAME],
        check=False,
        capture_output=True,
        text=True,
        creationflags=_windows_creation_flags(),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Windows could not stop Obsync").strip()
        raise ValueError(f"Could not stop syncing on this computer: {detail}")


def background_companion_is_running(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    if not is_windows():
        return False
    result = run(
        ["schtasks.exe", "/Query", "/TN", TASK_NAME, "/FO", "CSV", "/NH"],
        check=False,
        capture_output=True,
        text=True,
        creationflags=_windows_creation_flags(),
    )
    if result.returncode != 0:
        return False
    try:
        fields = next(csv.reader([result.stdout.strip()]))
    except (csv.Error, StopIteration):
        return False
    return any(field.strip().casefold() == "running" for field in fields)


async def existing_pairing_is_valid(config: AgentConfig) -> bool:
    if not config.server_url or not config.agent_token:
        return False
    async with httpx.AsyncClient(
        base_url=config.server_url.rstrip("/"),
        headers={"Authorization": f"Bearer {config.agent_token}"},
        verify=config.verify_tls,
        timeout=10,
    ) as client:
        response = await client.get("/api/v1/agent/status")
        if response.status_code == 404:
            # Compatibility with Obsync 0.6 and earlier.
            response = await client.post(
                "/api/v1/agent/heartbeat",
                json={"agent_version": __version__},
            )
    return response.is_success


def _current_standalone_executable() -> Path:
    if not getattr(sys, "frozen", False):
        raise ValueError(
            "Automatic installation requires Obsync Desktop for Windows from GitHub Releases"
        )
    return Path(sys.executable).resolve()


async def install_companion(
    *,
    server_url: str,
    enrollment_code: str,
    computer_name: str,
    vault_path: str = "",
    config_path: Path | None = None,
    source_executable: Path | None = None,
) -> CompanionInstall:
    if not is_windows():
        raise ValueError("The guided Obsync Desktop installer is available on Windows")
    server = server_url.strip().rstrip("/")
    code = enrollment_code.strip()
    name = computer_name.strip() or socket.gethostname()
    if not server.startswith(("http://", "https://")):
        raise ValueError(
            "Enter the complete Obsync server address beginning with http:// or https://"
        )

    target_config = config_path or default_config_path()
    existing = AgentConfig.load(target_config)
    existing_matches = bool(existing.agent_token and existing.server_url.rstrip("/") == server)
    try:
        existing_valid = existing_matches and await existing_pairing_is_valid(existing)
    except httpx.RequestError as exc:
        raise ValueError(
            "Could not reach the Obsync server. Check the address and that both computers are "
            "connected to the same LAN or VPN."
        ) from exc
    if existing_valid:
        config = existing
        config.name = name
    elif code:
        try:
            config = await pair_agent(server_url=server, code=code, name=name)
        except httpx.RequestError as exc:
            raise ValueError(
                "Could not reach the Obsync server. Check the address and that both computers "
                "are connected to the same LAN or VPN."
            ) from exc
    else:
        if existing.agent_token and existing.server_url.rstrip("/") == server:
            raise ValueError(
                "This saved connection is no longer authorized. Create a new pairing code in "
                "Obsync and enter it here."
            )
        raise ValueError("Enter the one-time pairing code shown in Obsync")

    if vault_path:
        config.set_vault(Path(vault_path))
    saved_config = config.save(target_config)

    source = (source_executable or _current_standalone_executable()).resolve()
    destination = installed_companion_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source != destination.resolve():
        shutil.copy2(source, destination)
    install_startup_task(destination, saved_config)
    start_background_companion(destination, saved_config)
    return CompanionInstall(
        executable=destination,
        config_path=saved_config,
        server_url=config.server_url,
        computer_name=config.name,
        vault_path=config.vault_path,
    )


def _background_log_path() -> Path:
    path = companion_data_dir() / "desktop.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def run_background(config_path: Path) -> int:
    logging.basicConfig(
        filename=_background_log_path(),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        config = AgentConfig.load(config_path)
        logging.info("Starting Obsync Desktop %s for %s", __version__, config.name)
        asyncio.run(AgentRuntime(config, config_path=config_path).run_forever())
    except Exception:
        logging.exception("Obsync Desktop stopped unexpectedly")
        return 1
    return 0


def run_setup_gui(
    *,
    server_url: str = "",
    enrollment_code: str = "",
    computer_name: str = "",
    config_path: Path | None = None,
) -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError as exc:  # pragma: no cover - standalone packaging guard
        raise ValueError("The Windows setup window is unavailable in this build") from exc

    target_config = config_path or default_config_path()
    existing = AgentConfig.load(target_config)
    root = tk.Tk()
    root.title(f"Obsync Desktop {__version__}")
    root.geometry("590x760")
    root.minsize(540, 680)

    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Connect this Windows PC", font=("Segoe UI", 18, "bold")).pack(anchor="w")
    ttk.Label(
        frame,
        text=(
            "This is the Windows side of Obsync. Pair once, then folder watching runs quietly "
            "in the background and starts automatically when you sign in."
        ),
        wraplength=500,
    ).pack(anchor="w", pady=(6, 20))

    fields = ttk.Frame(frame)
    fields.pack(fill="x")

    server_value = tk.StringVar(value=server_url or existing.server_url)
    code_value = tk.StringVar(value=enrollment_code)
    name_value = tk.StringVar(value=computer_name or existing.name or socket.gethostname())
    has_vault = tk.BooleanVar(value=bool(existing.vault_path))
    vault_value = tk.StringVar(value=existing.vault_path)
    status_value = tk.StringVar(value="Ready to connect.")

    def paste_setup_details() -> None:
        try:
            details = parse_pairing_details(root.clipboard_get())
            server_value.set(details["server"])
            code_value.set(details["code"])
            name_value.set(details["name"] or name_value.get())
            status_value.set("Setup details pasted. Review them, then connect.")
        except (tk.TclError, ValueError) as exc:
            messagebox.showerror("Could not paste setup details", str(exc), parent=root)

    ttk.Button(frame, text="Paste setup details", command=paste_setup_details).pack(
        anchor="w", pady=(0, 14)
    )

    def add_field(
        label: str,
        variable: tk.StringVar,
        help_text: str,
        *,
        secret: bool = False,
    ) -> None:
        ttk.Label(fields, text=label, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Entry(fields, textvariable=variable, show="*" if secret else "").pack(
            fill="x", pady=(4, 2)
        )
        ttk.Label(fields, text=help_text, wraplength=500).pack(anchor="w", pady=(0, 12))

    add_field(
        "Obsync server address",
        server_value,
        "Copy the server address shown in Sources → Add another computer.",
    )
    add_field(
        "One-time pairing code",
        code_value,
        "Required for the first connection. Leave it blank when repairing an existing connection.",
    )
    add_field(
        "Computer name",
        name_value,
        "The friendly name that will appear on the Sources page.",
    )

    vault_row = ttk.Frame(fields)
    vault_row.pack(fill="x", pady=(2, 6))
    ttk.Checkbutton(
        vault_row,
        text="This computer contains my Obsidian vault",
        variable=has_vault,
    ).pack(side="left")

    vault_display = ttk.Label(fields, textvariable=vault_value, wraplength=400)
    vault_display.pack(anchor="w", pady=(0, 4))

    def choose_vault() -> None:
        try:
            selected = choose_directory("Choose your Obsidian vault", vault_value.get())
            vault_value.set(str(selected))
            has_vault.set(True)
        except ValueError:
            return

    ttk.Button(fields, text="Choose vault folder…", command=choose_vault).pack(anchor="w")
    ttk.Label(
        fields,
        text="Optional. You can also select the vault later from Obsync Settings.",
    ).pack(anchor="w", pady=(3, 14))

    status = ttk.Label(frame, textvariable=status_value, wraplength=500)
    status.pack(anchor="w", pady=(4, 12))
    connect_button = ttk.Button(frame, text="Connect and install")
    connect_button.pack(fill="x", ipady=7)

    desktop_controls = ttk.LabelFrame(frame, text="This computer", padding=12)
    desktop_controls.pack(fill="x", pady=(16, 0))
    sync_status_value = tk.StringVar(value="Checking background status…")
    ttk.Label(desktop_controls, textvariable=sync_status_value).pack(anchor="w")
    ttk.Label(
        desktop_controls,
        text=(
            "Stopping here stops this PC's watcher. To cancel server-side AI work too, use "
            "Stop syncing in the Obsync dashboard."
        ),
        wraplength=500,
    ).pack(anchor="w", pady=(3, 10))
    control_row = ttk.Frame(desktop_controls)
    control_row.pack(fill="x")

    def refresh_background_status() -> None:
        if not AgentConfig.load(target_config).agent_token:
            sync_status_value.set("Not connected yet")
            start_button.configure(state="disabled")
            stop_button.configure(state="disabled")
            open_button.configure(state="disabled")
            return
        running = background_companion_is_running()
        sync_status_value.set(
            "Folder watching is running" if running else "Folder watching is stopped"
        )
        start_button.configure(state="disabled" if running else "normal")
        stop_button.configure(state="normal" if running else "disabled")
        open_button.configure(state="normal")

    def start_syncing() -> None:
        try:
            executable = installed_companion_path()
            if not executable.is_file():
                raise ValueError("Connect and install Obsync Desktop before starting it")
            install_startup_task(executable, target_config)
            start_background_companion(executable, target_config)
            sync_status_value.set("Folder watching is running")
        except (OSError, ValueError) as exc:
            messagebox.showerror("Could not start Obsync", str(exc), parent=root)
        refresh_background_status()

    def stop_syncing() -> None:
        try:
            stop_background_companion()
            sync_status_value.set("Folder watching is stopped")
        except (OSError, ValueError) as exc:
            messagebox.showerror("Could not stop Obsync", str(exc), parent=root)
        refresh_background_status()

    def open_obsync() -> None:
        server = AgentConfig.load(target_config).server_url
        if server:
            webbrowser.open(server)

    start_button = ttk.Button(control_row, text="Start this PC", command=start_syncing)
    start_button.pack(side="left")
    stop_button = ttk.Button(control_row, text="Stop this PC", command=stop_syncing)
    stop_button.pack(side="left", padx=(8, 0))
    open_button = ttk.Button(control_row, text="Open Obsync", command=open_obsync)
    open_button.pack(side="right")

    def finish_success(result: CompanionInstall) -> None:
        status_value.set(
            f"Connected as {result.computer_name}. Obsync Desktop is running in the background and "
            "will start automatically at Windows sign-in."
        )
        connect_button.configure(state="normal", text="Installed")
        refresh_background_status()
        messagebox.showinfo(
            "Obsync Desktop is ready",
            "This PC is connected. You may close this window; folder watching stays in the "
            "background.",
            parent=root,
        )

    def finish_error(message: str) -> None:
        status_value.set(message)
        connect_button.configure(state="normal", text="Connect and install")
        messagebox.showerror("Could not connect", message, parent=root)

    def connect() -> None:
        selected_vault = vault_value.get().strip() if has_vault.get() else ""
        if has_vault.get() and not selected_vault:
            choose_vault()
            selected_vault = vault_value.get().strip()
            if not selected_vault:
                return
        connect_button.configure(state="disabled", text="Connecting…")
        status_value.set("Pairing this PC and enabling automatic startup…")
        server_text = server_value.get()
        code_text = code_value.get()
        name_text = name_value.get()

        def worker() -> None:
            try:
                result = asyncio.run(
                    install_companion(
                        server_url=server_text,
                        enrollment_code=code_text,
                        computer_name=name_text,
                        vault_path=selected_vault,
                        config_path=target_config,
                    )
                )
            except Exception as exc:  # GUI boundary reports a concise user-facing error
                root.after(0, finish_error, str(exc))
            else:
                root.after(0, finish_success, result)

        threading.Thread(target=worker, daemon=True).start()

    connect_button.configure(command=connect)
    ttk.Label(
        frame,
        text=(
            "Installed for your Windows account in Local AppData. This window is only needed "
            "when you want to change or repair the desktop connection."
        ),
        wraplength=500,
    ).pack(anchor="w", pady=(12, 0))
    refresh_background_status()
    root.mainloop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install or run Obsync Desktop for Windows")
    parser.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config", default="", help=argparse.SUPPRESS)
    parser.add_argument("--server", default="", help="Prefill the Obsync server address")
    parser.add_argument("--code", default="", help="Prefill the one-time pairing code")
    parser.add_argument("--name", default="", help="Prefill the computer name")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    if args.background:
        raise SystemExit(run_background(config_path))
    with setup_instance_lock() as acquired:
        if not acquired:
            try:
                from tkinter import messagebox

                messagebox.showinfo(
                    "Obsync Desktop is already open",
                    "Use the Obsync Desktop window that is already open. "
                    "Only one window can pair at a time.",
                )
            except Exception:
                pass
            raise SystemExit(0)
        try:
            code = run_setup_gui(
                server_url=args.server,
                enrollment_code=args.code,
                computer_name=args.name,
                config_path=config_path,
            )
        except (ValueError, OSError) as exc:
            try:
                from tkinter import messagebox

                messagebox.showerror("Obsync Desktop", str(exc))
            except Exception:
                pass
            code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
