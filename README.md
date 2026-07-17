# Obsync

**Continuously turn folders from any computer into organized Obsidian Markdown.**

Obsync watches external folders on Windows, Linux, macOS, NAS devices, and network shares. When a file is added, changed, renamed, or removed, a lightweight agent reports it to one self-hosted server. The server extracts the content, optionally asks Ollama or LM Studio to classify it, and safely creates or updates an organized `.md` note inside an Obsidian vault.

The source stays untouched. Obsync is not a copy-for-copy file mirror; it is a live knowledge-ingestion layer.

> [!NOTE]
> Obsync is in early alpha. The core sync path, multi-device enrollment, local-LLM adapters, Docker server, and review workflow are implemented and tested. Keep backups of any vault used for testing.

## What it does

- Watches multiple folders on multiple computers
- Detects additions, updates, renames, and missing source files
- Scans first and compares every source with the actual managed note in Obsidian
- Requires an explicit Obsidian vault choice before Global Sync can start
- Shows green **In Obsidian**, orange **Modified**, and red **New/Missing** states before syncing
- Adopts matching existing Obsync notes so database rebuilds do not create duplicates
- Holds likely title matches for review before creating a second note
- Extracts text from Markdown, text, PDF, Word, Excel, PowerPoint, HTML, email, CSV, JSON, source code, and images with optional OCR
- Organizes documents with Ollama, LM Studio, or another OpenAI-compatible local model
- Provides immutable Full transfer and Brief summary AI profiles plus editable, copyable custom profiles
- Exposes the role prompt, prompt template, inference parameters, content behavior, and Obsidian organization controls
- Indexes the complete Markdown vault—content, folders, headings, aliases, properties, tags, links, backlinks, entities, and stable record identifiers—for whole-vault matching
- Updates a matching existing note in place instead of creating a duplicate, while preserving the original note and later manual additions
- Learns each vault's own organization model and adds only evidence-backed, materially relevant relationships—never links based on a shared word or document type alone
- Runs manual or scheduled Index and Maintenance Sweeps with live model reasoning/output, progress, safe Stop, Review/automatic modes, complete diffs, and Undo Sweep
- Falls back to deterministic rules whenever the LLM is unavailable
- Generates Obsidian properties, summaries, tags, categories, and `[[wikilinks]]`
- Preserves everything written below the generated note's **My notes** heading
- Sends uncertain classifications to a review queue
- Coordinates Windows, Linux, macOS, NAS, and network-share sources from one minimal web UI
- Includes Obsync Desktop for Windows with built-in folder watching, start/stop controls, silent background operation, and automatic startup
- Stops active sync and AI classification from one global control without changing source files or existing notes
- Starts, pauses, or stops each watched folder independently
- Updates computer, folder, document, and review status live without a page refresh
- Removes individual watched folders from a computer without deleting the real folder or existing Obsidian notes
- Safely disconnects old computers without deleting source files or Obsidian notes
- Explains controls with contextual `?` tips and a complete in-app Help center
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

Each watched computer makes outbound connections to the central server. Watched computers do not need inbound firewall ports. The central server owns the processing ledger. It can write to a Docker-mounted vault itself, or delegate safe managed-note writes to one selected desktop agent when the vault lives in Windows Documents or on another computer.

Read [Architecture](docs/ARCHITECTURE.md) and [Multi-PC setup](docs/MULTI_PC.md) for the full design.

## Quick start: central server

1. Clone the repository and enter it.
2. Copy `.env.example` to `.env`.
3. Start with the included vault mount, or point `OBSYNC_VAULT_HOST_PATH` at a host-accessible vault. Confirm the exact destination later in **Obsidian Vault**.
4. Start the server.

```bash
git clone https://github.com/eliautobot/obsync.git
cd obsync
cp .env.example .env
docker compose up -d --build
```

On the computer running Obsync, open `http://localhost:7769`. A fresh installation opens automatically as temporary **Admin** with no password and immediately asks you to register a local username and password. You may choose **Continue for now**, but a security warning remains visible and other computers cannot use the temporary login.

After the administrator account is secured, open `http://SERVER_IP:7769` from other computers and sign in normally. The default Compose file exposes port `7769`; use a private LAN/VPN or an HTTPS reverse proxy for remote access.

Example `.env` additions:

```dotenv
OBSYNC_VAULT_HOST_PATH=/absolute/path/to/your/ObsidianVault
OBSYNC_PUBLIC_URL=https://obsync.example.com
OBSYNC_BIND_IP=0.0.0.0
PUID=1000
PGID=1000
```

Passwords are hashed with scrypt in `/data/obsync.db`. Browser sessions use expiring HttpOnly cookies, CSRF protection, and login rate limiting. If you forget the login, reset it from the server:

```bash
docker compose exec -it obsync obsync admin reset-password --username admin
```

For headless or unattended deployments, `OBSYNC_ADMIN_USERNAME` and `OBSYNC_ADMIN_PASSWORD` can create the first account. Remove both values from `.env` after the first successful start. As a short-lived alternative, `OBSYNC_LOCAL_SETUP_IPS` can trust a comma-separated management IP for initial setup; remove it immediately after securing the account. Set `OBSYNC_SECURE_COOKIES=true` when the UI is served exclusively over HTTPS.

Upgrading from v0.1.0 is automatic. Open Obsync locally on the server to replace the old token with a username and password; remote browsers show where setup must be completed. Token access is disabled after the account is created.

## Updating

Update the Docker server first, then update each paired desktop agent to the same release. Obsync keeps its database in the `/data` volume and keeps the vault separate, so recreating the application container does not erase accounts, pairings, watched folders, or notes.

For a server installed by cloning this repository, run these commands from the Obsync folder:

```bash
git status --short
git pull --ff-only
docker compose build --pull
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:7769/api/v1/health
```

`git status --short` should return no output before you pull. Keep your existing `.env` file; do not replace it with `.env.example` during an update.

On Windows, use PowerShell and first enter the folder that actually contains both `.git` and `docker-compose.yml`:

```powershell
Set-Location "C:\Users\you\Documents\Obsync\obsync"
Test-Path .\.git
Test-Path .\docker-compose.yml
git status --short
git pull --ff-only
docker compose build --pull
docker compose up -d
docker compose ps
Invoke-RestMethod -Uri "http://127.0.0.1:7769/api/v1/health"
```

Both `Test-Path` commands must return `True`. If they do not, locate the Compose folder with `Get-ChildItem "$HOME\Documents" -Filter docker-compose.yml -Recurse` and change into the returned folder. PowerShell uses complete one-line commands; do not append Linux `\` line continuations.

If your Compose file uses the published `ghcr.io/eliautobot/obsync` image instead of building from the repository, run:

```bash
docker compose pull
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:7769/api/v1/health
```

Update Python-based desktop agents to the same release as the server, then verify and scan:

```bash
python -m pip install --upgrade \
  "obsync-app @ git+https://github.com/eliautobot/obsync.git@v0.16.0"
obsync --version
obsync agent scan
```

Replace `v0.16.0` with the release you are installing. For Windows, use **Sources → Add another computer → Download Obsync Desktop**, right-click it, choose **Run as administrator**, and then choose **Connect and install**. Elevation is required only for setup; the watcher runs with limited permissions and no visible terminal. Command-line Windows and Linux agents remain available for advanced installations.

Before any update, back up the Obsidian vault and Obsync `/data` volume. The full [Updating and rollback guide](docs/UPDATING.md) includes copy-and-paste backup commands for Linux and Windows, fixed-version installs, every agent type, verification, and safe rollback instructions.

## Computers and watched folders

The Obsync server appears automatically in **Sources** and is included in the Overview computer count. That card is the control plane; it is not a paired desktop. If Docker runs inside a VM or Docker Desktop, pair the physical Windows/macOS/Linux desktop whenever its folders or vault are outside the container—even if it is the same physical machine hosting Docker. Paired desktops are what appear in the folder and vault computer selectors.

Choose **Sources → Add another computer**. Create a one-time pairing code, download Obsync Desktop, right-click it and choose **Run as administrator**, click **Copy all setup details**, then use **Paste setup details** in the desktop app. Click **Connect and install**. It installs for the current Windows user, runs silently with limited permissions, and starts automatically at sign-in. Its window also provides **Start this PC**, **Stop this PC**, and **Open Obsync**. Once the computer card appears, confirm the exact destination under **Obsidian Vault**, choose **Start Global Sync**, then add folders.

Use **Disconnect** on a computer card to revoke an old desktop and remove its Obsync ledger. Source files and existing Obsidian notes are always kept. If the computer is the active vault writer, select another destination in **Obsidian Vault** first.

If a paired computer goes offline, use **Reconnect** on its card. First try **Open installed Desktop → Start this PC**; a valid saved pairing reconnects automatically. If its credential or installation is damaged, use the generated reconnect details with the latest Desktop installer. Repairing in place keeps the computer ID, watched folders, document history, and vault assignment.

Use **Stop Global Sync** to cancel all active sync and AI classification while keeping every connection intact. **Start Global Sync** resumes every running folder and immediately reconciles changes that occurred while stopped. Every folder also has independent **Start**, **Pause**, and **Stop** controls. Use **Remove** to forget only that folder; its originals and existing notes are kept.

Each watched folder shows a file comparison before syncing:

- Green **In Obsidian**: the source hash matches the managed note in the vault.
- Orange **Modified**: the source or its managed Obsidian representation changed.
- Red **New**: the source is not represented in Obsidian yet.
- Red **Missing**: the expected managed note or original source is missing.

Use **View files** to inspect the inventory, **Scan** to compare again without writing, and **Sync changes** to extract, classify, tag, and write only the new or changed items. Matching managed notes already in the vault are adopted instead of duplicated.

Before a new note is written, Obsync searches the whole-vault index using source identity, hashes, normalized content, titles, aliases, stable record identifiers, named entities, tags, paths, headings, and full text. Exact matches update the existing note automatically. Strong ordinary-note matches are held as **Possible duplicate** for first-time adoption unless automatic adoption is explicitly enabled. Ambiguous matches always require review. Once adopted, later source changes update the same note in place and preserve the original ordinary-note content outside Obsync's managed region.

The equivalent manual commands are:

```bash
python -m pip install "obsync-app @ git+https://github.com/eliautobot/obsync.git"
obsync agent pair --server https://obsync.example.com --code XXXX-XXXX-XXXX --name "Office PC"
obsync agent set-vault --browse
obsync agent add-folder --browse
obsync agent scan
obsync agent sync
obsync agent run
```

The `set-vault` command is optional. Use it only when that desktop contains the Obsidian vault. To enter a known path instead of browsing:

```powershell
obsync agent set-vault "C:\Users\me\Documents\My Vault"
obsync agent add-folder "C:\Users\me\Documents\Projects" --name "Projects"
obsync agent run
```

Release builds provide standalone agent executables so Python is not required. See [Getting started](docs/GETTING_STARTED.md) for background-service instructions.

## In-app help

Open **Help** from the sidebar or the top-right `?` button for a five-step quick start, page explanations, status-color definitions, Obsync Desktop guidance, local-model setup, safety behavior, and troubleshooting. Small `?` controls beside individual settings and terms show a concise explanation on hover, keyboard focus, or tap.

## Local LLM setup

Open the dedicated **Local AI** tab.

- Ollama: base URL such as `http://host.docker.internal:11434`, model such as `qwen3:8b`
- LM Studio: base URL such as `http://host.docker.internal:1234`, then select the model loaded in LM Studio
- OpenAI-compatible: use any compatible local endpoint and optional API key

Use **Check connection** before saving. The check lists available models instead of running inference, so it is fast and does not wait for a cold model to load. If the model stops, Obsync continues syncing using filenames, folders, extensions, and extracted text; those notes enter review when confidence is below the configured threshold.

Choose an active AI profile before syncing:

- **Full document transfer** puts the complete extracted source text in the note. The model adds organization metadata; its summary never replaces the document.
- **Brief summary** creates a compact note containing only the important information.
- **Custom profiles** can be copied from either built-in, renamed, edited, activated, and deleted. Built-ins remain immutable fallbacks.

Every custom profile exposes its role prompt, user-prompt template, content mode, input/output limits, temperature, Top P, candidate/tag/link limits, and controls for vault context, `[[wikilinks]]`, tags, YAML properties, category folders, and source details. The protected output schema and prompt-injection boundary are visible but read-only.

Obsidian has no remote core API for this workflow. Obsync integrates through Obsidian's native vault formats. Its scheduled or manual whole-vault index records note content, headings, aliases, properties, tags, paths, folders, links, backlinks, stable identifiers, hashes, and modification times. Corpus-adaptive retrieval shortlists relevant notes, then a per-vault Local AI model decides whether a real relationship exists. Returned path-qualified links require a specific relationship, grounded source and target evidence, and the configured confidence before Obsync performs the Markdown write.

## Whole-vault sweeps and maintenance

Open **Obsidian Vault** to run or schedule two independent operations:

- **Index Sweep** refreshes the agent's whole-vault knowledge. It can index only, send resulting recommendations to Review, or automatically apply them.
- **Maintenance Sweep** requires Local AI, learns or refreshes the vault-specific organization model, and recalculates evidence-backed links and tags. Similarity only retrieves candidates; it never creates a link.

Both sweeps support **Start**, **Stop**, daily/weekly/monthly/custom schedules, live note-level progress, and no-overlap protection. Review mode is the safe default. Automatic mode carries a prominent warning because it can change existing entries without human approval. Every applied sweep change stores expected hashes, evidence, confidence, and complete before/after content. Concurrent user edits stop the affected recommendation, and **Undo Sweep** restores changes that are still current. Sweeps never automatically delete or merge notes.

## Generated-note safety

Obsync owns the frontmatter and the content between these markers:

```markdown
<!-- obsync:generated:start -->
...
<!-- obsync:generated:end -->

## My notes
Anything here is preserved.
```

If an ordinary destination is not a verified exact match or an explicitly approved adoption, processing stops instead of overwriting it. Approved adoption preserves the entire original note below the managed section. Missing source files are marked in their notes and kept; deletion is never propagated automatically.

## Documentation

- [Project record](docs/PROJECT_RECORD.md)
- [Features and behavior](docs/FEATURES.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Getting started](docs/GETTING_STARTED.md)
- [Updating and rollback](docs/UPDATING.md)
- [Multi-PC and network shares](docs/MULTI_PC.md)
- [Supported files](docs/SUPPORTED_FILES.md)
- [Security model](docs/SECURITY.md)
- [Development and testing](docs/DEVELOPMENT.md)
- [v0.16.0 release notes](docs/releases/v0.16.0.md)
- [v0.15.1 release notes](docs/releases/v0.15.1.md)
- [v0.15.0 release notes](docs/releases/v0.15.0.md)
- [v0.14.0 release notes](docs/releases/v0.14.0.md)
- [v0.13.1 release notes](docs/releases/v0.13.1.md)
- [v0.13.0 release notes](docs/releases/v0.13.0.md)
- [v0.12.1 release notes](docs/releases/v0.12.1.md)
- [v0.12.0 release notes](docs/releases/v0.12.0.md)
- [v0.11.0 release notes](docs/releases/v0.11.0.md)
- [v0.10.0 release notes](docs/releases/v0.10.0.md)
- [v0.9.0 release notes](docs/releases/v0.9.0.md)
- [v0.8.0 release notes](docs/releases/v0.8.0.md)
- [v0.7.0 release notes](docs/releases/v0.7.0.md)

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run pytest
docker compose build
```

## License

MIT — see [LICENSE](LICENSE).
