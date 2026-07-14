from __future__ import annotations

import base64
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


_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_PASSWORD_MAX_BYTES = 1024


def validate_username(username: str) -> str:
    value = username.strip()
    if not 3 <= len(value) <= 64:
        raise ValueError("Username must be between 3 and 64 characters")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError("Username may contain letters, numbers, periods, underscores, and hyphens")
    return value


def validate_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Password must be at least 10 characters")
    if len(password.encode("utf-8")) > _PASSWORD_MAX_BYTES:
        raise ValueError("Password is too long")
    return password


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    validate_password(password)
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    encoded_salt = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    encoded_digest = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${encoded_salt}${encoded_digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        if len(password.encode("utf-8")) > _PASSWORD_MAX_BYTES:
            return False
        algorithm, n, r, p, encoded_salt, encoded_digest = encoded.split("$", 5)
        if algorithm != "scrypt" or (int(n), int(r), int(p)) != (
            _SCRYPT_N,
            _SCRYPT_R,
            _SCRYPT_P,
        ):
            return False
        salt = base64.urlsafe_b64decode(encoded_salt + "=" * (-len(encoded_salt) % 4))
        expected = base64.urlsafe_b64decode(encoded_digest + "=" * (-len(encoded_digest) % 4))
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(candidate, expected)
    except (ValueError, TypeError, OverflowError):
        return False


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
