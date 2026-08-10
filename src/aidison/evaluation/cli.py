"""Command-line entry point for the deterministic evaluation suite.

Usage::

    python -m aidison.evaluation [--cases k1,k2] [--mode offline] [--report PATH]
                                 [--json] [--allow-failure]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from aidison.evaluation.contracts import EvaluationMode
from aidison.evaluation.runner import EvaluationError, run_evaluation
from aidison.evaluation.settings import EvaluationSettings, build_evaluation_reporter


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    mode = EvaluationMode(args.mode)
    reporter = None
    if mode is EvaluationMode.LIVE:
        reporter = build_evaluation_reporter(EvaluationSettings())
        if reporter is None:
            print(
                "warning: LIVE mode requested but LangSmith is not enabled and fully "
                "configured; the report will not be sent",
                file=sys.stderr,
            )
    try:
        report = asyncio.run(
            run_evaluation(
                case_keys=_split_cases(args.cases),
                mode=mode,
                reporter=reporter,
            )
        )
    except EvaluationError as exc:
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


if __name__ == "__main__":
    raise SystemExit(main())
