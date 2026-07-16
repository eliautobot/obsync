# Development and testing

## Local environment

```bash
uv sync --extra dev
uv run obsync server
```

By default the development server uses `./data` and `./vault`. Never point development tests at an important vault.

## Quality checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run coverage run -m pytest
uv run coverage report
```

The test suite covers local-only temporary Admin, remote and cross-site setup rejection, password hashing, account changes, first-run and legacy auth migration, sessions, CSRF, login throttling, device enrollment, concurrent idempotent pairing, pair/disconnect cleanup, native folder selection, source inventories, source-to-vault comparisons, existing-note adoption, local and remote vault writers, Obsync Desktop installation/startup/stop/repair, global pipeline cancellation, independent AI cancellation, streamed model activity, authenticated activity events, repeated gateway subscriber cleanup, review approval/disregard/feedback-driven re-review, safe folder removal, path security, extractors, LLM normalization, generated-note preservation, updates, tombstones, rename identity, and UI delivery.

The CLI and windowed Desktop entry module are excluded from the aggregate coverage denominator because their process, Tk, and Windows-shell boundaries are validated through parser/unit tests, real Windows packaging, scheduled-task lifecycle checks, and launch smoke tests.

## Docker validation

```bash
docker compose build --pull
docker compose up -d
curl --fail http://127.0.0.1:7769/api/v1/health
docker compose logs --no-color obsync
docker compose down
```

## End-to-end test shape

Use temporary directories for source, data, and vault:

1. Start the API with temporary data and vault directories.
2. Create an administrator account and authenticated session.
3. Create and consume an enrollment.
4. Register a watched root.
5. Inventory a fixture and assert it is red/new.
6. Sync it and assert the managed note is green/in-sync.
7. Add manual text below `## My notes`.
8. Modify, rescan, and assert orange/modified before resyncing.
9. Assert manual text remains.
10. Remove the generated note and assert red/vault-missing, then repair it.
11. Remove the source and reconcile.
12. Repeat with a paired desktop vault writer and complete the audit/write commands.
13. Assert the note remains and is marked source-missing.
14. Stream a model response, verify the current file and activity trace, cancel it, and assert Global Sync stays enabled while the file enters Review.
15. Exercise Approve, Disregard, and forced Redo AI review with feedback.
16. Load a large document inventory and verify bounded panel scrolling, scroll retention across live refreshes, and no horizontal overflow at desktop or mobile widths.
17. Stream rapid updates for two simultaneous AI documents; verify sub-second rendering, independent follow/manual-scroll state, return-to-live controls, stable page position, and zero subscriber/timer leaks after repeated reload and sign-out cycles.

## Release builds

GitHub Actions runs lint and tests on Linux and Windows, builds the Docker image, and uses PyInstaller to produce standalone Windows and Linux command-line agents plus Obsync Desktop for tagged releases. Versioned release notes under `docs/releases/` are published verbatim when present. The central server remains Docker-first.
