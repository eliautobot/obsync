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

The active AI profile controls the role prompt, user-prompt template, provider parameters, input/output bounds, note content mode, and Obsidian organization features. The immutable Full document transfer profile makes the complete extracted source the note body; model output supplies metadata and a separate search-oriented summary. The immutable Brief summary profile produces a compact summary-only body. Custom profiles can be copied from either built-in and edited independently. The protected JSON schema and untrusted-document boundary remain visible but read-only.

When vault context is enabled, Obsync searches a persistent whole-vault index built from full Markdown content, relative paths, folders, titles, aliases, headings, YAML properties, YAML/inline tags, outgoing links, backlinks, stable record identifiers, hashes, and modification times. The index produces a private Vault Knowledge Graph: paths are canonical document identities; aliases resolve to those documents; labeled identifiers become durable entity nodes; existing folders and human links become structural edges; and inverse document frequency supplies entity/anchor specificity. A corpus-adaptive index retrieves a bounded candidate allowlist, but retrieval similarity never creates a link. Local AI learns a separate model for each vault, including observed entity and relationship types, and adjudicates candidates using the vault's organization. Each accepted path-qualified link must exactly match the allowlist, name canonical source and target entities, provide a specific directional snake-case predicate, use a graph-specific existing anchor, cite grounded source and target facts, and pass the configured confidence floor. Generic predicates such as `related_to` and `links_to` are rejected. Obsidian does not expose a remote core API for these operations, so Obsync uses its native filesystem contract: Markdown, YAML properties, tags, folders, and `[[wikilinks]]`.

Index and maintenance work is separate from ordinary source reconciliation. A manual or scheduled Index Sweep is strictly read-only: it refreshes the full catalog and deterministic graph incrementally or as a rebuild and records the vault's observed corpus structure without invoking Local AI or changing Markdown. Before maintenance learning, stored tags and links are reparsed from current Markdown and Obsync-owned tags are excluded from the human vocabulary. A Maintenance Sweep learns or refreshes the adaptive vault model, retrieves candidates, and asks Local AI to propose evidence-backed native edits. The AI path and the deterministic reciprocal/hub/navigation paths share one graph eligibility gate, so a shortcut cannot bypass canonical endpoints, typed predicates, or anchor specificity. Accepted relationships wrap an exact existing body phrase whose source sentence supports the target; dates, numbers, metadata labels, shorthand aliases such as `backup plan`, protected Markdown, and already-linked targets are rejected. Accepted tags need their own grounded evidence and confidence. Exact duplicates are identified deterministically before AI inference. Folder placement, category-index membership, and canonical-note selection are separate review-only operations. No visible Obsync maintenance block is added, and legacy blocks are proposed for removal during the next Maintenance Sweep. Existing-document matching, source freshness, generated properties, and first folder placement stay in the ordinary source-reconciliation path, where Obsync has source identity and complete extracted content. Server-mounted sweeps process notes cooperatively in the server; Desktop-vault sweeps stream bounded index batches, report progress, and honor stop requests through authenticated agent endpoints. Only one sweep can run at a time, and startup closes orphaned sweep state left by an interrupted process.

Every maintenance write uses the indexed content hash as an optimistic concurrency check. The database stores complete before/after content, reason, evidence, confidence, review state, sweep identity, and ownership for each operation. Review can approve a safe subset of a card's links, tags, or organization operations; only selected operations become the applied audit record. Automatic mode applies the same validated native content operations without per-note approval, while organization and duplicate decisions remain review-only. Ordinary source sync rebases an owned native edit only while that edit remains present in the current note, so a later human removal is respected. Moves are not proposed for managed notes, backlink targets, or notes with a competing content recommendation; approved moves carry ownership and canonical-resolution paths with them. Undo reverses only notes whose current hash still matches the applied result and updates operation ownership. Sweeps never automatically delete or merge notes.

Classification and vault-sweep requests use streaming responses when the provider supports them. Each model activity update is pushed to authenticated browsers over a server-sent event stream instead of waiting for the general dashboard refresh interval. The Local AI page and the Index/Maintenance Sweep panels update stable session elements in place and keep independent follow/manual-scroll state for every trace. Sweep sessions include the current note, vault-model learning, provider reasoning/output, validated decisions, errors, and final outcome; an index-only sweep explicitly has no AI session. Traces are bounded in memory and are not persisted as chat transcripts. Browser disconnects remove their bounded subscriber queue in a `finally` cleanup path. An administrator can cancel an individual document inference without stopping the global pipeline; the source file remains untouched and the document moves to Review. **Stop Sweep** cancels the active model request and prevents any later note from beginning. On startup, Obsync safely closes queued, running, or stopping sweep rows and resets a model-learning record that cannot still have a live task.

## Data flow

```text
1. User adds a folder, an OS event occurs, or periodic reconciliation starts
2. Agent inventories stable files with relative paths, size, modification time, and SHA-256
3. Server compares the manifest with its ledger and the active Obsidian vault writer
4. Existing managed notes are matched by source identity and updated instead of duplicated
5. The extracted source is compared with the whole-vault index using hashes, normalized content, titles, aliases, record identifiers, entities, and full-text relevance
6. Exact ordinary-note matches are safely adopted; strong matches enter Review; ambiguous matches never auto-merge
7. UI reports in-sync, modified, new, possible-duplicate, vault-missing, and source-missing states
8. Sync uploads only pending source files
9. Server checks agent token, root ownership, size, hash, and safe path
10. Extractor produces plain text bounded by the active AI profile
11. Whole-vault ranking supplies validated related notes, entity evidence, content excerpts, and an existing-folder candidate
12. LLM activity is pushed live to subscribed browsers and returns structured organization metadata, or rules provide fallback
13. Only model-selected relationships with exact real-vault targets and grounded two-sided evidence survive validation
14. Server chooses/stabilizes the existing or new destination and renders the configured full-text or summary-only Markdown body
15. Server preserves the managed manual section or the complete pre-adoption ordinary note
16. Server writes atomically under `/vault`, or queues the managed note for the selected desktop vault writer
17. SQLite document ledger and whole-vault index are updated before the next source document is processed
```

At any processing boundary, the global Stop control can cancel the operation before a vault write. Cancelled documents remain pending/paused and are eligible for reconciliation after Start. The narrower **Stop inference** action cancels only one active model request and routes that document to Review while Global Sync remains enabled.

Review decisions are explicit. Approve accepts the current classification or existing duplicate match, Disregard creates no new note, never deletes an existing one, and remains ignored across ordinary rescans, Redo AI review forces an unchanged source through classification again with optional one-run reviewer feedback, and Create separate note overrides a possible-duplicate hold.

## Identity and idempotency

A source document is identified by `(agent_id, root_id, relative_path)`. Each gets an immutable UUID. Content SHA-256 makes repeated events idempotent. Device registration is transactional and accepts a client-generated credential so a lost response can be retried without creating another computer. Rename hints let the server update the existing row and note rather than creating a new identity. If the processing database is rebuilt, a vault audit matches managed notes by source computer, watched-root name, and relative path before creating anything new.

The server keeps the adopted or first safe destination path after classification. Later content changes update that path in place. New notes can reuse the folder of a high-confidence related note; otherwise they fall back to the configured destination/device/root/category path. Routine source updates never move an established note.

## Generated-content boundary

Obsync replaces the properties and generated region. Text following `## My notes` is merged back byte-for-byte. If a destination exists without Obsync markers, the server stops unless the whole-vault matcher proves an exact content/source match or an administrator explicitly approves first-time adoption. Adoption preserves the complete original note below the generated region.

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
