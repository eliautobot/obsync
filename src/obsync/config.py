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


@dataclass(slots=True)
class Settings:
    data_dir: Path
    vault_path: Path
    admin_token: str
    host: str = "0.0.0.0"
    port: int = 7769
    max_upload_mb: int = 100
    max_extract_chars: int = 200_000
    allow_registration: bool = True
    log_level: str = "info"

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = Path(os.getenv("OBSYNC_DATA_DIR", "./data")).expanduser().resolve()
        vault_path = Path(os.getenv("OBSYNC_VAULT_PATH", "./vault")).expanduser().resolve()
        return cls(
            data_dir=data_dir,
            vault_path=vault_path,
            admin_token=os.getenv("OBSYNC_ADMIN_TOKEN", "").strip(),
            host=os.getenv("OBSYNC_HOST", "0.0.0.0"),
            port=_env_int("OBSYNC_PORT", 7769),
            max_upload_mb=_env_int("OBSYNC_MAX_UPLOAD_MB", 100),
            max_extract_chars=_env_int("OBSYNC_MAX_EXTRACT_CHARS", 200_000),
            allow_registration=_env_bool("OBSYNC_ALLOW_REGISTRATION", True),
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
