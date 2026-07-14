# Getting started

## 1. Prepare the central server

Requirements:

- Docker Engine with Compose v2
- A writable Obsidian vault path on the server host
- A private network, VPN, or HTTPS reverse proxy if agents connect across machines

Clone the repository, create `.env`, and set at least:

```dotenv
OBSYNC_ADMIN_TOKEN=a-long-random-secret
OBSYNC_VAULT_HOST_PATH=/absolute/path/to/MyVault
```

Start and verify:

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:7769/api/v1/health
```

Open the UI, enter the admin token, and verify that **System → Obsidian vault** says **Ready**.

### Windows Docker host

Docker Desktop accepts Windows paths in Compose environment variables. In PowerShell:

```powershell
$env:OBSYNC_VAULT_HOST_PATH = "C:\Users\me\Documents\My Vault"
docker compose up -d --build
```

Keep the vault on a local filesystem whenever possible. If it is itself on a synced/cloud folder, test carefully for filesystem locking behavior.

## 2. Configure a local model (optional)

The model endpoint must be reachable from the server container.

- Model on the Docker host: try `host.docker.internal`
- Model on another LAN/VPN computer: use that private hostname or IP
- Ollama default port: `11434`
- LM Studio default port: `1234`

In **Settings**, choose the provider, base URL, and loaded model. Press **Test connection**. Do not expose an unauthenticated LLM port to the public Internet.

## 3. Pair a source computer

Create a code under **Sources → Add device**. Install the agent from a release executable or Python package, then pair:

```bash
obsync agent pair --server https://your-server --code XXXX-XXXX-XXXX --name "Laptop"
```

The pairing stores a device-specific token in the user's Obsync config directory with owner-only permissions on Unix.

## 4. Add folders

```bash
obsync agent add-folder "/home/me/Documents" --name "Documents"
obsync agent add-folder "/mnt/team-share/Projects" --name "Team Projects" --destination "Company Knowledge"
obsync agent list
obsync agent scan
```

After the first successful scan, run continuously:

```bash
obsync agent run
```

## 5. Run the agent in the background

### Windows

Use the standalone `obsync-agent-windows-x64.exe` release. Start with a normal foreground scan first. For automatic startup, create a Task Scheduler task that runs:

```text
obsync-agent-windows-x64.exe agent run
```

Run it as the Windows user who can read every watched folder and network share. A Windows service running as Local System often cannot see user-mapped network drives; use UNC paths or a service account instead.

### Linux systemd user service

Create `~/.config/systemd/user/obsync-agent.service`:

```ini
[Unit]
Description=Obsync folder agent
After=network-online.target

[Service]
ExecStart=%h/.local/bin/obsync agent run
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now obsync-agent
```

### Docker agent

Use `docker-compose.agent.yml` for NAS/Linux hosts. Mount each source read-only and create the paired agent config in the mounted config directory before starting the long-running container.

## 6. Review first results

Start with a test folder and vault backup. Confirm:

- Generated categories and tags make sense
- The chosen destination hierarchy fits the vault
- LLM confidence is calibrated appropriately
- Editing below **My notes** survives a source update
- Removing a source marks the note missing without deleting it

