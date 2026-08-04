from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from sqlalchemy import select

from aidison.artifacts.contracts import ArtifactStatus
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.orm import ArtifactRow


@dataclass(frozen=True)
class ArtifactInventoryEntry:
    artifact_id: str
    storage_key: str
    content_hash: str
    size_bytes: int
    status: str


@dataclass(frozen=True)
class ArtifactIssue:
    artifact_id: str
    storage_key: str
    issue: str


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _storage_path(root: Path, storage_key: str) -> Path:
    candidate = (root / storage_key).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("storage_key escaped artifact root")
    return candidate


def audit_inventory(
    entries: list[ArtifactInventoryEntry],
    artifact_root: Path,
    *,
    strict_orphans: bool = False,
) -> dict[str, Any]:
    root = artifact_root.resolve()
    issues: list[ArtifactIssue] = []
    referenced_paths: set[Path] = set()
    present_rows = 0
    non_present_rows = 0

    for entry in entries:
        if entry.status != ArtifactStatus.PRESENT.value:
            non_present_rows += 1
            continue
        present_rows += 1
        try:
            path = _storage_path(root, entry.storage_key)
        except ValueError:
            issues.append(ArtifactIssue(entry.artifact_id, entry.storage_key, "invalid_path"))
            continue
        referenced_paths.add(path)
        if not path.is_file():
            issues.append(ArtifactIssue(entry.artifact_id, entry.storage_key, "missing"))
            continue
        if path.stat().st_size != entry.size_bytes:
            issues.append(ArtifactIssue(entry.artifact_id, entry.storage_key, "size_mismatch"))
            continue
        if _sha256_file(path) != entry.content_hash:
            issues.append(ArtifactIssue(entry.artifact_id, entry.storage_key, "hash_mismatch"))

    actual_files: set[Path] = set()
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_file():
                resolved = path.resolve()
                if resolved == root or root not in resolved.parents:
                    continue
                actual_files.add(resolved)
    orphan_files = sorted(
        path.relative_to(root).as_posix() for path in actual_files - referenced_paths
    )
    ok = not issues and (not strict_orphans or not orphan_files)
    return {
        "schema_version": 1,
        "ok": ok,
        "artifact_root": str(root),
        "metadata_rows": len(entries),
        "present_rows": present_rows,
        "non_present_rows": non_present_rows,
        "referenced_unique_files": len(referenced_paths),
        "actual_files": len(actual_files),
        "issues": [asdict(issue) for issue in issues],
        "orphan_files": orphan_files,
        "strict_orphans": strict_orphans,
    }


async def _load_inventory() -> list[ArtifactInventoryEntry]:
    engine = create_engine(DatabaseSettings())
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            rows = await session.scalars(select(ArtifactRow).order_by(ArtifactRow.id))
            return [
                ArtifactInventoryEntry(
                    artifact_id=str(row.id),
                    storage_key=row.storage_key,
                    content_hash=row.content_hash,
                    size_bytes=row.size_bytes,
                    status=row.status,
                )
                for row in rows
            ]
    finally:
        await engine.dispose()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only PostgreSQL/Artifact byte consistency check."
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path(os.getenv("AIDISON_ARTIFACT_ROOT", "artifacts/data")),
    )
    parser.add_argument(
        "--strict-orphans",
        action="store_true",
        help="also fail when byte files are not referenced by PostgreSQL metadata",
    )
    return parser.parse_args()


async def _run() -> int:
    args = _parse_args()
    try:
        report = audit_inventory(
            await _load_inventory(),
            args.artifact_root,
            strict_orphans=args.strict_orphans,
        )
    except Exception as exc:  # pragma: no cover - operational envelope
        report = {"schema_version": 1, "ok": False, "error": str(exc)}
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 2


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
