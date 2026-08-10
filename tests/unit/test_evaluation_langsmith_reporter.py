from __future__ import annotations

import asyncio
import json

from pydantic import SecretStr
from pytest import MonkeyPatch

from aidison.evaluation.langsmith_reporter import (
    LangSmithEvaluationReporter,
    NoopEvaluationReporter,
)
from aidison.evaluation.runner import run_evaluation
from aidison.evaluation.settings import EvaluationSettings, build_evaluation_reporter

REAL_KEY = "lsv2_pt_0000000000000000000000000000000000000000000000000000000000"


def _settings(**changes: object) -> EvaluationSettings:
    defaults: dict[str, object] = {
        "langsmith_enabled": False,
        "langsmith_project": "aidison-evaluation",
        "langsmith_api_key": None,
    }
    defaults.update(changes)
    return EvaluationSettings(_env_file=None, **defaults)


def test_build_reporter_returns_none_when_disabled() -> None:
    reporter = build_evaluation_reporter(_settings(langsmith_enabled=False))
    assert reporter is None


def test_build_reporter_returns_none_when_missing_key_or_project() -> None:
    assert (
        build_evaluation_reporter(_settings(langsmith_enabled=True, langsmith_api_key=None)) is None
    )
    assert (
        build_evaluation_reporter(
            _settings(
                langsmith_enabled=True,
                langsmith_api_key=SecretStr(REAL_KEY),
                langsmith_project="",
            )
        )
        is None
    )


def test_build_reporter_enabled_only_when_fully_configured() -> None:
    reporter = build_evaluation_reporter(
        _settings(langsmith_enabled=True, langsmith_api_key=SecretStr(REAL_KEY))
    )
    assert reporter is not None
    assert reporter.enabled is True


def test_settings_accepts_standard_langsmith_project_variable(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_PROJECT", "aidison-live-evaluation")
    settings = EvaluationSettings(_env_file=None)
    assert settings.langsmith_project == "aidison-live-evaluation"


def test_report_payload_never_contains_the_api_key() -> None:
    report = asyncio.run(run_evaluation())
    payload = json.dumps(report.model_dump(mode="json"), sort_keys=True)
    assert REAL_KEY not in payload
    assert "api_key" not in payload.lower()


def test_settings_dump_redacts_the_secret() -> None:
    settings = _settings(langsmith_enabled=True, langsmith_api_key=SecretStr(REAL_KEY))
    dumped = json.dumps(settings.model_dump(mode="json"), sort_keys=True)
    assert REAL_KEY not in dumped
    assert "**********" in dumped


def test_fake_client_records_run_and_feedback_without_network() -> None:
    report = asyncio.run(run_evaluation(case_keys=("red-action-blocked",)))
    created: list[FakeClient] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            self.constructor_kwargs = kwargs
            self.runs: list[dict[str, object]] = []
            self.feedback: list[dict[str, object]] = []
            self.flushed = 0

        def create_run(self, **kwargs: object) -> None:
            self.runs.append(kwargs)

        def create_feedback(self, **kwargs: object) -> None:
            self.feedback.append(kwargs)

        def flush(self) -> None:
            self.flushed += 1

    def factory(api_key: str, api_url: str) -> FakeClient:
        client = FakeClient(api_key=api_key, api_url=api_url)
        created.append(client)
        return client

    reporter = LangSmithEvaluationReporter(
        project_name="aidison-evaluation",
        api_key=REAL_KEY,
        client_factory=factory,
    )
    assert reporter.enabled is True
    reporter.report(report)

    assert len(created) == 1
    client = created[0]
    assert len(client.runs) == 1
    assert client.runs[0]["id"] == str(report.run_id)
    assert "run_id" not in client.runs[0]
    assert client.runs[0]["run_type"] == "chain"
    assert "aidison-evaluation" in client.runs[0]["tags"]
    assert len(client.feedback) == 1
    assert client.feedback[0]["key"] == "case_status"
    assert client.feedback[0]["score"] == 1.0
    assert client.flushed == 1
    # The client receives the raw key internally, but no report payload does.
    assert client.constructor_kwargs["api_key"] == REAL_KEY


def test_reporting_failure_is_swallowed_fail_closed() -> None:
    report = asyncio.run(run_evaluation(case_keys=("red-action-blocked",)))

    class BrokenClient:
        def create_run(self, **kwargs: object) -> None:
            raise ConnectionError("simulated network failure")

        def create_feedback(self, **kwargs: object) -> None:
            raise AssertionError("should not be reached")

        def flush(self) -> None:
            raise AssertionError("should not be reached")

    reporter = LangSmithEvaluationReporter(
        project_name="aidison-evaluation",
        api_key=REAL_KEY,
        client_factory=lambda api_key, api_url: BrokenClient(),
    )
    reporter.report(report)  # must not raise


def test_disabled_reporter_never_touches_the_client() -> None:
    report = asyncio.run(run_evaluation(case_keys=("red-action-blocked",)))

    class ExplodingClient:
        def __init__(self, **kwargs: object) -> None:
            raise AssertionError("client must not be constructed when disabled")

    reporter = LangSmithEvaluationReporter(
        project_name="aidison-evaluation",
        api_key="",
        client_factory=lambda api_key, api_url: ExplodingClient(),
    )
    assert reporter.enabled is False
    reporter.report(report)


def test_noop_reporter_is_disabled_sink() -> None:
    reporter = NoopEvaluationReporter()
    assert reporter.enabled is False
    assert reporter.name == "none"
    report = asyncio.run(run_evaluation())
    reporter.report(report)  # no-op
