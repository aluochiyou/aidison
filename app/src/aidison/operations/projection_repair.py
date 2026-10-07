"""Guarded maintenance command for repairing one event-backed read model.

The command deliberately has no API route.  Operators first run a read-only
preview, inspect its two relation hashes, then supply the previewed current
hash again with ``--apply``.  This prevents a delayed maintenance command from
overwriting a projection that changed after review.

Usage::

    python -m aidison.operations.projection_repair \\
        --project-id <UUID> --execution-plan-id <UUID>

    python -m aidison.operations.projection_repair \\
        --project-id <UUID> --execution-plan-id <UUID> \\
        --apply --expected-current-relation-hash <preview hash>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aidison.application.event_replay import (
    AgentResultProjectionRepairPreview,
    AgentResultProjectionRepairService,
    AgentRunDecisionProjectionRepairPreview,
    AgentRunDecisionProjectionRepairService,
    AgentRunProjectionRepairPreview,
    AgentRunProjectionRepairService,
    ExecutionPlanProjectionRepairPreview,
    ExecutionPlanProjectionRepairService,
    ProjectionRepairConflictError,
)
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory


@dataclass(frozen=True)
class ProjectionRepairCommandReport:
    """JSON-safe result of a preview or a reviewed repair application."""

    preview: (
        ExecutionPlanProjectionRepairPreview
        | AgentRunProjectionRepairPreview
        | AgentResultProjectionRepairPreview
        | AgentRunDecisionProjectionRepairPreview
    )
    applied: bool

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": "projection-repair.v1",
            "project_id": str(self.preview.project_id),
            "event_cursor": self.preview.event_cursor,
            "event_count": self.preview.event_count,
            "current_relation_hash": self.preview.current_relation_hash,
            "replayed_relation_hash": self.preview.replayed_relation_hash,
            "repair_required": self.preview.repair_required,
            "applied": self.applied,
        }
        if isinstance(self.preview, ExecutionPlanProjectionRepairPreview):
            payload["aggregate_type"] = "execution_plan"
            payload["execution_plan_id"] = str(self.preview.execution_plan_id)
        elif isinstance(self.preview, AgentRunProjectionRepairPreview):
            payload["aggregate_type"] = "agent_run"
            payload["agent_run_id"] = str(self.preview.agent_run_id)
        else:
            if isinstance(self.preview, AgentResultProjectionRepairPreview):
                payload["aggregate_type"] = "agent_run_result"
                payload["agent_run_result_id"] = str(self.preview.agent_run_result_id)
                return payload
            payload["aggregate_type"] = "agent_run_decision"
            payload["agent_run_decision_id"] = str(self.preview.agent_run_decision_id)
        return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aidison.operations.projection_repair",
        description="Preview or explicitly repair one event-backed relation projection.",
    )
    parser.add_argument("--project-id", required=True, type=UUID)
    aggregate = parser.add_mutually_exclusive_group(required=True)
    aggregate.add_argument("--execution-plan-id", type=UUID)
    aggregate.add_argument("--agent-run-id", type=UUID)
    aggregate.add_argument("--agent-run-result-id", type=UUID)
    aggregate.add_argument("--agent-run-decision-id", type=UUID)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the reviewed repair; omitting this flag is always read-only",
    )
    parser.add_argument(
        "--expected-current-relation-hash",
        default="",
        help="exact current_relation_hash returned by the reviewed preview; required with --apply",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.apply and not args.expected_current_relation_hash:
        parser.error("--apply requires --expected-current-relation-hash from a reviewed preview")
    if not args.apply and args.expected_current_relation_hash:
        parser.error("--expected-current-relation-hash is only valid together with --apply")

    try:
        report = asyncio.run(_run(args))
    except ProjectionRepairConflictError as exc:
        print(
            json.dumps(
                {
                    "schema_version": "projection-repair.v1",
                    "ok": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


async def _run(args: argparse.Namespace) -> ProjectionRepairCommandReport:
    """Open only the short database transaction needed by the requested mode."""

    engine = create_engine(DatabaseSettings())
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            if args.execution_plan_id is not None:
                report = await _run_execution_plan_repair(session, args)
            elif args.agent_run_id is not None:
                report = await _run_agent_run_repair(session, args)
            elif args.agent_run_result_id is not None:
                report = await _run_agent_result_repair(session, args)
            else:
                report = await _run_agent_decision_repair(session, args)
            if report.applied:
                await session.commit()
            else:
                await session.rollback()
            return report
    finally:
        await engine.dispose()


async def _run_execution_plan_repair(
    session: AsyncSession,
    args: argparse.Namespace,
) -> ProjectionRepairCommandReport:
    """Run the selected plan mode inside the caller-owned transaction."""

    service = ExecutionPlanProjectionRepairService(session)
    if not args.apply:
        preview = await service.preview(
            project_id=args.project_id,
            execution_plan_id=args.execution_plan_id,
        )
        return ProjectionRepairCommandReport(preview=preview, applied=False)
    result = await service.apply(
        project_id=args.project_id,
        execution_plan_id=args.execution_plan_id,
        expected_current_relation_hash=args.expected_current_relation_hash,
        dry_run=False,
    )
    return ProjectionRepairCommandReport(preview=result.preview, applied=result.applied)


async def _run_agent_run_repair(
    session: AsyncSession,
    args: argparse.Namespace,
) -> ProjectionRepairCommandReport:
    """Run the selected inactive-AgentRun mode inside the caller transaction."""

    service = AgentRunProjectionRepairService(session)
    if not args.apply:
        preview = await service.preview(
            project_id=args.project_id,
            agent_run_id=args.agent_run_id,
        )
        return ProjectionRepairCommandReport(preview=preview, applied=False)
    result = await service.apply(
        project_id=args.project_id,
        agent_run_id=args.agent_run_id,
        expected_current_relation_hash=args.expected_current_relation_hash,
        dry_run=False,
    )
    return ProjectionRepairCommandReport(preview=result.preview, applied=result.applied)


async def _run_agent_result_repair(
    session: AsyncSession,
    args: argparse.Namespace,
) -> ProjectionRepairCommandReport:
    """Run the selected result/admission pair repair inside the caller transaction."""

    service = AgentResultProjectionRepairService(session)
    if not args.apply:
        preview = await service.preview(
            project_id=args.project_id,
            agent_run_result_id=args.agent_run_result_id,
        )
        return ProjectionRepairCommandReport(preview=preview, applied=False)
    result = await service.apply(
        project_id=args.project_id,
        agent_run_result_id=args.agent_run_result_id,
        expected_current_relation_hash=args.expected_current_relation_hash,
        dry_run=False,
    )
    return ProjectionRepairCommandReport(preview=result.preview, applied=result.applied)


async def _run_agent_decision_repair(
    session: AsyncSession,
    args: argparse.Namespace,
) -> ProjectionRepairCommandReport:
    """Run the selected user-decision repair inside the caller transaction."""

    service = AgentRunDecisionProjectionRepairService(session)
    if not args.apply:
        preview = await service.preview(
            project_id=args.project_id,
            agent_run_decision_id=args.agent_run_decision_id,
        )
        return ProjectionRepairCommandReport(preview=preview, applied=False)
    result = await service.apply(
        project_id=args.project_id,
        agent_run_decision_id=args.agent_run_decision_id,
        expected_current_relation_hash=args.expected_current_relation_hash,
        dry_run=False,
    )
    return ProjectionRepairCommandReport(preview=result.preview, applied=result.applied)


if __name__ == "__main__":
    raise SystemExit(main())
