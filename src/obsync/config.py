from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_list(name: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, "").split(",") if value.strip())


@dataclass(slots=True)
class Settings:
    data_dir: Path
    vault_path: Path
    admin_token: str
    admin_username: str = ""
    admin_password: str = ""
    host: str = "0.0.0.0"
    port: int = 7769
    max_upload_mb: int = 100
    max_extract_chars: int = 200_000
    allow_registration: bool = True
    secure_cookies: bool = False
    session_hours: int = 12
    remembered_session_days: int = 30
    local_setup_ips: tuple[str, ...] = ()
    log_level: str = "info"

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = Path(os.getenv("OBSYNC_DATA_DIR", "./data")).expanduser().resolve()
        vault_path = Path(os.getenv("OBSYNC_VAULT_PATH", "./vault")).expanduser().resolve()
        admin_token = os.getenv("OBSYNC_ADMIN_TOKEN", "").strip()
        legacy_token_file = data_dir / "admin-token.txt"
        if not admin_token and legacy_token_file.is_file():
            admin_token = legacy_token_file.read_text(encoding="utf-8").strip()
        return cls(
            data_dir=data_dir,
            vault_path=vault_path,
            admin_token=admin_token,
            admin_username=os.getenv("OBSYNC_ADMIN_USERNAME", "").strip(),
            admin_password=os.getenv("OBSYNC_ADMIN_PASSWORD", ""),
            host=os.getenv("OBSYNC_HOST", "0.0.0.0"),
            port=_env_int("OBSYNC_PORT", 7769),
            max_upload_mb=_env_int("OBSYNC_MAX_UPLOAD_MB", 100),
            max_extract_chars=_env_int("OBSYNC_MAX_EXTRACT_CHARS", 200_000),
            allow_registration=_env_bool("OBSYNC_ALLOW_REGISTRATION", True),
            secure_cookies=_env_bool("OBSYNC_SECURE_COOKIES", False),
            session_hours=max(1, _env_int("OBSYNC_SESSION_HOURS", 12)),
            remembered_session_days=max(1, _env_int("OBSYNC_REMEMBERED_SESSION_DAYS", 30)),
            local_setup_ips=_env_list("OBSYNC_LOCAL_SETUP_IPS"),
            log_level=os.getenv("OBSYNC_LOG_LEVEL", "info"),
        )

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.vault_path.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "tmp").mkdir(parents=True, exist_ok=True)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "obsync.db"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024
