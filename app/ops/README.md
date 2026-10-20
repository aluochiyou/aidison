# Backup and isolated restore

These scripts are for the local Windows + Docker Desktop demo environment. A backup is a
consistent pair: a PostgreSQL custom dump, the content-addressed Artifact bytes, and a SHA-256
manifest. The scripts do not expose backup or restore as an application API.

## Backup

Run from the `app/` directory in Windows PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\backup.ps1 `
  -OutputRoot D:\aidison-backups -ComposeProject aidison
```

The script records which of `api` and `worker` are running, pauses those write paths, runs the
read-only Artifact integrity gate, copies both stores, and restores the original service state in
a `finally` block. It never overwrites an existing backup. A `present` metadata row with missing or
corrupt bytes causes the backup to fail closed.

## Restore verification

Restore only into a new, isolated Compose project (also from `app/`):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\restore.ps1 `
  -BackupPath D:\aidison-backups\aidison-backup-YYYYMMDDTHHMMSSZ-xxxxxxxx `
  -TargetComposeProject aidison-restore-verify `
  -PostgresPort 55434
```

The restore script rejects the source project and any existing target containers or named volumes.
It keeps the isolated target after verification so it can be inspected. It has no option to replace
the active `aidison` runtime database or Artifact volume.

## Production deployment on this Windows host

`compose.production.yaml` is deliberately separate from the local developer stack. PostgreSQL and
the FastAPI service have no host port; only Caddy binds to `127.0.0.1`. `cloudflared` creates the
outbound-only public connection, so the router needs no port forwarding and the home IP is not
published. Caddy Basic Auth is an origin-side fail-closed guard; Cloudflare Access should be enabled
as a second identity gate.

1. Copy `.env.production.example` to `.env.production` and fill the required values.
2. In Cloudflare, create a remotely managed Tunnel and set its public hostname service to
   `http://gateway:8080`. Configure an Access application for the same hostname before inviting
   users.
3. Start Docker Desktop, then run from `app/` in Windows PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\deploy-production.ps1
```

The script validates required secrets and the Compose model, builds the application images, starts
the stack, and waits for the loopback-only edge health endpoint. It never prints secret values.

Before each production release, create the consistent PostgreSQL + Artifact pair with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\backup.ps1 `
  -OutputRoot D:\aidison-backups `
  -ComposeFile compose.production.yaml `
  -ComposeProject aidison-production `
  -EnvFile .env.production
```

Test restore into an isolated developer Compose project at least once after every database
migration. Do not publish ports `5432`, `8000`, `3000`, or `8080` through the router or Windows
Firewall; all public traffic must enter through the Tunnel.
