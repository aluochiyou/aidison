from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from aidison.operations.artifact_integrity import ArtifactInventoryEntry, audit_inventory


def _entry(
    *,
    artifact_id: str = "artifact-1",
    storage_key: str,
    content: bytes,
    status: str = "present",
) -> ArtifactInventoryEntry:
    return ArtifactInventoryEntry(
        artifact_id=artifact_id,
        storage_key=storage_key,
        content_hash=sha256(content).hexdigest(),
        size_bytes=len(content),
        status=status,
    )


def test_audit_inventory_accepts_deduplicated_present_bytes(tmp_path: Path) -> None:
    content = b"evidence"
    key = f"sha256/{sha256(content).hexdigest()[:2]}/payload"
    path = tmp_path / key
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    report = audit_inventory(
        [
            _entry(artifact_id="artifact-1", storage_key=key, content=content),
            _entry(artifact_id="artifact-2", storage_key=key, content=content),
        ],
        tmp_path,
    )

    assert report["ok"] is True
    assert report["present_rows"] == 2
    assert report["referenced_unique_files"] == 1
    assert report["issues"] == []


def test_audit_inventory_reports_missing_and_hash_mismatch(tmp_path: Path) -> None:
    expected = b"expected"
    corrupt_key = "sha256/aa/corrupt"
    corrupt = tmp_path / corrupt_key
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"wrong---")

    report = audit_inventory(
        [
            _entry(storage_key="sha256/bb/missing", content=expected),
            _entry(artifact_id="artifact-2", storage_key=corrupt_key, content=expected),
        ],
        tmp_path,
    )

    assert report["ok"] is False
    assert [issue["issue"] for issue in report["issues"]] == ["missing", "hash_mismatch"]


def test_audit_inventory_ignores_non_present_rows_and_can_fail_on_orphans(
    tmp_path: Path,
) -> None:
    orphan = tmp_path / "sha256" / "cc" / "orphan"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    entry = _entry(storage_key="sha256/dd/not-required", content=b"absent", status="missing")

    relaxed = audit_inventory([entry], tmp_path)
    strict = audit_inventory([entry], tmp_path, strict_orphans=True)

    assert relaxed["ok"] is True
    assert relaxed["non_present_rows"] == 1
    assert relaxed["orphan_files"] == ["sha256/cc/orphan"]
    assert strict["ok"] is False


def test_audit_inventory_rejects_escaped_storage_key(tmp_path: Path) -> None:
    report = audit_inventory(
        [_entry(storage_key="../outside", content=b"outside")],
        tmp_path,
    )

    assert report["ok"] is False
    assert report["issues"][0]["issue"] == "invalid_path"


def test_audit_inventory_checks_quarantined_project_document_bytes(tmp_path: Path) -> None:
    content = b"quarantined project source"
    key = f"project-sources/sha256/{sha256(content).hexdigest()[:2]}/payload"
    path = tmp_path / key
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    report = audit_inventory(
        [
            ArtifactInventoryEntry(
                artifact_id="project_source_document:document-1",
                storage_key=key,
                content_hash=sha256(content).hexdigest(),
                size_bytes=len(content),
                status="quarantined",
                expected_bytes=True,
            )
        ],
        tmp_path,
        strict_orphans=True,
    )

    assert report["ok"] is True
    assert report["present_rows"] == 1
    assert report["orphan_files"] == []
