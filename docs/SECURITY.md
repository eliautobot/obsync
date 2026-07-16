# Security model

## Authentication

- A fresh server grants temporary passwordless `Admin` access only through a loopback URL on the server host, or from an address explicitly listed in `OBSYNC_LOCAL_SETUP_IPS`.
- Temporary Admin opens a security prompt immediately and displays a persistent warning until the account is secured.
- Remote clients receive a setup-required screen and cannot use temporary Admin.
- Registering a username/password permanently disables temporary Admin and any pre-0.2 administrator token.
- Passwords are salted and hashed with scrypt; plaintext passwords are not stored.
- Browser sessions use random, expiring HttpOnly + SameSite cookies.
- State-changing admin requests require a separate CSRF value.
- Failed logins are rate limited. "Keep me signed in" sessions expire after 30 days; normal sessions expire after 12 hours by default.
- Devices enroll with short-lived, single-use codes.
- Each device receives a distinct long random token.
- The database stores SHA-256 token digests, not raw device tokens.
- Device tokens can access only agent endpoints; root ownership is checked server-side. Disconnecting a computer revokes its token immediately.
- Obsync Desktop uses one-time elevation to register automatic startup on Windows installations that restrict Task Scheduler. It still installs per-user in Local AppData, creates a limited current-user `ONLOGON` task, and does not install a system service.
- Global Stop cancels queued and active processing before further vault writes; connected desktops remain authenticated only for heartbeat and safe configuration/removal operations until Start.
- Folder removal deletes only Obsync's server/desktop tracking records for that root. It never deletes the underlying source directory, source files, or existing notes.

## Network exposure

The Compose default binds the selected port on all interfaces to support multi-PC use. Run Obsync on a private LAN/VPN or behind an HTTPS reverse proxy. Set `OBSYNC_BIND_IP=127.0.0.1` when remote agents are not needed.

Temporary Admin requires both a local client path and a loopback request target such as `http://localhost:7769`. This prevents a same-host reverse proxy with a public hostname from inheriting passwordless setup access. State-changing temporary-Admin requests are restricted to same-origin browser traffic. `OBSYNC_LOCAL_SETUP_IPS` is an explicit override for headless installations; trust only a specific management IP and remove it immediately after registration.

Do not publish Ollama or LM Studio directly to the Internet. The Obsync server should reach them over the host bridge, LAN, or VPN.

## Filesystem safety

- Source files are opened read-only by agents.
- Symlinked source files are skipped.
- Upload paths must be relative and cannot contain `..`.
- Destination paths are resolved and verified to remain below the mounted or selected desktop vault.
- UUID suffixes prevent common filename collisions.
- Existing files without Obsync ownership markers are not overwritten.
- Atomic sibling writes reduce partial-file risk.
- Missing sources do not delete generated notes.
- Disconnecting a computer removes only the server-side device ledger. Source files and existing Obsidian notes are retained.
- Desktop vault writers accept only server-authenticated commands, use atomic writes, and refuse non-Obsync collisions.

Mount source directories read-only when running an agent in Docker.

## LLM safety and privacy

Extracted document content is sent to the configured model endpoint. With a local Ollama/LM Studio deployment, content stays within the networks and machines you control. A generic OpenAI-compatible endpoint may be remote; its privacy policy then applies.

The system prompt labels source content as untrusted and tells the model not to follow embedded instructions. Model output is parsed as a strict object, normalized, length-limited, and constrained. Related links must exactly match server-provided candidates. All model-provided path components are slugged before filesystem use.

The Local AI page can display provider-emitted reasoning and streamed output for the active and most recent inference. These bounded traces stay in server memory and are not persisted as a chat transcript. Reviewer feedback for an explicit re-review is stored with that document for auditability and is sent to the configured model endpoint for that run.

LLMs are not security boundaries. Keep the vault backed up and review generated notes before relying on them for high-stakes decisions.

## Secrets

- Prefer local interactive browser setup so no plaintext password remains in deployment configuration.
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
