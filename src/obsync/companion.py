from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path

from . import __version__
from .agent import AgentConfig, AgentRuntime, default_config_path, pair_agent
from .desktop import choose_directory

TASK_NAME = "Obsync Companion"
COMPANION_FILENAME = "Obsync-Companion.exe"


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
    return user_data_path("Obsync") / "companion"


def installed_companion_path() -> Path:
    return companion_data_dir() / __version__ / COMPANION_FILENAME


def scheduled_task_command(executable: Path, config_path: Path) -> str:
    return subprocess.list2cmdline([str(executable), "--background", "--config", str(config_path)])


def _windows_creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def install_startup_task(
    executable: Path,
    config_path: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    if not is_windows():
        raise ValueError("Automatic companion startup is currently available on Windows")
    command = scheduled_task_command(executable, config_path)
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


def start_background_companion(
    executable: Path,
    config_path: Path,
    *,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> None:
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


def _current_standalone_executable() -> Path:
    if not getattr(sys, "frozen", False):
        raise ValueError(
            "Automatic installation requires the standalone Windows Companion from GitHub Releases"
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
        raise ValueError("The guided companion installer is available on Windows")
    server = server_url.strip().rstrip("/")
    code = enrollment_code.strip()
    name = computer_name.strip() or socket.gethostname()
    if not server.startswith(("http://", "https://")):
        raise ValueError(
            "Enter the complete Obsync server address beginning with http:// or https://"
        )

    target_config = config_path or default_config_path()
    if code:
        config = await pair_agent(server_url=server, code=code, name=name)
    else:
        config = AgentConfig.load(target_config)
        if not config.agent_token:
            raise ValueError("Enter the one-time pairing code shown in Obsync")
        if config.server_url.rstrip("/") != server:
            raise ValueError("A new server requires a new pairing code")
        config.name = name

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
    path = companion_data_dir() / "companion.log"
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
        logging.info("Starting Obsync Companion %s for %s", __version__, config.name)
        asyncio.run(AgentRuntime(config, config_path=config_path).run_forever())
    except Exception:
        logging.exception("Obsync Companion stopped unexpectedly")
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
    root.title(f"Obsync Companion {__version__}")
    root.geometry("560x570")
    root.minsize(520, 520)

    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Connect this Windows PC", font=("Segoe UI", 18, "bold")).pack(anchor="w")
    ttk.Label(
        frame,
        text=(
            "Pair once, then Obsync runs quietly in the background and starts automatically "
            "when you sign in. No PowerShell window or Administrator access is required."
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
        "Copy the 12-character code shown in Obsync. It expires after 20 minutes.",
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

    def finish_success(result: CompanionInstall) -> None:
        status_value.set(
            f"Connected as {result.computer_name}. Obsync is running in the background and "
            "will start automatically at Windows sign-in."
        )
        connect_button.configure(state="normal", text="Installed")
        messagebox.showinfo(
            "Obsync Companion is ready",
            "This PC is connected. You may close this window and return to Obsync in your browser.",
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
            "Installed per-user in Local AppData. Remove it later from Windows Task Scheduler "
            "if needed."
        ),
        wraplength=500,
    ).pack(anchor="w", pady=(12, 0))
    root.mainloop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install or run the Obsync Windows Companion")
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

            messagebox.showerror("Obsync Companion", str(exc))
        except Exception:
            pass
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
