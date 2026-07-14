# Security model

## Authentication

- First launch creates one administrator username/password account.
- Passwords are salted and hashed with scrypt; plaintext passwords are not stored.
- Browser sessions use random, expiring HttpOnly + SameSite cookies.
- State-changing admin requests require a separate CSRF value.
- Failed logins are rate limited. "Keep me signed in" sessions expire after 30 days; normal sessions expire after 12 hours by default.
- Devices enroll with short-lived, single-use codes.
- Each device receives a distinct long random token.
- The database stores SHA-256 token digests, not raw device tokens.
- Device tokens can access only agent endpoints; root ownership is checked server-side.

## Network exposure

The Compose default binds the selected port on all interfaces to support multi-PC use. Run Obsync on a private LAN/VPN or behind an HTTPS reverse proxy. Set `OBSYNC_BIND_IP=127.0.0.1` when remote agents are not needed.

Do not publish Ollama or LM Studio directly to the Internet. The Obsync server should reach them over the host bridge, LAN, or VPN.

## Filesystem safety

- Source files are opened read-only by agents.
- Symlinked source files are skipped.
- Upload paths must be relative and cannot contain `..`.
- Destination paths are resolved and verified to remain below the vault mount.
- UUID suffixes prevent common filename collisions.
- Existing files without Obsync ownership markers are not overwritten.
- Atomic sibling writes reduce partial-file risk.
- Missing sources do not delete generated notes.

Mount source directories read-only when running an agent in Docker.

## LLM safety and privacy

Extracted document content is sent to the configured model endpoint. With a local Ollama/LM Studio deployment, content stays within the networks and machines you control. A generic OpenAI-compatible endpoint may be remote; its privacy policy then applies.

The system prompt labels source content as untrusted and tells the model not to follow embedded instructions. Model output is parsed as a strict object, normalized, length-limited, and constrained. Related links must exactly match server-provided candidates. All model-provided path components are slugged before filesystem use.

LLMs are not security boundaries. Keep the vault backed up and review generated notes before relying on them for high-stakes decisions.

## Secrets

- Prefer interactive browser setup so no plaintext password remains in deployment configuration.
- Optional `OBSYNC_ADMIN_USERNAME` and `OBSYNC_ADMIN_PASSWORD` values are for unattended first boot only; remove them after the account is created.
- Set `OBSYNC_SECURE_COOKIES=true` when the UI is served exclusively over HTTPS.
- Pre-0.2 `OBSYNC_ADMIN_TOKEN` values are accepted only until the one-time username/password migration completes.
- LLM API keys are stored in the server SQLite database and are never returned after configuration.
- Protect `/data` with host filesystem permissions and backups.
- Never commit `.env`, agent configuration, databases, or generated token files.

## Reverse proxy requirements

Use HTTPS, request-size limits compatible with `OBSYNC_MAX_UPLOAD_MB`, sensible timeouts for LLM-backed requests, and additional edge rate limiting on enrollment and authentication paths. Forwarded headers are trusted only from addresses configured with `OBSYNC_FORWARDED_ALLOW_IPS`.

## Reporting vulnerabilities

Please open a private GitHub security advisory rather than a public issue when the repository's Security tab is available.
