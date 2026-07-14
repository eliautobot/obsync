# Obsync project record

## Purpose

Obsync is a self-hosted ingestion layer that keeps an Obsidian vault aligned with external folders. Lightweight agents watch folders on Windows, Linux, macOS, NAS hosts, and mounted network shares. The central server turns supported source files into organized Markdown rather than mirroring them byte for byte.

## Repository

- Source: <https://github.com/eliautobot/obsync>
- License: MIT
- Initial release: `v0.1.0`
- Current release: `v0.2.0`

## Product decisions

- Source files are read-only. Obsync never moves, edits, or deletes them.
- Only the central server writes generated notes into the vault.
- Agents connect outbound, so watched computers do not require inbound ports.
- Generated sections can be safely replaced; text below **My notes** is preserved.
- Missing sources are marked, not deleted.
- Non-Obsync destination collisions stop processing instead of overwriting a note.
- Local AI is optional. Ollama, LM Studio, and OpenAI-compatible endpoints are supported, with deterministic rules as the offline fallback.
- One server coordinates any number of paired devices and watched folders.
- Human administrators use username/password login; agents keep separate non-interactive device tokens.

## Authentication update

Version 0.2.0 replaces the long administrator token login with first-run username/password setup. It uses scrypt password hashing, expiring HttpOnly sessions, CSRF protection, login throttling, a recovery command, and a one-time migration that disables the old token after account creation.

## Initial validation

The initial release was validated with automated unit and integration tests, Linux and Windows agents, live Ollama and LM Studio inference, Docker health checks, and a responsive browser UI pass in light and dark modes. GitHub Actions repeats testing on Linux and Windows and builds the Docker image.

## Related documents

- [[Obsync - Features]]
- [[Obsync - Architecture]]
- [[Obsync - Getting Started]]
- [[Obsync - Multi-PC]]
- [[Obsync - Supported Files]]
- [[Obsync - Security]]
