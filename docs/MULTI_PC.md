# Multi-PC and network-share design

## One server, many sources

Run one central Obsync server. Its control-plane card appears automatically as the first computer. Add a lightweight agent to every desktop whose source folders or vault are outside the server process/container. A Windows host still needs a desktop agent when the server is inside Docker Desktop or a VM.

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
2. On Windows, download the Companion from Obsync, copy all setup details, paste them into the Companion, and click **Connect and install**. On other systems, pair the agent once from the CLI.
3. Choose **Add folder** on that computer's card and select a local directory.
4. Review the inventory comparison, then choose **Sync changes**.
5. The Windows Companion starts automatically at sign-in; configure the CLI agent to start at login/boot on other systems.

The central UI shows the device, its roots, last heartbeat, file counts, and comparison states. **Scan** compares without writing; **Sync changes** processes the pending red/orange items. Commands are received by the desktop agent within approximately 30 seconds.

Use **Disconnect** to retire a stale computer. Its credential, server-side watched-folder ledger, and pending commands are removed. Original files and existing Obsidian notes remain untouched. If it is the active desktop vault writer, choose another vault first.

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
2. **Vault on a desktop:** pair the desktop, select the vault in the Windows Companion (or run `obsync agent set-vault --browse` for a CLI agent), and select that computer in **Settings**. The native agent writes only validated Obsync-managed notes into that local vault. This is the recommended mode when the vault is in Windows Documents and the server runs elsewhere.

Only one vault writer is active. The server remains the authoritative ledger and queues idempotent write/status commands when a desktop is selected. If that desktop is offline, documents remain pending and complete after the agent reconnects.

The Obsidian app does not need to be open, but the selected desktop agent must be running for remote writes. Keep the generated-note location backed up and avoid pointing multiple Obsync servers at the same output tree.
