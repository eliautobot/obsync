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
- Existing-note matching across managed and ordinary vault notes using identity, hashes, normalized content, titles, aliases, stable record identifiers, entities, and full text
- Exact-match automatic adoption, review-first strong ordinary-note adoption, ambiguous-match holds, and in-place updates after adoption

## Knowledge processing

- Deterministic text extraction before LLM use
- Optional OCR for common image formats
- Prompt-injection boundary: source content is explicitly marked as untrusted data
- Ollama native API adapter
- LM Studio and generic OpenAI-compatible API adapter
- Strict structured response schema and normalization
- Rules-only fallback so model downtime never stops synchronization
- Review threshold based on classification confidence
- Immediate server-pushed, read-only visibility into the active AI file, processing stages, model-emitted reasoning/output, and final decision
- Independent follow-latest mode for every inference panel, with manual-scroll takeover and an explicit return-to-live control
- Independent inference cancellation that leaves Global Sync and other folders running
- Feedback-driven AI re-review with one-run reviewer instructions
- Immutable Full document transfer and Brief summary AI profiles
- Copyable, editable, activatable, and deletable custom AI profiles
- Visible profile prompts, prompt template, provider parameters, context limits, and output behavior
- Per-profile Obsidian controls for vault context, `[[wikilinks]]`, tags, properties, folders, and source details
- Persistent whole-vault indexing of full note content, headings, aliases, properties, tags, folders, links, backlinks, named entities, stable identifiers, hashes, and modification times
- Persistent heading-aware graph chunks, canonical nodes, exact entity mentions, typed factual edges, quote/offset/hash provenance, temporal state, entity frequency, and folder hierarchy
- Two-pass Maintenance analysis: conservative factual extraction first, supported-edge link selection second
- Incremental semantic reuse for unchanged note hashes and hybrid graph-plus-corpus candidate retrieval
- Whole-vault relevance ranking with bounded full-content context and exact path-qualified link validation
- Adaptive per-vault Local AI organization models with no compiled business taxonomy or folder convention
- Corpus-adaptive candidate retrieval separated from relationship decisions, so similarity alone never creates a link
- Evidence-gated links requiring a precomputed edge ID, canonical endpoints, allowed predicate, exact existing source phrase, grounded source/target facts, configurable confidence, and a default safety ceiling of three
- Read-only Index Sweeps plus live Maintenance Sweep inference with streamed provider reasoning/output, processing stages, validated decisions, errors, current-note context, and independent follow-latest controls
- Context-grounded anchor validation that rejects dates, numbers, metadata labels, generic words, protected Markdown, duplicate existing links, and source sentences that do not support the target
- Current-Markdown metadata rebuilding, human-tag-only learning, exact duplicate detection, review-only folder/index recommendations, and per-operation approval

## Obsidian output

Each document receives:

- Stable `obsync_id`
- Source machine, watched root, relative source path, hash, type, and update properties
- Human-readable title
- Full extracted content, a combined summary and full body, or a brief summary according to the active profile
- Optional category, tags, related-note wikilinks, YAML properties, and source details according to the profile
- A manual section that Obsync preserves across updates

Approved maintenance uses native Obsidian Markdown only: exact contextual body phrases become inline path-qualified `[[wikilinks]]`, and independently evidenced tags join the existing YAML frontmatter. It adds no visible Obsync maintenance section. Legacy block-only tags are migrated into YAML before marked-block cleanup can be applied. Exact duplicates, folder placement, and index membership remain explicit review operations and never auto-delete or auto-merge notes. Operation ownership is stored in SQLite so later source sync can rebase only edits that still exist; a human-removed link or tag is not resurrected.

New notes reuse a strongly related existing vault folder when the index supplies a validated high-confidence context; otherwise they use the configured destination/device/root/category fallback. Their destination remains stable on later edits, preventing routine reclassification from breaking backlinks.

Ordinary existing notes may be adopted after an exact match or explicit review. Adoption stores the latest complete source in the managed region and preserves the entire pre-adoption note below it. Future versions of the same source update that one note rather than creating another entry.

## Central control panel

- Responsive minimal interface
- Automatic system/light/dark theme with manual toggle
- Automatic local temporary Admin with immediate account-security prompt
- Persistent unsecured-account warning and remote setup lockout until registration
- First-run username/password registration with safe v0.1.0 token migration
- Expiring browser sessions, sign-out, CSRF protection, and login throttling
- Dashboard counts, current active-file panel, and bounded event stream
- Device and watched-root status
- Native **Add folder**, per-folder **Scan**, **View files**, and **Sync changes** controls
- Global **Start Global Sync / Stop Global Sync** control with active sync and AI cancellation
- Independent **Start / Pause / Stop** controls for every watched folder
- Change-aware live status updates that preserve page and panel scroll positions; Local AI activity is pushed immediately rather than waiting for the general refresh interval
- Per-folder **Remove** action that preserves originals and existing notes
- Green/orange/red comparison indicators and aggregate folder counts
- One-time device enrollment codes
- Remote scan command
- Searchable, compact document table with bounded panel scrolling and responsive mobile cards
- Error retry commands
- Complete review workflow with Approve, Disregard, feedback-driven Redo AI review, and explicit separate-note creation for possible duplicates
- Combined Review queue for document decisions and native whole-vault edit recommendations, with exact anchor sentences, operation counts, per-operation evidence/confidence, before/after diffs, selective operation approval, and explicit duplicate/organization decisions
- Manual and scheduled read-only Index and native Maintenance Sweeps with live progress and model inference, safe Stop, daily/weekly/monthly/custom timing, review/automatic modes, audit history, concurrent-edit protection, and Undo Sweep
- Complete Local AI connection, profile, prompt, parameter, and Obsidian behavior configuration
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
- Non-Obsync note collisions are never overwritten unless an exact match is safely adopted or a person explicitly approves adoption
- Writes use a temporary sibling file followed by an atomic replace
- Manual note content below the generated boundary is preserved
- Original ordinary-note content is preserved during first-time adoption
- Maintenance changes are hash-checked, atomically written, owned per native operation, fully logged with before/after content, and reversible while current
- Sweep automation never permanently deletes or automatically merges notes
- Source deletion is represented as status, never propagated as note deletion
- Stopping work never disconnects computers, deletes source files, or deletes existing notes
- Stopping one AI inference moves only that file to Review and leaves Global Sync running
- Disregarded documents create no new note, never delete an existing note, and stay ignored until an explicit re-review
- Removing a watched folder deletes only Obsync's connection and ledger for that root
- API secrets are never returned to the UI after storage
- Administrator passwords and browser session credentials are never stored in plaintext

## Deliberate non-goals for the first release

- Two-way edits from Obsidian back into source documents
- Automatic source file moves or renames
- Cloud-hosted accounts or billing
- Embedding/vector search as a hard dependency
- Replacing Obsidian Sync for vault-to-vault replication
