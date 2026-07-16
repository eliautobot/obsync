# Obsync project record

## Purpose

Obsync is a self-hosted ingestion layer that keeps an Obsidian vault aligned with external folders. Lightweight agents watch folders on Windows, Linux, macOS, NAS hosts, and mounted network shares. The central server turns supported source files into organized Markdown rather than mirroring them byte for byte.

## Repository

- Source: <https://github.com/eliautobot/obsync>
- License: MIT
- Initial release: `v0.1.0`
- Current development version: `v0.11.0`

## Product decisions

- Source files are read-only. Obsync never moves, edits, or deletes them.
- The central server owns the ledger; exactly one active server mount or paired desktop performs managed vault writes.
- Agents connect outbound, so watched computers do not require inbound ports.
- Generated sections can be safely replaced; text below **My notes** is preserved.
- Missing sources are marked, not deleted.
- Non-Obsync destination collisions stop processing instead of overwriting a note.
- Local AI is optional. Ollama, LM Studio, and OpenAI-compatible endpoints are supported, with deterministic rules as the offline fallback.
- One server coordinates any number of paired devices and watched folders.
- Human administrators use username/password login; agents keep separate non-interactive device tokens.

## Authentication update

Version 0.2.0 replaces the long administrator token login with first-run username/password setup. It uses scrypt password hashing, expiring HttpOnly sessions, CSRF protection, login throttling, a recovery command, and a one-time migration that disables the old token after account creation.

Version 0.3.0 simplifies first use: a fresh local installation opens as temporary passwordless **Admin**, immediately offers username/password registration, and retains a visible warning if setup is deferred. Temporary Admin is restricted to a loopback URL on the server (or an explicitly trusted setup IP), while remote clients remain locked out until registration.

Version 0.4.0 adds a desktop-vault mode for vaults located in Windows Documents or on another computer, native folder selection, a corrected standalone Windows pairing flow, quick non-inference model checks, an automatic server-computer card, and administrator account settings.

Version 0.5.0 adds central **Add folder** controls, inventory-only scans, visible green/orange/red source-to-vault comparison states, separate sync actions, desktop vault audits, existing-note adoption, and dynamic watching for folders added while an agent is running.

Version 0.6.0 adds a guided Windows Companion that installs per-user, runs silently, and starts automatically at sign-in; contextual `?` explanations; a complete in-app Help center; sharp top-layer notifications above dialogs; and first-party release notes.

Version 0.7.0 adds safe computer disconnection, transactionally idempotent pairing, duplicate-window prevention, saved-pairing repair, verified Windows automatic startup, one-click setup-detail transfer, and an app-served matching Windows Companion download.

Version 0.8.0 turns the Windows component into Obsync Desktop with bundled watcher controls, adds global start/stop with active AI cancellation, and adds safe watched-folder removal that preserves originals and notes.

Version 0.9.0 separates vault, Local AI, and application settings; requires explicit vault confirmation; adds conservative duplicate review, custom AI instructions, per-folder controls, live UI updates, immediate reconciliation, one-click Desktop launching, and clear Windows administrator setup guidance.

Version 0.10.0 adds live active-file and model-inference visibility, per-inference cancellation, complete review resolutions with feedback-driven AI re-review, scroll-stable live refreshes, and compact responsive document panels that remain bounded under large inventories.

Version 0.11.0 replaces tick-based Local AI repainting with authenticated server-sent activity events, adds independent smart follow mode and return-to-live controls for every inference panel, and closes browser and server stream resources deterministically across reload, sign-out, and disconnect cycles.

## Initial validation

The initial release was validated with automated unit and integration tests, Linux and Windows agents, live Ollama and LM Studio inference, Docker health checks, and a responsive browser UI pass in light and dark modes. GitHub Actions repeats testing on Linux and Windows and builds the Docker image.

## Related documents

- [[Obsync - Features]]
- [[Obsync - Architecture]]
- [[Obsync - Getting Started]]
- [[Obsync - Multi-PC]]
- [[Obsync - Supported Files]]
- [[Obsync - Security]]
