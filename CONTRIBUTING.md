# Contributing

Thanks for helping improve Obsync.

## Before opening a pull request

1. Open an issue for substantial behavior or architecture changes.
2. Keep source-folder behavior read-only.
3. Add tests for every changed sync, security, or Markdown-preservation path.
4. Run the complete local checks:

```bash
uv sync --extra dev
uv run ruff check .
uv run ruff format --check .
uv run coverage run -m pytest
uv run coverage report
```

5. Test with temporary source and vault folders, never an important live vault.

## Code principles

- Safe and idempotent before clever
- One-way source ingestion only
- No setup-specific paths, hosts, tokens, or model names
- Local/offline behavior remains functional without an LLM
- Cross-platform paths and Windows behavior are first-class
- UI changes support both light and dark themes
- New model providers must preserve the untrusted-content prompt boundary

## Commit style

Use short imperative subjects such as `Add PDF OCR fallback` or `Fix rename reconciliation`.

## Security reports

Do not open public issues for vulnerabilities. Follow [SECURITY.md](SECURITY.md).

