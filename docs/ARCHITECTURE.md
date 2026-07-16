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
- Review queue, bounded live processing activity with authenticated server-sent subscribers, event history, and remote command queue

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

### Obsync Desktop for Windows

Obsync Desktop packages the watch-agent runtime, pairing, background startup, local watcher controls, and a dashboard shortcut in one windowed app. Published server images serve the matching executable directly. It accepts all setup details through one clipboard paste, pairs with a one-time code, stores device configuration separately from the executable, copies the versioned app into Local AppData, creates and verifies a limited current-user `ONLOGON` task, registers the per-user `obsync://` app link, and starts the background runtime with no console window. Upgrading replaces the legacy `Obsync Companion` startup task without losing pairing, roots, or vault selection. Only one setup window may run at a time; valid pairings are reused for repair. The setup window requires one-time elevation because some Windows installations reject Task Scheduler registration otherwise. The installed task still runs with limited permissions. Obsync Desktop does not install a Windows service or open an inbound port.

### Pipeline control

The server stores a durable global running/stopped flag plus an independent running/paused/stopped state for every root. Global Stop cancels queued and active work across all roots. A per-root Pause or Stop cancels only that root while other folders continue. Starting either level queues immediate inventory and pending-work reconciliation rather than waiting for the periodic interval. Configuration and safe folder-removal commands remain available.

### LLM providers

The server—not each agent—connects to the configured model endpoint. Supported protocols:

- Ollama `/api/chat` for classification and `/api/tags` for connection checks
- OpenAI-compatible `/v1/chat/completions` for classification and `/v1/models` for connection checks, including LM Studio

The server sends extracted text, metadata, and an optional allowlist of candidate related-note titles. User organization instructions are appended below a protected system prompt and cannot replace its JSON or untrusted-document rules. The model has no Obsidian API access. Obsync itself indexes the selected vault and rejects related links not present in the allowlist.

Classification requests use streaming responses when the provider supports them. Each model activity update is pushed to authenticated browsers over a server-sent event stream instead of waiting for the general dashboard refresh interval. The Local AI page updates stable session elements in place and keeps independent follow/manual-scroll state for every document trace. The trace is bounded in memory and is not persisted as a chat transcript. Browser disconnects remove their bounded subscriber queue in a `finally` cleanup path. An administrator can cancel an individual inference without stopping the global pipeline; the source file remains untouched and the document moves to Review.

## Data flow

```text
1. User adds a folder, an OS event occurs, or periodic reconciliation starts
2. Agent inventories stable files with relative paths, size, modification time, and SHA-256
3. Server compares the manifest with its ledger and the active Obsidian vault writer
4. Existing managed notes are adopted by source identity instead of duplicated
5. Strong title matches against any Markdown note are held for duplicate review
6. UI reports in-sync, modified, new, possible-duplicate, vault-missing, and source-missing states
7. Sync uploads only pending source files
8. Server checks agent token, root ownership, size, hash, and safe path
9. Extractor produces bounded plain text
10. LLM activity is pushed live to subscribed browsers and returns structured classification, or rules provide fallback
11. Server chooses/stabilizes destination and renders Markdown
12. Server merges the preserved manual section
13. Server writes atomically under `/vault`, or queues the managed note for the selected desktop vault writer
14. SQLite ledger, comparison state, and event stream are updated
```

At any processing boundary, the global Stop control can cancel the operation before a vault write. Cancelled documents remain pending/paused and are eligible for reconciliation after Start. The narrower **Stop inference** action cancels only one active model request and routes that document to Review while Global Sync remains enabled.

Review decisions are explicit. Approve accepts the current classification or existing duplicate match, Disregard creates no new note, never deletes an existing one, and remains ignored across ordinary rescans, Redo AI review forces an unchanged source through classification again with optional one-run reviewer feedback, and Create separate note overrides a possible-duplicate hold.

## Identity and idempotency

A source document is identified by `(agent_id, root_id, relative_path)`. Each gets an immutable UUID. Content SHA-256 makes repeated events idempotent. Device registration is transactional and accepts a client-generated credential so a lost response can be retried without creating another computer. Rename hints let the server update the existing row and note rather than creating a new identity. If the processing database is rebuilt, a vault audit matches managed notes by source computer, watched-root name, and relative path before creating anything new.

The server keeps the first safe destination path after classification. Later content changes update that path in place. A future explicit reorganization feature may move notes with backlink-aware review, but routine synchronization does not.

## Generated-content boundary

Obsync replaces the properties and generated region. Text following `## My notes` is merged back byte-for-byte. If a destination exists without Obsync markers, the server stops with an error rather than assuming ownership.

## Failure and recovery

- Agent offline: changes remain on disk and the next rescan finds them.
- Server offline: agent requests fail, local ledger remains unchanged, and later scans retry.
- LLM offline/malformed: rule-based analysis completes the note and usually sends it to review.
- LLM stopped by user: that inference is cancelled, its document enters Review, and other synchronization remains active.
- Process crash during write: atomic replace leaves either the old complete note or the new complete note.
- Filesystem event missed: periodic full reconciliation repairs state.
- Source removed: note is retained and marked `source-missing`.
- Upload interrupted: staged temporary file is removed; no ledger success is recorded.
- Pipeline stopped: active extraction/classification is cancelled, queued work is cancelled, agents remain connected, and restart reconciles missed changes.
- Watched folder removed: the desktop forgets the root and its local/server ledger; original files and existing notes remain untouched.
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
