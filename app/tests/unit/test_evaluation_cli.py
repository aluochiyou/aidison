from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pytest import MonkeyPatch

from aidison.evaluation import cli
from aidison.evaluation.contracts import EvaluationMode
from aidison.evaluation.runner import run_evaluation


def test_replay_bundle_arguments_must_be_supplied_together() -> None:
    with pytest.raises(SystemExit, match="2"):
        cli.main(["--replay-agent-run-id", str(uuid4())])


def test_replay_bundle_evaluation_rejects_static_case_selection() -> None:
    with pytest.raises(SystemExit, match="2"):
        cli.main(
            [
                "--cases",
                "structured-contract-complete",
                "--replay-agent-run-id",
                str(uuid4()),
                "--replay-bundle-ref",
                "artifact+sha256://bundle/one",
            ]
        )


def test_replay_bundle_cli_dispatches_exact_frozen_inputs(monkeypatch: MonkeyPatch) -> None:
    run_id = uuid4()
    captured: dict[str, object] = {}

    async def fake_evaluation(**kwargs: object):
        captured.update(kwargs)
        return await run_evaluation(case_keys=("structured-contract-complete",))

    monkeypatch.setattr(cli, "_run_persisted_replay_bundle_evaluation", fake_evaluation)

    exit_code = cli.main(
        [
            "--replay-agent-run-id",
            str(run_id),
            "--replay-bundle-ref",
            "artifact+sha256://bundle/one",
            "--artifact-root",
            "test-artifacts",
        ]
    )

    assert exit_code == 0
    assert captured == {
        "agent_run_id": run_id,
        "bundle_ref": "artifact+sha256://bundle/one",
        "artifact_root": Path("test-artifacts"),
        "mode": EvaluationMode.OFFLINE,
        "reporter": None,
    }


def test_default_cli_keeps_using_static_fixture_evaluation(monkeypatch: MonkeyPatch) -> None:
    called = False

    async def forbidden_replay(**kwargs: object) -> object:
        del kwargs
        nonlocal called
        called = True
        return await run_evaluation(case_keys=("structured-contract-complete",))

    monkeypatch.setattr(cli, "_run_persisted_replay_bundle_evaluation", forbidden_replay)

    assert cli.main(["--cases", "structured-contract-complete"]) == 0
    assert called is False
