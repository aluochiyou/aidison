from __future__ import annotations

from pathlib import Path

import pytest

from aidison.research.source_collection import (
    LocalFileResearchSourceCollector,
    LocalFileSourceCollectionError,
)


@pytest.mark.asyncio
async def test_local_file_collector_reads_only_explicit_relative_source(tmp_path: Path) -> None:
    reference = tmp_path / "specs" / "flight-control.md"
    reference.parent.mkdir()
    reference.write_text("# Flight control\n\nUART is required.", encoding="utf-8")
    (tmp_path / "private.txt").write_text("not configured", encoding="utf-8")

    sources = await LocalFileResearchSourceCollector(
        source_root=tmp_path,
        source_targets=("specs/flight-control.md",),
    ).collect(
        run=object(),  # type: ignore[arg-type]
        task=object(),  # type: ignore[arg-type]
        question="A task question does not supply a filesystem path.",
    )

    assert len(sources) == 1
    [source] = sources
    assert source.source.canonical_locator == "aidison://project-file/specs/flight-control.md"
    assert source.normalized_document == "# Flight control\n\nUART is required."
    assert source.media_type == "text/markdown"
    assert source.coverage_source_kinds == ("evidence",)


@pytest.mark.asyncio
async def test_local_file_collector_rejects_missing_and_non_utf8_sources(tmp_path: Path) -> None:
    async def collect(target: str) -> LocalFileSourceCollectionError:
        collector = LocalFileResearchSourceCollector(
            source_root=tmp_path,
            source_targets=(target,),
        )
        with pytest.raises(LocalFileSourceCollectionError) as raised:
            await collector.collect(
                run=object(),  # type: ignore[arg-type]
                task=object(),  # type: ignore[arg-type]
                question="ignored",
            )
        return raised.value

    assert (await collect("missing.md")).reason_code == "local_source_not_found"
    invalid = tmp_path / "invalid.txt"
    invalid.write_bytes(b"\xff\xfe")
    assert (await collect("invalid.txt")).reason_code == "local_source_invalid_text"


@pytest.mark.asyncio
async def test_local_file_collector_rejects_oversized_source_before_reading(tmp_path: Path) -> None:
    oversized = tmp_path / "large.txt"
    oversized.write_bytes(b"x" * 33)
    collector = LocalFileResearchSourceCollector(
        source_root=tmp_path,
        source_targets=("large.txt",),
        max_document_characters=8,
    )

    with pytest.raises(LocalFileSourceCollectionError) as raised:
        await collector.collect(
            run=object(),  # type: ignore[arg-type]
            task=object(),  # type: ignore[arg-type]
            question="ignored",
        )

    assert raised.value.reason_code == "local_document_too_large"


@pytest.mark.parametrize(
    "target",
    ("../private.txt", "/etc/passwd", "specs/../private.txt", "specs//one.md"),
)
def test_local_file_collector_rejects_escaping_or_ambiguous_paths(
    tmp_path: Path, target: str
) -> None:
    with pytest.raises(ValueError):
        LocalFileResearchSourceCollector(
            source_root=tmp_path,
            source_targets=(target,),
        )
