from __future__ import annotations

from pathlib import Path

import pytest

from obsync.security import (
    hash_password,
    hash_token,
    new_enrollment_code,
    new_token,
    safe_relative_path,
    safe_vault_path,
    slugify,
    validate_password,
    validate_username,
    verify_password,
    verify_token,
)


def test_tokens_are_random_and_verifiable() -> None:
    first = new_token("agent")
    second = new_token("agent")
    assert first.startswith("agent_")
    assert first != second
    assert verify_token(first, hash_token(first))
    assert not verify_token(second, hash_token(first))


def test_passwords_are_salted_and_verifiable() -> None:
    first = hash_password("a memorable password")
    second = hash_password("a memorable password")
    assert first.startswith("scrypt$")
    assert first != second
    assert verify_password("a memorable password", first)
    assert not verify_password("wrong password", first)
    assert not verify_password("a memorable password", "not-a-valid-hash")


@pytest.mark.parametrize("password", ["short", "123456789"])
def test_weak_passwords_are_rejected(password: str) -> None:
    with pytest.raises(ValueError, match="at least 10"):
        validate_password(password)


@pytest.mark.parametrize("username", ["ab", "admin user", "admin/owner"])
def test_invalid_usernames_are_rejected(username: str) -> None:
    with pytest.raises(ValueError):
        validate_username(username)


def test_enrollment_code_is_human_readable() -> None:
    code = new_enrollment_code()
    assert len(code.split("-")) == 3
    assert all(len(part) == 4 for part in code.split("-"))


@pytest.mark.parametrize(
    "value", ["../secret", "folder/../../secret", "/absolute/file", "C:\\absolute\\file"]
)
def test_unsafe_relative_paths_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        safe_relative_path(value)


def test_safe_vault_path_stays_inside_root(tmp_path: Path) -> None:
    result = safe_vault_path(tmp_path, "Knowledge/Invoices/invoice.md")
    assert result == tmp_path / "Knowledge" / "Invoices" / "invoice.md"
    with pytest.raises(ValueError):
        safe_vault_path(tmp_path, "../../escape.md")


def test_slugify_handles_unicode_and_unsafe_characters() -> None:
    assert slugify("  Résumé / Q3: Plans?  ") == "resume-q3-plans"
    assert slugify("***", fallback="document") == "document"
