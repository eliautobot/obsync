# Obsync

**Continuously turn folders from any computer into organized Obsidian Markdown.**

Obsync watches external folders on Windows, Linux, macOS, NAS devices, and network shares. When a file is added, changed, renamed, or removed, a lightweight agent reports it to one self-hosted server. The server extracts the content, optionally asks Ollama or LM Studio to classify it, and safely creates or updates an organized `.md` note inside an Obsidian vault.

The source stays untouched. Obsync is not a copy-for-copy file mirror; it is a live knowledge-ingestion layer.

> [!NOTE]
> Obsync is in early alpha. The core sync path, multi-device enrollment, local-LLM adapters, Docker server, and review workflow are implemented and tested. Keep backups of any vault used for testing.

## What it does

- Watches multiple folders on multiple computers
- Detects additions, updates, renames, and missing source files
- Extracts text from Markdown, text, PDF, Word, Excel, PowerPoint, HTML, email, CSV, JSON, source code, and images with optional OCR
- Organizes documents with Ollama, LM Studio, or another OpenAI-compatible local model
- Falls back to deterministic rules whenever the LLM is unavailable
- Generates Obsidian properties, summaries, tags, categories, and `[[wikilinks]]`
- Preserves everything written below the generated note's **My notes** heading
- Sends uncertain classifications to a review queue
- Coordinates Windows, Linux, macOS, NAS, and network-share sources from one minimal web UI
- Runs the central server in Docker; the watcher agent can run natively or in Docker
- Never moves, edits, or deletes a source file

## Architecture

```text
Windows PC ─┐
Linux PC   ─┼─ Obsync Agent ── outbound HTTP(S) ─┐
NAS/share  ─┘                                     │
                                                  ▼
                                         Obsync Server
                                  extract → classify → link
                                           │
                     Ollama / LM Studio ◀───┤
                                           ▼
                                  mounted Obsidian vault
                                           │
                                           ▼
                                        Obsidian
```

Each watched computer makes outbound connections to the central server. Watched computers do not need inbound firewall ports. The central server owns the processing ledger and is the only component that writes generated notes into the vault.

Read [Architecture](docs/ARCHITECTURE.md) and [Multi-PC setup](docs/MULTI_PC.md) for the full design.

## Quick start: central server

1. Clone the repository and enter it.
2. Copy `.env.example` to `.env`.
3. Point `OBSYNC_VAULT_HOST_PATH` at the Obsidian vault in `.env` or the Compose command.
4. Start the server.

```bash
git clone https://github.com/eliautobot/obsync.git
cd obsync
cp .env.example .env
docker compose up -d --build
```

Open `http://SERVER_IP:7769` and create the administrator username and password. The first browser to reach a fresh server can create the account, so complete setup immediately on a trusted private network. The default Compose file exposes port `7769`; use a private LAN/VPN or an HTTPS reverse proxy for remote access.

Example `.env` additions:

```dotenv
OBSYNC_VAULT_HOST_PATH=/absolute/path/to/your/ObsidianVault
OBSYNC_BIND_IP=0.0.0.0
PUID=1000
PGID=1000
```

Passwords are hashed with scrypt in `/data/obsync.db`. Browser sessions use expiring HttpOnly cookies, CSRF protection, and login rate limiting. If you forget the login, reset it from the server:

```bash
docker compose exec -it obsync obsync admin reset-password --username admin
```

For unattended deployments, `OBSYNC_ADMIN_USERNAME` and `OBSYNC_ADMIN_PASSWORD` can create the first account. Remove both values from `.env` after the first successful start. Set `OBSYNC_SECURE_COOKIES=true` when the UI is served exclusively over HTTPS.

Upgrading from v0.1.0 is automatic. Obsync asks for the old admin token once, then disables token access after the username/password account is created.

## Add a watched computer

In the web UI, choose **Sources → Add device** and create a one-time pairing code. On the watched computer:

```bash
python -m pip install "obsync-app @ git+https://github.com/eliautobot/obsync.git"
obsync agent pair --server https://obsync.example.com --code XXXX-XXXX-XXXX --name "Office PC"
obsync agent add-folder "/path/to/source" --name "Projects"
obsync agent run
```

On Windows, quote paths normally:

```powershell
obsync agent add-folder "C:\Users\me\Documents\Projects" --name "Projects"
obsync agent run
```

Release builds provide standalone agent executables so Python is not required. See [Getting started](docs/GETTING_STARTED.md) for background-service instructions.

## Local LLM setup

Open **Settings → Local AI organization**.

- Ollama: base URL such as `http://host.docker.internal:11434`, model such as `qwen3:8b`
- LM Studio: base URL such as `http://host.docker.internal:1234`, then select the model loaded in LM Studio
- OpenAI-compatible: use any compatible local endpoint and optional API key

Use **Test connection** before saving. If the model stops, Obsync continues syncing using filenames, folders, extensions, and extracted text; those notes enter review when confidence is below the configured threshold.

## Generated-note safety

Obsync owns the frontmatter and the content between these markers:

```markdown
<!-- obsync:generated:start -->
...
<!-- obsync:generated:end -->

## My notes
Anything here is preserved.
```

If a destination collision is not already an Obsync-managed note, processing stops instead of overwriting it. Missing source files are marked in their notes and kept; deletion is never propagated automatically.

## Documentation

- [Project record](docs/PROJECT_RECORD.md)
- [Features and behavior](docs/FEATURES.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Getting started](docs/GETTING_STARTED.md)
- [Multi-PC and network shares](docs/MULTI_PC.md)
- [Supported files](docs/SUPPORTED_FILES.md)
- [Security model](docs/SECURITY.md)
- [Development and testing](docs/DEVELOPMENT.md)

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
docker compose build
```

## License

MIT — see [LICENSE](LICENSE).
