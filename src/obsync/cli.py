from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from contextlib import suppress
from pathlib import Path

import uvicorn

from . import __version__
from .agent import AgentConfig, AgentRuntime, default_config_path, pair_agent
from .config import Settings
from .desktop import choose_directory
from .service import ObsyncService


def _load_legacy_admin_token(settings: Settings) -> str:
    if settings.admin_token:
        return settings.admin_token
    token_file = settings.data_dir / "admin-token.txt"
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            settings.admin_token = token
            return token
    return ""


def _server(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    settings.prepare()
    legacy_token = _load_legacy_admin_token(settings)
    if legacy_token:
        os.environ["OBSYNC_ADMIN_TOKEN"] = legacy_token
    os.environ["OBSYNC_DATA_DIR"] = str(settings.data_dir)
    os.environ["OBSYNC_VAULT_PATH"] = str(settings.vault_path)
    uvicorn.run(
        "obsync.api:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("OBSYNC_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )
    return 0


def _admin_reset(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    settings.prepare()
    _load_legacy_admin_token(settings)
    password = getpass.getpass("New password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise ValueError("Passwords do not match")
    account = ObsyncService(settings).reset_admin_account(args.username, password)
    print(f"Admin credentials updated for {account['username']}. All sessions were signed out.")
    return 0


def _agent_pair(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    if config_path.exists() and AgentConfig.load(config_path).agent_token and not args.force:
        raise ValueError(f"Agent is already paired in {config_path}; use --force to replace it")
    config = asyncio.run(
        pair_agent(
            server_url=args.server,
            code=args.code,
            name=args.name,
            verify_tls=not args.insecure,
        )
    )
    path = config.save(config_path)
    print(f"Paired {config.name} with {config.server_url}")
    print(f"Configuration saved to {path}")
    return 0


def _agent_add(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    config = AgentConfig.load(config_path)
    selected = (
        choose_directory("Choose a folder for Obsync to watch")
        if getattr(args, "browse", False) or not args.path
        else Path(args.path)
    )
    root = config.add_root(
        selected,
        name=args.name,
        destination=args.destination,
    )
    config.save(config_path)
    print(f"Watching {root.path} as '{root.name}'")
    return 0


def _agent_set_vault(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    config = AgentConfig.load(config_path)
    selected = (
        choose_directory("Choose your Obsidian vault", config.vault_path)
        if args.browse or not args.path
        else Path(args.path)
    )
    vault = config.set_vault(selected)
    config.save(config_path)
    print(f"Obsidian vault set to {vault}")
    return 0


def _agent_list(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    config = AgentConfig.load(config_path)
    print(f"Server: {config.server_url or '(not paired)'}")
    print(f"Device: {config.name or '(unnamed)'}")
    print(f"Obsidian vault: {config.vault_path or '(not selected)'}")
    if not config.roots:
        print("Watched folders: none")
    else:
        print("Watched folders:")
        for root in config.roots:
            state = "enabled" if root.enabled else "paused"
            print(f"  - {root.name}: {root.path} -> {root.destination} ({state})")
    return 0


def _load_runtime(args: argparse.Namespace) -> AgentRuntime:
    config_path = Path(args.config).expanduser() if args.config else default_config_path()
    return AgentRuntime(AgentConfig.load(config_path), config_path=config_path)


def _agent_scan(args: argparse.Namespace) -> int:
    result = asyncio.run(_load_runtime(args).scan_all())
    for name, stats in result.items():
        print(
            f"{name}: {stats['synced']} synced, {stats['unchanged']} unchanged, "
            f"{stats['errors']} errors, {stats['files']} files"
        )
    return 1 if any(stats["errors"] for stats in result.values()) else 0


def _agent_run(args: argparse.Namespace) -> int:
    with suppress(KeyboardInterrupt):
        asyncio.run(_load_runtime(args).run_forever())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsync",
        description="Turn folders from any computer into organized Obsidian Markdown.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    server = commands.add_parser("server", help="Run the central Obsync server")
    server.add_argument("--host", default="", help="Bind host (default: OBSYNC_HOST or 0.0.0.0)")
    server.add_argument("--port", type=int, default=0, help="Bind port (default: 7769)")
    server.set_defaults(handler=_server)

    admin = commands.add_parser("admin", help="Manage the Obsync administrator account")
    admin_commands = admin.add_subparsers(dest="admin_command", required=True)
    reset_password = admin_commands.add_parser(
        "reset-password", help="Create or reset the administrator login"
    )
    reset_password.add_argument("--username", default="admin", help="Administrator username")
    reset_password.set_defaults(handler=_admin_reset)

    agent = commands.add_parser("agent", help="Manage the folder-watching agent")
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)

    pair = agent_commands.add_parser("pair", help="Pair this computer to an Obsync server")
    pair.add_argument("--server", required=True, help="Central server URL")
    pair.add_argument("--code", required=True, help="One-time enrollment code")
    pair.add_argument("--name", default="", help="Friendly device name")
    pair.add_argument("--config", default="", help="Agent configuration path")
    pair.add_argument(
        "--insecure", action="store_true", help="Skip TLS verification (testing only)"
    )
    pair.add_argument("--force", action="store_true", help="Replace an existing pairing")
    pair.set_defaults(handler=_agent_pair)

    add = agent_commands.add_parser("add-folder", help="Add a local or network folder")
    add.add_argument("path", nargs="?", default="", help="Folder to watch")
    add.add_argument("--browse", action="store_true", help="Choose the folder in a window")
    add.add_argument("--name", default="", help="Friendly folder name")
    add.add_argument("--destination", default="Obsync", help="Vault destination prefix")
    add.add_argument("--config", default="", help="Agent configuration path")
    add.set_defaults(handler=_agent_add)

    vault = agent_commands.add_parser(
        "set-vault", help="Make this computer the Obsidian vault writer"
    )
    vault.add_argument("path", nargs="?", default="", help="Obsidian vault folder")
    vault.add_argument("--browse", action="store_true", help="Choose the vault in a window")
    vault.add_argument("--config", default="", help="Agent configuration path")
    vault.set_defaults(handler=_agent_set_vault)

    listing = agent_commands.add_parser("list", help="Show pairing and watched folders")
    listing.add_argument("--config", default="", help="Agent configuration path")
    listing.set_defaults(handler=_agent_list)

    scan = agent_commands.add_parser("scan", help="Run one complete reconciliation")
    scan.add_argument("--config", default="", help="Agent configuration path")
    scan.set_defaults(handler=_agent_scan)

    run = agent_commands.add_parser("run", help="Watch continuously")
    run.add_argument("--config", default="", help="Agent configuration path")
    run.set_defaults(handler=_agent_run)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        code = args.handler(args)
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
