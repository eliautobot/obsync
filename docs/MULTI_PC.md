# Multi-PC and network-share design

## One server, many sources

Choose the computer or server that can write the Obsidian vault. Run the Obsync server there. Every other computer runs only the lightweight agent.

```text
Home desktop ──────┐
Office Windows PC ─┼── outbound HTTPS/Tailscale ── Obsync server ── Obsidian vault
Linux workstation ─┤
NAS folders ───────┘
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

## Vault on a different PC

The Obsidian app does not have to run on the source computers. It can run on the same PC as the server or on another PC that receives the vault through an existing vault-sync system. Obsync's responsibility ends after writing Markdown to the mounted server-side vault.

For the fewest conflicts, make the server's mounted vault the authoritative generated-note location and let Obsidian/Obsidian Sync handle normal vault availability to other personal devices.

