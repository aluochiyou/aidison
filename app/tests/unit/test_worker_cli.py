from pathlib import Path

import pytest
from pydantic import ValidationError

from aidison.worker import WorkerSettings, _parser


def test_worker_settings_use_safe_local_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "AIDISON_ARTIFACT_ROOT",
        "AIDISON_WORKER_ID",
        "AIDISON_WORKER_CONCURRENCY",
        "AIDISON_WORKER_LEASE_SECONDS",
        "AIDISON_WORKER_POLL_SECONDS",
        "AIDISON_DURABLE_RECHECK_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = WorkerSettings(_env_file=None)

    assert settings.artifact_root == Path("artifacts/data")
    assert settings.concurrency == 3
    assert settings.lease_seconds == 60
    assert settings.poll_seconds == 0.5
    assert settings.durable_recheck_seconds == 30
    assert settings.worker_id


def test_worker_rejects_concurrency_that_can_deadlock_parent_wave() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 3"):
        WorkerSettings(_env_file=None, concurrency=2)


@pytest.mark.parametrize(
    ("arguments", "once", "check"),
    [([], False, False), (["--once"], True, False), (["--check"], False, True)],
)
def test_worker_cli_modes(arguments: list[str], once: bool, check: bool) -> None:
    parsed = _parser().parse_args(arguments)
    assert parsed.once is once
    assert parsed.check is check


def test_worker_cli_parses_targeted_research_run_id() -> None:
    parsed = _parser().parse_args(
        ["--runtime", "research", "--once", "--run-id", "cd07e61a-5d09-4e07-bda5-9a4a2f0a3cf1"]
    )

    assert str(parsed.run_id) == "cd07e61a-5d09-4e07-bda5-9a4a2f0a3cf1"
