from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from aidison.evaluation.deep_research_bench_ii import build_engineering_manifest


def _row(index: int, *, theme: str = "Software") -> dict[str, object]:
    return {
        "id": f"task-{index}",
        "idx": index,
        "language": "zh",
        "theme": theme,
        "description": f"engineering task {index}",
        "prompt": f"prepare report {index}",
        "content": {
            "task": f"prepare report {index}",
            "rubric": {
                "info_recall": ["fact a", "fact b"],
                "analysis": ["compare trade-offs"],
                "presentation": ["include a table"],
            },
            "blocked": {"urls": [f"https://blocked.example/{index}"]},
        },
        "license": "CC BY 4.0",
    }


def test_builds_content_addressed_engineering_subset(tmp_path) -> None:
    dataset = tmp_path / "tasks_and_rubrics.jsonl"
    dataset.write_text(
        "\n".join(json.dumps(_row(index)) for index in (51, 88, 113)) + "\n",
        encoding="utf-8",
    )

    manifest = build_engineering_manifest(
        dataset_path=dataset,
        source_revision="main@2026-10-26",
        generated_at=datetime(2026, 10, 26, tzinfo=UTC),
    )

    assert [case.task_idx for case in manifest.cases] == [51, 88, 113]
    assert manifest.cases[0].max_token_budget == 1_000_000_000
    assert manifest.cases[0].rubric_counts == {
        "info_recall": 2,
        "analysis": 1,
        "presentation": 1,
    }
    assert manifest.cases[0].report_path == "report/aidison/idx-51.md"
    assert len(manifest.dataset_sha256) == 64
    assert len(manifest.content_hash) == 64


def test_rejects_officially_excluded_or_missing_tasks(tmp_path) -> None:
    dataset = tmp_path / "tasks_and_rubrics.jsonl"
    dataset.write_text(json.dumps(_row(112)) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="officially excluded"):
        build_engineering_manifest(
            dataset_path=dataset,
            source_revision="main@2026-10-26",
            task_indexes=(112,),
        )

    with pytest.raises(ValueError, match="missing from dataset"):
        build_engineering_manifest(
            dataset_path=dataset,
            source_revision="main@2026-10-26",
            task_indexes=(51,),
        )
