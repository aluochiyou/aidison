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
published. Caddy Basic Auth is an origin-side fail-closed guard. For the current single-owner test
deployment, Cloudflare Access is a required first identity gate rather than an optional recommendation.

For a temporary one-person test deployment without Cloudflare Zero Trust, use the explicit
`OriginBasicAuth` mode below. It retains the Tunnel and Caddy's bcrypt-protected Basic Auth, but it
does not provide identity-based access logs, MFA, or per-user revocation. Never use this mode for
multiple users or sensitive shared access.

1. In Cloudflare, create a remotely managed Tunnel and copy its connector token. Set the public
   hostname service to `http://gateway:8080`. Before running the initializer, create a **Self-hosted**
   Cloudflare Access application for exactly that hostname and add one **Allow** policy containing only
   your exact email address. Do not use `Everyone`, an unrestricted email domain, or `One-time PIN` as
   the policy's only Include rule. The initializer requires you to confirm this gate explicitly.
2. Start Docker Desktop, then initialize the local secret file from `app/` in Windows PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\initialize-production.ps1
```

If you are intentionally running a single-owner test without Cloudflare Access, use:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\initialize-production.ps1 `
  -AccessMode OriginBasicAuth -Deploy
```

This switch is explicit so an Access-protected deployment cannot silently lose its identity gate.

## Private Tailscale deployment (no card and no public endpoint)

For a one-person test, Tailscale is the recommended alternative when Cloudflare Tunnel or Access is
unavailable. It exposes the Caddy gateway only to devices signed in to your Tailscale tailnet;
`aidison.xyz` is not used and no router port forwarding is required.

1. Install Tailscale for Windows and sign in on this computer and each device that should access
   Aidison.
2. Initialize and deploy without any Cloudflare credential:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\initialize-production.ps1 `
  -PublishMode Tailscale -Deploy
```

3. In an Administrator PowerShell terminal, publish only the loopback gateway to your tailnet:

```powershell
tailscale serve --bg http://127.0.0.1:8080
tailscale serve status
```

Tailscale prints the private HTTPS address. Caddy Basic Auth remains a second gate, so a Tailscale
member must also know the locally generated administrator password. To remove private access later,
run `tailscale serve reset`.

The initializer asks for the administrator password and Tunnel Token without echoing them,
generates a URL-safe PostgreSQL password, hashes the administrator password through Caddy stdin,
and imports only the allowlisted Provider keys already present in `.env`. Standard `LANGFUSE_*`
variables are mapped to Aidison's runtime configuration; when both Langfuse keys are present, tracing
is enabled. It creates the Git-ignored `.env.production` atomically and refuses to overwrite an
existing file.

3. Deploy from the same directory:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\deploy-production.ps1
```

Initialization and deployment can also be run together with
`initialize-production.ps1 -Deploy`.

The deployment script validates required secrets and the Compose model, builds the application
images, starts the stack, and waits for the loopback-only edge health endpoint. It never prints
secret values.

### Enable optional module visuals

Module structure is committed independently from its illustration, so Aidison remains usable when
the optional Qwen visual-spec provider is not configured. To enable it without exposing the API key
in shell history, run from `app/` in Windows PowerShell and paste the key into the hidden prompt:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\configure-module-visual-key.ps1
```

The script atomically updates the Git-ignored `.env.production`, recreates only
`module-visual-worker`, and verifies that the secret reached the container without printing it.

### Switch deep research discovery to Firecrawl

Aidison supports either Tavily or Firecrawl as the bounded search provider. To switch production
research to Firecrawl without putting its key in PowerShell history, run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\configure-firecrawl-search.ps1
```

The script stores the key in `.env.production`, sets
`AIDISON_WEB_SEARCH_PROVIDER=firecrawl`, recreates only the research worker, and verifies the
effective provider without printing the credential.

For manual initialization, copy `.env.production.example` to `.env.production` and fill every
required value. In Cloudflare, create a remotely managed Tunnel and route the public hostname to
`http://gateway:8080`. Configure the single-owner Access application for the same hostname before
initializing the local production file.

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
