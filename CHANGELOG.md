# Changelog

All notable changes to Obsync will be documented here.

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
