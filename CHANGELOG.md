# Changelog

All notable changes to Obsync will be documented here.

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
