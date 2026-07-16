# Changelog

All notable changes to Obsync will be documented here.

## 0.14.0 - 2026-07-16

### Added

- Added an adaptive per-vault organization model learned from indexed note content, structure, folders, properties, and human review outcomes without built-in business categories
- Added a separate Local AI relationship-adjudication pass requiring an exact candidate target, a specific relationship, source-side evidence, target-side evidence, grounded facts, and configurable confidence
- Added visible adaptive-model status, learned vault patterns, candidate limits, confidence controls, evidence-backed link ceilings, and relationship details in Review

### Changed

- Whole-vault retrieval now uses corpus-adaptive relevance only to shortlist candidates; similarity can never create a link by itself
- Document processing now uses AI-selected existing folders and evidence-backed AI relationships instead of appending deterministic score-based links
- Maintenance Sweeps now require a configured Local AI model and learn or refresh the vault model when content, the active AI profile, model, or review feedback changes
- Relationship feedback from applied and rejected recommendations is included in later model learning
- The default link safety ceiling is 20 and the supported ceiling is 50; it is a maximum, not a target

### Fixed

- Prevented invoices, templates, and other same-type notes from being linked merely because they share document vocabulary, tags, folders, or formats
- Excluded generated maintenance blocks from future tags, links, entities, retrieval, model learning, fingerprints, prompts, and evidence validation
- Added a safe recalculation path that can remove previously overlinked generated blocks while preserving all user-authored content
- Superseded unreviewed v0.13 deterministic recommendations during migration so obsolete suggestions cannot be applied after upgrade
- Preserved existing generated blocks when Local AI fails instead of treating an inference error as an empty relationship decision

### Validation

- Added adversarial invoice-corpus tests, hallucinated-target and ungrounded-evidence rejection tests, novel-vault taxonomy tests, migration/repair tests, Desktop round-trip coverage, and a 5,000-note adaptive-index stress test

## 0.13.1 - 2026-07-16

### Fixed

- Normalized YAML-native dates, datetimes, nested collections, non-finite numbers, binary values, and other uncommon frontmatter scalars before Desktop vault notes cross the JSON API boundary
- Prevented real-world Obsidian date properties from stopping Index and Maintenance Sweeps at the first Desktop upload batch
- Kept the latest sweep error visible beside its controls and clarified the difference between the migrated vault cache and the last successful Index Sweep

## 0.13.0 - 2026-07-16

### Added

- Added a persistent whole-vault knowledge index containing full Markdown content, paths, folders, titles, aliases, headings, properties, YAML/inline tags, links, backlinks, named entities, stable record identifiers, hashes, and modification times
- Added layered existing-note detection using Obsync identity, source hashes, normalized content, titles, aliases, identifiers, entities, and relevance evidence
- Added safe first-time adoption of exact or approved ordinary notes, with complete original-content preservation and future in-place source updates
- Added entity-aware, path-qualified linking to every materially relevant validated note, with configurable score and limits up to 250
- Added manual and scheduled Index and Maintenance Sweeps with daily, weekly, monthly, and custom-interval timing
- Added live sweep progress, cooperative Stop, no-overlap enforcement, review/automatic change modes, evidence, confidence, before/after diffs, bulk review, audit history, and Undo Sweep
- Added optimistic hash checks that reject maintenance writes when a note changed after the recommendation was created

### Changed

- New notes can reuse a high-confidence related existing folder instead of always being forced under the watched-root destination tree
- Full document transfer now considers up to 200 ranked vault candidates and supports up to 100 validated related links by default
- Vault context sent to the model now includes bounded content excerpts, headings, entities, paths, tags, and relationship evidence instead of metadata-only titles
- Ordinary source reconciliation reuses the persistent index after bootstrap, so expensive whole-vault reads are controlled by explicit/scheduled sweeps
- Review now combines source-document decisions with whole-vault maintenance recommendations

### Fixed

- Completed empty-vault rebuilds now clear stale indexed notes and record a visible completion time instead of leaving the previous index in place
- Bundled IANA timezone data so scheduled sweeps accept named timezones consistently on Windows

### Safety

- Sweeps skip symlinks and `.obsidian`, never automatically delete or merge notes, preserve source files, and write atomically
- Automatic change mode requires an explicit setting and shows a warning; Review remains the maintenance default and Index-only remains the index default
- Desktop-vault sweeps and writes remain outbound-only authenticated operations and honor server stop requests

## 0.11.0 - 2026-07-15

### Added

- Added authenticated server-sent events for immediate Local AI activity updates without waiting for the general dashboard refresh interval
- Added independent follow-latest behavior to every active document inference panel
- Added a per-panel down-arrow control that appears when the user scrolls away and restores live following when selected

### Changed

- Updated only the changing Local AI session metadata and trace rows instead of rebuilding the entire Local AI panel for each model update
- Made Local AI cards and traces fit narrow/mobile viewports without horizontal page overflow

### Fixed

- Stopped live inference from overriding a user's manual trace position while new tokens continue arriving
- Closed browser EventSource connections, auto-scroll timers, and server subscriber queues during sign-out, page unload, reconnect, and cancelled gateway streams

## 0.10.0 - 2026-07-15

### Added

- Added the current active source file and processing stage to Overview
- Added a read-only live Local AI session showing the active file, provider, model, elapsed time, processing stages, streamed model reasoning/output, and final decision
- Added an independent **Stop inference** control that cancels only the selected Local AI request, keeps Global Sync running, and moves the file to Review
- Added complete Review actions: **Approve**, **Disregard**, and **Redo AI review** with optional one-run reviewer feedback; possible duplicates retain **Create separate note**

### Changed

- Replaced unconditional live-page rerenders with change-aware refreshes that preserve page and panel scroll positions
- Made document, review, event, active-work, inventory, and AI-trace lists compact, bounded, and independently scrollable
- Reflowed desktop document tables into mobile cards without horizontal page scrolling
- Streamed Ollama and OpenAI-compatible model responses so supported model activity appears while inference is running

### Fixed

- Forced re-review commands now process unchanged files and carry reviewer feedback to the model
- Disregarded documents remain ignored during later scans until the user explicitly requests another AI review

## 0.9.0 - 2026-07-15

### Added

- Added dedicated **Obsidian Vault** and **Local AI** pages with explicit vault confirmation and protected custom AI instructions
- Added conservative existing-note title matching, possible-duplicate review, per-folder Start/Pause/Stop controls, and automatic live page updates

### Changed

- Focused Settings on server/Desktop operation and clarified one-time Windows administrator setup
- Renamed global controls to **Start Global Sync / Stop Global Sync** and reconciled missed work immediately after restart

## 0.8.0 - 2026-07-15

### Added

- Replaced the separate Companion product experience with **Obsync Desktop**, a single Windows app that bundles pairing, background folder watching, automatic startup, local start/stop controls, and a shortcut back to the Obsync dashboard
- Added a global **Start syncing / Stop syncing** control that pauses connected watchers, cancels active synchronization and AI classification, cancels queued processing commands, and reconciles missed changes after restart
- Added per-folder **Remove** controls that forget a watched folder on both the server and desktop without deleting original files or existing Obsidian notes
- Added cancellation-safe processing states, desktop heartbeat propagation, late-command protection, and automated active-AI cancellation coverage

### Changed

- Renamed the Windows release artifact and in-app download to `obsync-desktop-windows-x64.exe`
- Migrated automatic startup from the legacy `Obsync Companion` task to `Obsync Desktop` while preserving pairing, watched folders, and vault configuration
- Moved extraction off the server event loop so Stop remains responsive during document processing

## 0.7.0 - 2026-07-14

### Added

- Added a safe **Disconnect** action for paired computers that revokes access and removes stale device, folder, document, and command records without touching source files or Obsidian notes
- Added one-click copying and pasting of all Windows pairing details
- Bundled the matching Windows Companion inside published Docker images so the app serves its own desktop download
- Added idempotent device registration and concurrent pairing stress coverage

### Fixed

- Prevented two Companion setup windows from consuming the same one-time pairing code
- Reused valid saved pairings when reopening the Companion or repairing automatic startup
- Replaced raw `400 Bad Request` setup errors with concise, actionable messages
- Verified the Windows automatic-start task after installation and start the background agent through the tracked task
- Stopped disconnected agents cleanly after the server revokes their credentials
- Rejected absolute source paths consistently on Windows and Unix systems

### Changed

- Clarified why Docker installations need a one-time native desktop bridge for Windows folders
- Added explicit PowerShell update commands and corrected Windows health-check guidance

## 0.6.0 - 2026-07-14

### Added

- Added a standalone Windows Companion setup app with a simple pairing window, per-user installation, silent background operation, and automatic startup at Windows sign-in
- Added an in-app Help center with a five-step setup guide, page explanations, status definitions, local-AI guidance, safety details, and troubleshooting
- Added visible `?` explanations throughout authentication, overview, sources, documents, review, settings, account, folder, and computer-pairing controls
- Added a dedicated Windows Companion release artifact alongside the existing Windows and Linux command-line agents

### Changed

- Replaced the Windows PowerShell onboarding flow with a download-and-open Companion workflow; no terminal remains open and Administrator access is not required
- Moved toast notifications into the browser top layer and removed backdrop blur so notices remain sharp above open dialogs
- Added first-party versioned release notes to the GitHub release workflow

## 0.5.0 - 2026-07-14

### Added

- Added **Add folder** to every connected desktop card with a remotely requested native directory picker
- Added inventory-only scans that hash and compare every source with its managed Obsidian note before writing
- Added green **In Obsidian**, orange **Modified**, and red **New/Missing** file states with per-folder counts and file inspection
- Added separate **Scan** and **Sync changes** controls for each watched folder
- Added local and desktop-vault audits that detect missing, stale, or overwritten managed notes
- Added existing-note adoption by source computer, watched root, and relative path to prevent duplicate notes after a ledger rebuild
- Added dynamic live watching for folders selected after the desktop agent has already started

### Changed

- Pairing a computer now connects it first; folders are added afterward from the central Sources page
- Clarified that the Overview computer count includes the central server while vault/folder selectors list paired desktops
- Periodic reconciliation now inventories, compares, and then syncs only pending changes

## 0.4.0 - 2026-07-14

### Added

- Added two vault modes: a Docker/server-mounted vault or a vault written by a paired desktop agent
- Added a native folder picker for Windows, Linux, and macOS desktop agents
- Added remotely requested vault selection from the web settings page
- Added an automatic always-connected server computer card to the Sources page
- Added complete Windows pairing commands that download and invoke the standalone agent executable
- Added account settings for changing the administrator username and password
- Added an account menu with explicit settings and sign-out actions
- Added a complete server and desktop-agent update, backup, verification, and rollback guide

### Changed

- Replaced inference-based model testing with quick model-list discovery for Ollama, LM Studio, and OpenAI-compatible endpoints
- Capped model connection checks at 15 seconds and report discovered/model-mismatch details
- Clarified that additional agents are optional when all source folders and the vault are available to the server
- Preserved the server's processing ledger while allowing exactly one selected desktop agent to perform safe managed-note writes

## 0.3.0 - 2026-07-14

### Changed

- Fresh local installations now open as a temporary passwordless `Admin`
- Added an immediate account-security prompt with a **Continue for now** option
- Added a persistent warning banner until a username and password are registered
- Limited temporary Admin access to a local loopback URL or an explicitly trusted setup IP
- Blocked remote, cross-site, and forged-Host attempts from using temporary Admin access
- Preserved the v0.1.0 token-migration path for remote upgrades

## 0.2.0 - 2026-07-14

### Changed

- Replaced the administrator bearer-token login with first-run username/password setup
- Added scrypt password hashing, expiring HttpOnly sessions, CSRF protection, and login throttling
- Added a one-time v0.1.0 token migration and an interactive password-reset command

## 0.1.0 - 2026-07-14

### Added

- Central self-hosted FastAPI server and minimal responsive web control panel
- Automatic light, dark, and system theme support
- Cross-platform filesystem watch agent with periodic reconciliation
- Multiple devices and watched folders per server
- One-time device enrollment and separate hashed agent tokens
- Change, rename, and missing-source handling with SHA-256 idempotency
- Text, PDF, Word, Excel, PowerPoint, HTML, email, CSV, JSON, image OCR, source-code, and metadata extraction paths
- Ollama, LM Studio, and OpenAI-compatible local-LLM adapters
- Deterministic organization fallback and confidence-based review queue
- Safe Obsidian Markdown properties, tags, categories, summaries, and wikilinks
- Generated-content ownership markers and preserved manual notes
- Docker server and optional Docker agent definitions
- Linux and Windows CI, Docker smoke tests, and standalone agent release builds
