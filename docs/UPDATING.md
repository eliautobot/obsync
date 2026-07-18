# Updating Obsync

Obsync updates are designed to preserve the administrator account, paired computers, watched folders, settings, processing history, and Obsidian vault. The server data lives in the Docker volume mounted at `/data`; the vault is a separate host folder or paired desktop vault and is never replaced by an application update.

## Before updating

1. Read the [release notes](https://github.com/eliautobot/obsync/releases) for the version you are installing.
2. Confirm the current server version in the bottom-left corner of the Obsync interface or with:

   ```bash
   curl http://127.0.0.1:7769/api/v1/health
   ```

3. Back up the Obsidian vault using your normal backup method.
4. Back up the Obsync data volume. From the folder containing `docker-compose.yml`:

   ```bash
   mkdir -p backups
   DATA_VOLUME="$(docker inspect obsync --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')"
   docker compose stop obsync
   docker run --rm \
     -v "$DATA_VOLUME:/data:ro" \
     -v "$PWD/backups:/backup" \
     alpine sh -c 'tar -czf /backup/obsync-data-before-update.tar.gz -C /data .'
   docker compose start obsync
   ```

   Keep the backup until the new version has been verified. If the container has a custom name, replace `obsync` in the `docker inspect` command.

   On a Windows Docker Desktop host, use PowerShell:

   ```powershell
   New-Item -ItemType Directory -Force -Path .\backups | Out-Null
   $DataVolume = docker inspect obsync --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}'
   $BackupPath = (Resolve-Path .\backups).Path
   docker compose stop obsync
   docker run --rm `
     --mount "type=volume,source=$DataVolume,target=/data,readonly" `
     --mount "type=bind,source=$BackupPath,target=/backup" `
     alpine sh -c 'tar -czf /backup/obsync-data-before-update.tar.gz -C /data .'
   docker compose start obsync
   ```

## Update a server installed from this repository

This is the standard method for installations created with the repository's `docker-compose.yml`:

```bash
cd /path/to/obsync
git status --short
git pull --ff-only
docker compose build --pull
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:7769/api/v1/health
```

`git status --short` should return no output before the pull. If it lists local changes, preserve or review them before updating; do not force-reset an installation with unknown changes.

Keep the existing `.env` file. Do not copy `.env.example` over it during an update; the example file documents new options but does not contain the installation's vault path or other settings.

On a Windows Docker Desktop host, use PowerShell and first enter the exact folder containing both `.git` and `docker-compose.yml`:

```powershell
Set-Location "C:\Users\you\Documents\Obsync\obsync"
Test-Path .\.git
Test-Path .\docker-compose.yml
git status --short
git pull --ff-only
docker compose build --pull
docker compose up -d
docker compose ps
Invoke-RestMethod -Uri "http://127.0.0.1:7769/api/v1/health"
```

Both `Test-Path` commands must return `True`. If either is false, locate the Compose file with `Get-ChildItem "$HOME\Documents" -Filter docker-compose.yml -Recurse` and change into that folder. PowerShell does not use the Linux `\` character for line continuation.

The `docker compose up -d` command recreates the application container while keeping the named data volume and vault mount. Database migrations run automatically when the updated server starts.

To install a specific release instead of the newest `main` branch:

```bash
git fetch --tags
git checkout v0.18.0
docker compose build --pull
docker compose up -d
```

Replace `v0.18.0` with the desired release tag. A tag checkout is intentionally fixed to that release; check out `main` again before using `git pull` for later updates.

## Update a server using the published Docker image

Pull the desired tag, then recreate the container using the same environment, ports, data volume, and vault mount as the existing deployment:

```bash
docker pull ghcr.io/eliautobot/obsync:0.18.0
```

Replace `0.18.0` with the desired version. Prefer a numbered tag for predictable deployments. If your Compose file references the published image, the complete update is:

```bash
docker compose pull
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:7769/api/v1/health
```

Do not remove the `/data` volume or change the vault mount during recreation.

## Update desktop agents

The server and desktop agents should normally use the same release. Update the server first, then each paired computer. Pairing details, watched folders, and the selected desktop vault are stored outside the executable and remain in place.

### Obsync Desktop for Windows

1. In Obsync, open **Sources → Add another computer → Download Obsync Desktop**. Published containers serve the matching executable directly.
2. Right-click it, choose **Run as administrator**, and approve the one-time setup prompt.
3. Leave the pairing code blank when the existing server address is unchanged, then click **Connect and install**.
4. Obsync Desktop installs the new version, migrates the legacy Companion task when present, refreshes automatic startup, and starts its built-in watcher in the background.

The versioned executable lives under Local AppData, while pairing, vault, and watched-folder configuration live separately and are preserved. The installer verifies the new limited `Obsync Desktop` startup task, registers the `obsync://` app link, and removes the legacy `Obsync Companion` task. The background watcher does not remain elevated.

### Windows standalone command-line agent

1. Stop the running agent or its Task Scheduler task.
2. Download `obsync-agent-windows-x64.exe` from the matching [GitHub release](https://github.com/eliautobot/obsync/releases).
3. Replace the old executable with the new one at the same path.
4. Verify and restart it:

   ```powershell
   .\obsync-agent-windows-x64.exe --version
   .\obsync-agent-windows-x64.exe agent scan
   .\obsync-agent-windows-x64.exe agent run
   ```

If Task Scheduler starts the agent, start that task instead of leaving the final foreground command open.

### Python installation

Install the matching release tag:

```bash
python -m pip install --upgrade \
  "obsync-app @ git+https://github.com/eliautobot/obsync.git@v0.18.0"
obsync --version
obsync agent scan
```

### Docker agent

Pull or rebuild the same version as the server, then recreate only the agent container:

```bash
docker compose -f docker-compose.agent.yml build --pull
docker compose -f docker-compose.agent.yml up -d
docker compose -f docker-compose.agent.yml logs --tail=50 obsync-agent
```

## Verify the update

After updating:

- Confirm the expected version in the UI or health response.
- Confirm the server and agents show connected in **Sources**.
- Run one manual scan from each updated desktop agent.
- Confirm a test file is processed and the Obsidian vault remains writable.
- Check recent server logs:

  ```bash
  docker compose logs --tail=100 obsync
  ```

## Roll back

Do not run an older Obsync version against data already migrated by a newer version unless the release notes explicitly say it is safe. The reliable rollback is to restore both the earlier application version and the matching pre-update data backup.

1. Stop the server.
2. Restore the pre-update `/data` archive to the Obsync data volume.
3. Check out the earlier release tag or select the earlier Docker image tag.
4. Recreate the container and verify `/api/v1/health`.

The Obsidian vault is separate from the application data. Restore the vault from its own backup only if files were changed and need to be reverted.
