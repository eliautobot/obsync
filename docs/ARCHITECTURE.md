# Architecture

## Components

### Central server

The server is the control plane and sole vault writer. It provides:

- FastAPI HTTP API and embedded web UI
- SQLite processing ledger in WAL mode
- One-time enrollment and hashed device tokens
- Upload staging with size limits and SHA-256 verification
- Format-specific extraction
- Optional local-LLM classification
- Atomic generated-note writes
- Review queue, event history, and remote command queue

The Docker container receives two persistent mounts:

- `/data`: database, generated admin token, and temporary uploads
- `/vault`: an Obsidian vault or a dedicated folder inside one

### Watch agent

The agent runs beside source folders. It never needs access to the Obsidian vault. It provides:

- OS filesystem events through `watchfiles`
- Periodic reconciliation to catch missed events or offline changes
- File-settle checks
- Local SHA-256 ledger
- Multipart upload over HTTP(S)
- Missing-source notices
- Polling for central scan/retry commands

Every network connection starts from the agent. No inbound port is required on a watched device.

### LLM providers

The server—not each agent—connects to the configured model endpoint. Supported protocols:

- Ollama `/api/chat`
- OpenAI-compatible `/v1/chat/completions`, including LM Studio

The server sends extracted text, metadata, and an allowlist of candidate related-note titles. It rejects related links not present in that allowlist.

## Data flow

```text
1. OS event or periodic rescan
2. Agent waits until size and modification time settle
3. Agent compares local mtime/size/hash ledger
4. Agent uploads changed file + relative-path manifest
5. Server checks agent token, root ownership, size, hash, and safe path
6. Extractor produces bounded plain text
7. LLM returns structured classification, or rules provide fallback
8. Server chooses/stabilizes destination and renders Markdown
9. Server merges the preserved manual section
10. Atomic replace writes the note under /vault
11. SQLite ledger and event stream are updated
```

## Identity and idempotency

A source document is identified by `(agent_id, root_id, relative_path)`. Each gets an immutable UUID. Content SHA-256 makes repeated events idempotent. Rename hints let the server update the existing row and note rather than creating a new identity.

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

## Scaling model

SQLite and single-server vault writing are intentional. Obsidian vaults are filesystem-oriented, and one writer prevents conflicting output. A single server comfortably coordinates many agents and typical personal/team document collections. Upload and LLM work can later move to a bounded worker queue without changing the agent protocol.

## Trust boundaries

```text
[Untrusted source content]
        │ bounded extractor / prompt boundary
        ▼
[Authenticated server process]
        │ validated relative destination
        ▼
[Mounted Obsidian vault]
```

Admin and agent tokens have separate privileges. Enrollment codes are single-use and expire. Tokens are stored as SHA-256 digests in the server database. Production deployments should terminate TLS at a private reverse proxy or run entirely over a private VPN/LAN.

