# Features and product behavior

## Product goal

Obsync is a one-way, continuously reconciled knowledge pipeline from arbitrary folders into an Obsidian vault. It does not mirror the source byte for byte. It produces human-readable Markdown representations that Obsidian can index, link, query, and show in Graph View or Bases.

## Source monitoring

- Any number of agents per server
- Any number of roots per agent
- Native paths, removable drives, mounted NAS paths, SMB shares, and NFS shares
- Immediate filesystem events plus periodic full rescans
- Stable-file checks to avoid uploading a document while another application is still writing it
- SHA-256 content comparison to avoid redundant processing
- Rename hints so the existing generated note can keep its identity
- Tombstones for missing files; no automatic note deletion
- Include and exclude globs per root
- Agent-local SQLite state for restart and offline recovery
- Explicit inventory pass before processing
- Per-file vault comparison: in-sync, modified, new, vault-missing, or source-missing
- Existing managed-note adoption to prevent overlaps and duplicate output

## Knowledge processing

- Deterministic text extraction before LLM use
- Optional OCR for common image formats
- Prompt-injection boundary: source content is explicitly marked as untrusted data
- Ollama native API adapter
- LM Studio and generic OpenAI-compatible API adapter
- Strict structured response schema and normalization
- Rules-only fallback so model downtime never stops synchronization
- Review threshold based on classification confidence
- Related-note candidate selection and exact-title validation before creating `[[wikilinks]]`

## Obsidian output

Each document receives:

- Stable `obsync_id`
- Source machine, watched root, relative source path, hash, type, and update properties
- Human-readable title
- Summary
- Category
- Tags
- Related-note wikilinks when the model selects an allowed candidate
- Source details and extracted content
- A manual section that Obsync preserves across updates

Generated notes are grouped by destination prefix, source device, watched folder, and initial category. Their destination remains stable on later edits, preventing routine reclassification from breaking backlinks.

## Central control panel

- Responsive minimal interface
- Automatic system/light/dark theme with manual toggle
- Automatic local temporary Admin with immediate account-security prompt
- Persistent unsecured-account warning and remote setup lockout until registration
- First-run username/password registration with safe v0.1.0 token migration
- Expiring browser sessions, sign-out, CSRF protection, and login throttling
- Dashboard counts and event stream
- Device and watched-root status
- Native **Add folder**, per-folder **Scan**, **View files**, and **Sync changes** controls
- Global **Start syncing / Stop syncing** control with active sync and AI cancellation
- Per-folder **Remove** action that preserves originals and existing notes
- Green/orange/red comparison indicators and aggregate folder counts
- One-time device enrollment codes
- Remote scan command
- Searchable document table
- Error retry commands
- Review approval workflow
- LLM configuration and connection test
- Fast model discovery that does not start inference
- Automatic server computer plus optional paired desktops
- Native desktop folder picker for watched roots and the vault
- Account menu with administrator username/password management and explicit sign-out
- Obsync Desktop for Windows with bundled watcher, local start/stop controls, dashboard shortcut, per-user installation, silent background operation, and automatic sign-in startup
- Single-instance, retry-safe Windows pairing with one-click copy/paste setup, legacy-task migration, and startup repair
- Safe computer disconnect that preserves source files and existing Obsidian notes
- Matching Obsync Desktop executable served directly by published Obsync containers
- Contextual `?` explanations across controls and a complete in-app Help center
- Top-layer notifications that remain sharp and readable above open dialogs

## Safety guarantees

- Source folders are read-only from Obsync's perspective
- The active writer writes only below the configured mounted or desktop vault path
- Path traversal is rejected
- Non-Obsync note collisions are never overwritten
- Writes use a temporary sibling file followed by an atomic replace
- Manual note content below the generated boundary is preserved
- Source deletion is represented as status, never propagated as note deletion
- Stopping work never disconnects computers, deletes source files, or deletes existing notes
- Removing a watched folder deletes only Obsync's connection and ledger for that root
- API secrets are never returned to the UI after storage
- Administrator passwords and browser session credentials are never stored in plaintext

## Deliberate non-goals for the first release

- Two-way edits from Obsidian back into source documents
- Automatic source file moves or renames
- Cloud-hosted accounts or billing
- Embedding/vector search as a hard dependency
- Replacing Obsidian Sync for vault-to-vault replication
