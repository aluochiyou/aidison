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
