# Architecture

## Components

### Central server

The server is the control plane and authoritative processing ledger. It provides:

- FastAPI HTTP API and embedded web UI
- SQLite processing ledger in WAL mode
- Local-only temporary Admin bootstrap followed by scrypt-hashed credentials and expiring browser sessions
- One-time enrollment and hashed device tokens
- Upload staging with size limits and SHA-256 verification
- Format-specific extraction
- Optional local-LLM classification
- Atomic local generated-note writes or authenticated remote write commands
- Review queue, event history, and remote command queue

The Docker container receives two persistent mounts in server-mounted mode:

- `/data`: database, administrator sessions, and temporary uploads
- `/vault`: an Obsidian vault or a dedicated folder inside one

### Watch agent

The agent runs beside source folders. It can optionally be selected as the single vault writer when the vault is on that desktop. It provides:

- OS filesystem events through `watchfiles`
- Periodic reconciliation to catch missed events or offline changes
- File-settle checks
- Local SHA-256 ledger
- Multipart upload over HTTP(S)
- Missing-source notices
- Polling for central scan/retry commands
- Native folder selection for watched roots and an optional vault
- Source inventory manifests and desktop-vault audits
- Safe atomic writes restricted to Obsync-managed notes below the selected vault

Every network connection starts from the agent. No inbound port is required on a watched device.

### Windows Companion

The Windows Companion packages the same watch-agent runtime in a windowed setup application. Published server images serve the matching executable directly. It accepts all setup details through one clipboard paste, pairs with a one-time code, stores the device configuration in the user's Obsync config directory, copies the versioned executable into Local AppData, creates and verifies a limited current-user `ONLOGON` Task Scheduler entry, and starts the background runtime through that tracked task with no console window. Only one setup window may run at a time; valid pairings are reused for repair. It does not install a Windows service, request elevation, or open an inbound port. The command-line agent remains available for advanced and non-Windows deployments.

### LLM providers

The server—not each agent—connects to the configured model endpoint. Supported protocols:

- Ollama `/api/chat` for classification and `/api/tags` for connection checks
- OpenAI-compatible `/v1/chat/completions` for classification and `/v1/models` for connection checks, including LM Studio

The server sends extracted text, metadata, and an allowlist of candidate related-note titles. It rejects related links not present in that allowlist.

## Data flow

```text
1. User adds a folder, an OS event occurs, or periodic reconciliation starts
2. Agent inventories stable files with relative paths, size, modification time, and SHA-256
3. Server compares the manifest with its ledger and the active Obsidian vault writer
4. Existing managed notes are adopted by source identity instead of duplicated
5. UI reports in-sync, modified, new, vault-missing, and source-missing states
6. Sync uploads only pending source files
7. Server checks agent token, root ownership, size, hash, and safe path
8. Extractor produces bounded plain text
9. LLM returns structured classification, or rules provide fallback
10. Server chooses/stabilizes destination and renders Markdown
11. Server merges the preserved manual section
12. Server writes atomically under `/vault`, or queues the managed note for the selected desktop vault writer
13. SQLite ledger, comparison state, and event stream are updated
```

## Identity and idempotency

A source document is identified by `(agent_id, root_id, relative_path)`. Each gets an immutable UUID. Content SHA-256 makes repeated events idempotent. Device registration is transactional and accepts a client-generated credential so a lost response can be retried without creating another computer. Rename hints let the server update the existing row and note rather than creating a new identity. If the processing database is rebuilt, a vault audit matches managed notes by source computer, watched-root name, and relative path before creating anything new.

The server keeps the first safe destination path after classification. Later content changes update that path in place. A future explicit reorganization feature may move notes with backlink-aware review, but routine synchronization does not.

## Generated-content boundary

Obsync replaces the properties and generated region. Text following `## My notes` is merged back byte-for-byte. If a destination exists without Obsync markers, the server stops with an error rather than assuming ownership.

## Failure and recovery

- Agent offline: changes remain on disk and the next rescan finds them.
- Server offline: agent requests fail, local ledger remains unchanged, and later scans retry.
- LLM offline/malformed: rule-based analysis completes the note and usually sends it to review.
- Process crash during write: atomic replace leaves either the old complete note or the new complete note.
- Filesystem event missed: periodic full reconciliation repairs state.
- Source removed: note is retained and marked `source-missing`.
- Upload interrupted: staged temporary file is removed; no ledger success is recorded.
- Computer disconnected: its credential is revoked and its server ledger is removed; original source files and existing Obsidian notes remain untouched.

## Scaling model

SQLite and a single selected vault writer are intentional. Obsidian vaults are filesystem-oriented, and one writer prevents conflicting output. A single server comfortably coordinates many agents and typical personal/team document collections. Upload and LLM work can later move to a bounded worker queue without changing the agent protocol.

## Trust boundaries

```text
[Untrusted source content]
        │ bounded extractor / prompt boundary
        ▼
[Authenticated server process]
        │ validated relative destination
        ▼
[Mounted vault or selected desktop vault writer]
```

Before registration, temporary Admin is accepted only when the network peer is loopback/the Docker host gateway and the request targets a loopback hostname, or when the peer was explicitly allowlisted for setup. It has no reusable credential; cross-site writes are rejected. A remote browser cannot claim a fresh server. After registration, temporary access disappears.

Administrator passwords are scrypt-hashed. Random browser session and CSRF values are stored only as SHA-256 digests; the session credential is held in an HttpOnly, SameSite cookie. Login attempts are rate limited. Agent tokens have separate privileges, and enrollment codes are single-use and expire. Production deployments should terminate TLS at a private reverse proxy or run entirely over a private VPN/LAN.
