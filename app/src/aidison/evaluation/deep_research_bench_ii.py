"""Content-addressed DeepResearch Bench II subset preparation for Aidison.

This module deliberately does not reimplement the official GPT judge.  It
freezes the exact public tasks that Aidison should execute and emits the report
paths expected by the official evaluator.  Official rubric scores and Aidison's
runtime/citation diagnostics remain separate metrics.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DRB2_SOURCE_URL = (
    "https://github.com/imlrz/DeepResearch-Bench-II/"
    "blob/main/tasks_and_rubrics.jsonl"
)
# Software framework comparison, hardware/contact engineering, and AI-assisted
# software engineering.  They exercise tables, multi-source synthesis, and an
# engineering decision narrative while keeping the first live run affordable.
AIDISON_ENGINEERING_SUBSET = (51, 88, 113)
# The official October 2026 leaderboard temporarily excludes these cases.
OFFICIAL_EXCLUDED_TASKS = frozenset({112, 114})
RUBRIC_DIMENSIONS = ("info_recall", "analysis", "presentation")


class DeepResearchBenchContent(BaseModel):
    """The nested task payload used by the official JSONL dataset."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    task: str = Field(min_length=1)
    rubric: dict[str, list[str]]
    blocked: dict[str, Any] = Field(default_factory=dict)

    @field_validator("rubric")
    @classmethod
    def rubric_has_all_official_dimensions(
        cls, value: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        missing = [dimension for dimension in RUBRIC_DIMENSIONS if not value.get(dimension)]
        if missing:
            raise ValueError(f"missing non-empty DRB2 rubric dimensions: {', '.join(missing)}")
        return value


class DeepResearchBenchTask(BaseModel):
    """The stable fields Aidison consumes from one official JSONL record."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str = Field(min_length=1)
    idx: int = Field(gt=0)
    language: Literal["zh", "en"]
    theme: str = Field(min_length=1)
    description: str
    prompt: str = Field(min_length=1)
    content: DeepResearchBenchContent
    license: str = Field(min_length=1)

    @property
    def blocked_urls(self) -> tuple[str, ...]:
        raw_urls = self.content.blocked.get("urls")
        if not isinstance(raw_urls, list):
            return ()
        return tuple(url for url in raw_urls if isinstance(url, str) and url)


class DeepResearchBenchRunCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    task_idx: int
    language: Literal["zh", "en"]
    theme: str
    description: str
    prompt: str
    license: str
    blocked_urls: tuple[str, ...]
    rubric_counts: dict[str, int]
    report_path: str
    max_token_budget: int = 128_000
    required_metrics: tuple[str, ...] = (
        "drb2_info_recall",
        "drb2_analysis",
        "drb2_presentation",
        "citation_support_rate",
        "evidence_coverage_rate",
        "token_usage",
        "tool_calls",
        "latency_p95",
        "self_recovery_rate",
    )


class DeepResearchBenchManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["aidison-drb2-manifest/v1"] = "aidison-drb2-manifest/v1"
    source_url: str = DRB2_SOURCE_URL
    source_revision: str
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    selection_policy: str
    cases: tuple[DeepResearchBenchRunCase, ...] = Field(min_length=1)

    @property
    def content_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"generated_at"})
        return sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()


def load_tasks(path: Path) -> tuple[DeepResearchBenchTask, ...]:
    """Validate every non-empty JSONL row and reject duplicate task indexes."""

    tasks: list[DeepResearchBenchTask] = []
    seen_indexes: set[int] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            task = DeepResearchBenchTask.model_validate_json(raw_line)
        except Exception as exc:
            raise ValueError(f"invalid DRB2 row {line_number}: {type(exc).__name__}") from exc
        if task.idx in seen_indexes:
            raise ValueError(f"duplicate DRB2 task index: {task.idx}")
        seen_indexes.add(task.idx)
        tasks.append(task)
    if not tasks:
        raise ValueError("DRB2 dataset is empty")
    return tuple(tasks)


def build_engineering_manifest(
    *,
    dataset_path: Path,
    source_revision: str,
    task_indexes: Sequence[int] = AIDISON_ENGINEERING_SUBSET,
    generated_at: datetime | None = None,
) -> DeepResearchBenchManifest:
    tasks = load_tasks(dataset_path)
    by_index = {task.idx: task for task in tasks}
    requested = tuple(dict.fromkeys(task_indexes))
    excluded = sorted(set(requested) & OFFICIAL_EXCLUDED_TASKS)
    if excluded:
        raise ValueError(f"officially excluded DRB2 task indexes requested: {excluded}")
    missing = sorted(set(requested) - by_index.keys())
    if missing:
        raise ValueError(f"DRB2 task indexes are missing from dataset: {missing}")

    cases = tuple(
        DeepResearchBenchRunCase(
            task_id=by_index[index].id,
            task_idx=index,
            language=by_index[index].language,
            theme=by_index[index].theme,
            description=by_index[index].description,
            prompt=by_index[index].prompt,
            license=by_index[index].license,
            blocked_urls=by_index[index].blocked_urls,
            rubric_counts={
                dimension: len(by_index[index].content.rubric[dimension])
                for dimension in RUBRIC_DIMENSIONS
            },
            report_path=f"report/aidison/idx-{index}.md",
        )
        for index in requested
    )
    return DeepResearchBenchManifest(
        source_revision=source_revision,
        dataset_sha256=sha256(dataset_path.read_bytes()).hexdigest(),
        generated_at=generated_at or datetime.now(UTC),
        selection_policy=(
            "Fixed three-case engineering slice: software framework comparison, "
            "hardware/contact engineering, and AI-assisted software engineering; "
            "officially excluded tasks 112/114 are rejected."
        ),
        cases=cases,
    )


def _parse_indexes(value: str) -> tuple[int, ...]:
    indexes = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    if not indexes:
        raise argparse.ArgumentTypeError("at least one task index is required")
    return indexes


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Freeze an Aidison DeepResearch Bench II engineering subset"
    )
    parser.add_argument("dataset", type=Path, help="official tasks_and_rubrics.jsonl")
    parser.add_argument(
        "--source-revision",
        required=True,
        help="upstream commit/tag or dated main snapshot label",
    )
    parser.add_argument(
        "--task-indexes",
        type=_parse_indexes,
        default=AIDISON_ENGINEERING_SUBSET,
        help="comma-separated task indexes (default: 51,88,113)",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)
    manifest = build_engineering_manifest(
        dataset_path=args.dataset,
        source_revision=args.source_revision,
        task_indexes=args.task_indexes,
    )
    rendered = json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AIDISON_ENGINEERING_SUBSET",
    "DeepResearchBenchManifest",
    "DeepResearchBenchRunCase",
    "DeepResearchBenchContent",
    "DeepResearchBenchTask",
    "build_engineering_manifest",
    "load_tasks",
    "main",
]
