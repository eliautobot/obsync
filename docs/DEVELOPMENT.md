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

The test suite covers path security, token/enrollment behavior, extractors, LLM response normalization, generated-note preservation, API authorization, complete source-to-vault synchronization, repeat updates, tombstones, rename identity, agent scanning, and UI/static delivery.

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

1. Start the API with a known test admin token.
2. Create and consume an enrollment.
3. Register a watched root.
4. Upload a fixture from an agent client.
5. Assert one generated Markdown note exists.
6. Add manual text below `## My notes`.
7. Modify and resync the source.
8. Assert manual text remains.
9. Remove the source and reconcile.
10. Assert the note remains and is marked missing.

## Release builds

GitHub Actions runs lint and tests on Linux and Windows, builds the Docker image, and uses PyInstaller to produce standalone Windows and Linux agent binaries for tagged releases. Release artifacts are convenience executables; the central server remains Docker-first.

