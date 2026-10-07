"""Command-line entry point for deterministic suite and frozen-run evaluation.

Usage::

    python -m aidison.evaluation [--cases k1,k2] [--mode offline] [--report PATH]
                                 [--json] [--allow-failure]

    python -m aidison.evaluation --replay-agent-run-id UUID \\
        --replay-bundle-ref artifact+sha256://... [--artifact-root PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from aidison.application.agent_run_replay import AgentRunReplayBundleService
from aidison.evaluation.contracts import EvaluationMode, EvaluationReport
from aidison.evaluation.reporter import EvaluationReporter
from aidison.evaluation.runner import EvaluationError, run_evaluation
from aidison.evaluation.settings import EvaluationSettings, build_evaluation_reporter
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.runtime.checkpointing import CheckpointRuntime, CheckpointSettings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aidison.evaluation",
        description="Run the deterministic Aidison evaluation suite",
    )
    parser.add_argument(
        "--cases",
        default="",
        help="comma-separated case keys; empty runs the full offline suite",
    )
    parser.add_argument(
        "--mode",
        default=EvaluationMode.OFFLINE.value,
        choices=[mode.value for mode in EvaluationMode],
        help="offline never contacts a reporter; live is strictly opt-in",
    )
    parser.add_argument("--report", default="", help="write the JSON report to this path")
    parser.add_argument("--json", action="store_true", help="print the JSON report to stdout")
    parser.add_argument(
        "--allow-failure",
        action="store_true",
        help="exit 0 even when a case fails or errors",
    )
    parser.add_argument(
        "--replay-agent-run-id",
        type=UUID,
        help=(
            "evaluate exactly one persisted AgentRun replay bundle; requires "
            "--replay-bundle-ref"
        ),
    )
    parser.add_argument(
        "--replay-bundle-ref",
        default="",
        help=(
            "content-addressed evaluation_replay_bundle artifact ref; requires "
            "--replay-agent-run-id"
        ),
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path(os.getenv("AIDISON_ARTIFACT_ROOT", "artifacts/data")),
        help="read-only Artifact Store root used to verify a persisted replay bundle",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    has_replay_run = args.replay_agent_run_id is not None
    has_replay_ref = bool(args.replay_bundle_ref)
    if has_replay_run != has_replay_ref:
        parser.error("--replay-agent-run-id and --replay-bundle-ref must be supplied together")
    if has_replay_run and args.cases:
        parser.error("--cases cannot be combined with a persisted replay bundle evaluation")
    mode = EvaluationMode(args.mode)
    reporter = None
    if mode is EvaluationMode.LIVE:
        reporter = build_evaluation_reporter(EvaluationSettings())
        if reporter is None:
            print(
                "warning: LIVE mode requested but no evaluation reporter is enabled and fully "
                "configured; the report will not be sent",
                file=sys.stderr,
            )
    try:
        if has_replay_run:
            report = asyncio.run(
                _run_persisted_replay_bundle_evaluation(
                    agent_run_id=args.replay_agent_run_id,
                    bundle_ref=args.replay_bundle_ref,
                    artifact_root=args.artifact_root,
                    mode=mode,
                    reporter=reporter,
                )
            )
        else:
            report = asyncio.run(
                run_evaluation(
                    case_keys=_split_cases(args.cases),
                    mode=mode,
                    reporter=reporter,
                )
            )
    except (EvaluationError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = report.model_dump(mode="json")
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))

    summary = report.summary
    print(
        f"evaluation {report.mode.value} (reporter={report.reporter}): "
        f"{summary.passed} passed, {summary.failed} failed, "
        f"{summary.errored} errored, {summary.skipped} skipped"
    )
    if summary.all_passed or args.allow_failure:
        return 0
    return 1


def _split_cases(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


async def _run_persisted_replay_bundle_evaluation(
    *,
    agent_run_id: UUID,
    bundle_ref: str,
    artifact_root: Path,
    mode: EvaluationMode,
    reporter: EvaluationReporter | None,
) -> EvaluationReport:
    """Evaluate one frozen bundle without starting a graph or any external tool.

    This operational adapter owns short-lived database/checkpointer resources only.
    It deliberately does not create an Artifact directory, invoke LangGraph, or
    contact a Provider. The injected saver is used solely for exact checkpoint
    anchor lookup before the already-frozen payload enters the offline evaluator.
    """

    database = DatabaseSettings()
    engine = create_engine(database)
    checkpoint_database_url = os.getenv(
        "AIDISON_CHECKPOINT_DATABASE_URL", database.database_url
    )
    checkpoints = CheckpointRuntime(
        CheckpointSettings(database_url=checkpoint_database_url)
    )
    try:
        checkpointer = await checkpoints.start()
        session_factory = create_session_factory(engine)
        async with session_factory() as session:
            return await AgentRunReplayBundleService(
                session=session,
                artifact_root=artifact_root,
                checkpointer=checkpointer,
            ).evaluate_persisted(
                agent_run_id=agent_run_id,
                bundle_ref=bundle_ref,
                mode=mode,
                reporter=reporter,
            )
    finally:
        await checkpoints.close()
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
