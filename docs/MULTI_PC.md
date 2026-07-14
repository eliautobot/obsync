# Multi-PC and network-share design

## One server, many sources

Run one central Obsync server. It appears automatically as the first computer. Add a lightweight agent only to computers that contain additional source folders or the Obsidian vault.

```text
Home desktop ──────┐
Office Windows PC ─┼── outbound HTTPS/Tailscale ── Obsync server ─┬─ mounted vault
Linux workstation ─┤
NAS folders ───────┘                                         └─ or selected desktop vault writer
```

Because agents connect outward, the source PCs need no inbound port forwarding. The server needs one reachable HTTP(S) endpoint.

## Recommended networking

In order of preference:

1. Private mesh VPN such as Tailscale or ZeroTier
2. Private LAN with firewall rules limiting server access
3. HTTPS reverse proxy with a trusted certificate and rate limiting

Do not expose plain HTTP or an unauthenticated local-LLM endpoint to the public Internet.

## Remote PC workflow

For each PC:

1. Create an expiring enrollment code in the central UI.
2. Pair the agent once.
3. Add one or more local folders.
4. Run a one-time scan.
5. Configure the agent to start at login/boot.

The central UI shows the device, its roots, last heartbeat, document count, errors, and review state. **Scan now** places a command in the device queue; the agent receives it within approximately 30 seconds.

## Network folders

Install the agent on a machine that already mounts the share reliably.

- Windows: prefer a UNC path such as `\\server\share\Projects` for services and scheduled tasks.
- Linux: mount SMB/NFS first, then watch the mount path.
- NAS: use the Docker agent with the share mounted read-only into the container.

The account running the agent needs read permission only. The server never attempts to write back.

## Offline behavior

The agent does not mark a successful local state until the central server accepts the upload. If either side is offline, the next reconciliation retries changed files. A full rescan also detects updates that occurred while the agent was stopped.

## Vault placement

Obsync supports two explicit modes:

1. **Server-mounted vault:** Docker receives a host folder as `/vault`. This is simplest when the vault is on the server or a reliably mounted share. Docker mounts are established at container startup, so a web page cannot browse arbitrary Windows host folders later.
2. **Vault on a desktop:** pair the desktop, run `obsync agent set-vault --browse`, and select it in **Settings**. The native agent writes only validated Obsync-managed notes into that local vault. This is the recommended mode when the vault is in Windows Documents and the server runs elsewhere.

Only one vault writer is active. The server remains the authoritative ledger and queues idempotent write/status commands when a desktop is selected. If that desktop is offline, documents remain pending and complete after the agent reconnects.

The Obsidian app does not need to be open, but the selected desktop agent must be running for remote writes. Keep the generated-note location backed up and avoid pointing multiple Obsync servers at the same output tree.
