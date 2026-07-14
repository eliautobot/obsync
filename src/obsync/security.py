from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata
from pathlib import Path


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), expected_hash)


def new_token(prefix: str = "obs") -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def new_enrollment_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    parts = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return "-".join(parts)


def slugify(value: str, fallback: str = "untitled", max_length: int = 80) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", ascii_value)
    cleaned = re.sub(r"[\s_]+", "-", cleaned).strip(" .-").lower()
    return cleaned[:max_length].rstrip(".-") or fallback


def safe_relative_path(value: str) -> Path:
    if re.match(r"^[A-Za-z]:[\\/]", value):
        raise ValueError("Path must be relative and cannot contain a drive prefix")
    candidate = Path(value.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("Path must be relative and cannot contain '..'")
    return Path(*(part for part in candidate.parts if part not in {"", "."}))


def safe_vault_path(vault_root: Path, relative: str | Path) -> Path:
    root = vault_root.resolve()
    rel = safe_relative_path(str(relative))
    result = (root / rel).resolve()
    if not result.is_relative_to(root):
        raise ValueError("Destination escapes the vault")
    return result


def redact_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••••••"
    return f"{value[:3]}••••{value[-3:]}"
