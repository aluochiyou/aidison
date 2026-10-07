from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import SecretStr
from pytest import MonkeyPatch

from aidison.evaluation import settings as settings_module
from aidison.evaluation.langfuse_reporter import LangfuseEvaluationReporter
from aidison.evaluation.runner import run_evaluation
from aidison.evaluation.settings import EvaluationSettings, build_evaluation_reporter

PUBLIC_KEY = "pk-lf-test-public"
SECRET_KEY = "sk-lf-test-secret"


def _settings(**changes: object) -> EvaluationSettings:
    defaults: dict[str, object] = {
        "langfuse_enabled": False,
        "langfuse_project": "aidison-evaluation",
        "langfuse_public_key": None,
        "langfuse_secret_key": None,
        "langfuse_base_url": "https://langfuse.test",
        "langfuse_environment": "test",
        "langfuse_release": None,
        "langsmith_enabled": False,
        "langsmith_project": "aidison-evaluation",
        "langsmith_api_key": None,
    }
    defaults.update(changes)
    return EvaluationSettings(_env_file=None, **defaults)


def _report() -> Any:
    return asyncio.run(
        run_evaluation(
            case_keys=(
                "structured-contract-complete",
                "red-action-blocked",
            )
        )
    )


class FakeObservation:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    def __enter__(self) -> FakeObservation:
        self._client.entered += 1
        if self._client.failure_point == "enter":
            raise RuntimeError("simulated observation enter failure")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> bool:
        del exc_type, exc_value, traceback
        self._client.exited += 1
        if self._client.failure_point == "exit":
            raise RuntimeError("simulated observation exit failure")
        return False


class FakeClient:
    def __init__(self, *, failure_point: str | None = None) -> None:
        self.failure_point = failure_point
        self.observation_calls: list[dict[str, Any]] = []
        self.score_calls: list[dict[str, Any]] = []
        self.entered = 0
        self.exited = 0
        self.flushed = 0

    def start_as_current_observation(self, **kwargs: Any) -> FakeObservation:
        if self.failure_point == "start":
            raise RuntimeError("simulated observation creation failure")
        self.observation_calls.append(kwargs)
        return FakeObservation(self)

    def create_score(self, **kwargs: Any) -> None:
        if self.failure_point == "score":
            raise RuntimeError("simulated score export failure")
        self.score_calls.append(kwargs)

    def flush(self) -> None:
        if self.failure_point == "flush":
            raise RuntimeError("simulated flush failure")
        self.flushed += 1


class FakeClientFactory:
    def __init__(self, *, failure_point: str | None = None) -> None:
        self.failure_point = failure_point
        self.calls: list[dict[str, Any]] = []
        self.clients: list[FakeClient] = []

    def __call__(self, **kwargs: Any) -> FakeClient:
        self.calls.append(kwargs)
        if self.failure_point == "factory":
            raise RuntimeError("simulated client construction failure")
        client = FakeClient(failure_point=self.failure_point)
        self.clients.append(client)
        return client


def _reporter(factory: FakeClientFactory) -> LangfuseEvaluationReporter:
    return LangfuseEvaluationReporter(
        project_name="aidison-evaluation",
        public_key=PUBLIC_KEY,
        secret_key=SECRET_KEY,
        base_url="https://langfuse.test",
        environment="test",
        release="test-release",
        client_factory=factory,
    )


def test_build_reporter_is_disabled_by_default() -> None:
    settings = _settings()

    assert settings.langfuse_enabled is False
    assert build_evaluation_reporter(settings) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"langfuse_public_key": None},
        {"langfuse_secret_key": None},
        {"langfuse_project": ""},
    ],
)
def test_build_reporter_requires_complete_configuration(changes: dict[str, object]) -> None:
    configured: dict[str, object] = {
        "langfuse_enabled": True,
        "langfuse_public_key": SecretStr(PUBLIC_KEY),
        "langfuse_secret_key": SecretStr(SECRET_KEY),
    }
    configured.update(changes)

    assert build_evaluation_reporter(_settings(**configured)) is None


def test_build_reporter_forwards_all_langfuse_settings(monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_reporter(**kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(settings_module, "LangfuseEvaluationReporter", fake_reporter)
    reporter = build_evaluation_reporter(
        _settings(
            langfuse_enabled=True,
            langfuse_project="aidison-ci",
            langfuse_public_key=SecretStr(PUBLIC_KEY),
            langfuse_secret_key=SecretStr(SECRET_KEY),
            langfuse_base_url="https://self-hosted.langfuse.test",
            langfuse_environment="ci",
            langfuse_release="2026.10.6",
        )
    )

    assert reporter is sentinel
    assert captured == {
        "project_name": "aidison-ci",
        "public_key": PUBLIC_KEY,
        "secret_key": SECRET_KEY,
        "base_url": "https://self-hosted.langfuse.test",
        "environment": "ci",
        "release": "2026.10.6",
    }


def test_disabled_reporter_never_constructs_a_client() -> None:
    factory = FakeClientFactory()
    reporter = LangfuseEvaluationReporter(
        project_name="aidison-evaluation",
        public_key=PUBLIC_KEY,
        secret_key="",
        client_factory=factory,
    )

    assert reporter.enabled is False
    assert reporter.report(_report()) is False
    assert factory.calls == []


def test_client_construction_is_lazy() -> None:
    factory = FakeClientFactory()
    reporter = _reporter(factory)

    assert reporter.enabled is True
    assert factory.calls == []

    assert reporter.report(_report()) is True

    assert factory.calls == [
        {
            "public_key": PUBLIC_KEY,
            "secret_key": SECRET_KEY,
            "base_url": "https://langfuse.test",
            "environment": "test",
            "release": "test-release",
        }
    ]


def test_report_creates_one_evaluator_trace_and_one_score_per_metric() -> None:
    report = _report()
    factory = FakeClientFactory()

    assert _reporter(factory).report(report) is True

    client = factory.clients[0]
    assert len(client.observation_calls) == 1
    observation = client.observation_calls[0]
    assert observation["as_type"] == "evaluator"
    assert observation["trace_context"] == {"trace_id": report.run_id.hex}
    assert client.entered == 1
    assert client.exited == 1

    expected_metrics = [
        (case, metric)
        for case in report.cases
        for metric in case.metrics
    ]
    assert len(client.score_calls) == len(expected_metrics)
    assert [score["name"] for score in client.score_calls] == [
        f"{case.case_key}.{metric.metric_id}" for case, metric in expected_metrics
    ]
    assert all(score["trace_id"] == report.run_id.hex for score in client.score_calls)
    assert client.flushed == 1


def test_score_ids_are_stable_when_the_same_report_is_exported_again() -> None:
    report = _report()
    first_factory = FakeClientFactory()
    second_factory = FakeClientFactory()

    assert _reporter(first_factory).report(report) is True
    assert _reporter(second_factory).report(report) is True

    first_ids = [score["score_id"] for score in first_factory.clients[0].score_calls]
    second_ids = [score["score_id"] for score in second_factory.clients[0].score_calls]
    assert first_ids == second_ids
    assert all(first_ids)
    assert len(first_ids) == len(set(first_ids))


@pytest.mark.parametrize(
    "failure_point",
    ["factory", "start", "enter", "score", "exit", "flush"],
)
def test_export_failure_never_changes_or_raises_from_local_report(
    failure_point: str,
) -> None:
    report = _report()
    original = report.model_dump(mode="json")

    assert _reporter(FakeClientFactory(failure_point=failure_point)).report(report) is False

    assert report.model_dump(mode="json") == original


def test_report_payload_and_settings_dump_never_expose_secret() -> None:
    report = _report()
    factory = FakeClientFactory()
    settings = _settings(
        langfuse_enabled=True,
        langfuse_public_key=SecretStr(PUBLIC_KEY),
        langfuse_secret_key=SecretStr(SECRET_KEY),
    )

    assert _reporter(factory).report(report) is True

    report_payload = json.dumps(report.model_dump(mode="json"), sort_keys=True)
    settings_payload = json.dumps(settings.model_dump(mode="json"), sort_keys=True)
    export_payload = json.dumps(
        {
            "observation": factory.clients[0].observation_calls,
            "scores": factory.clients[0].score_calls,
        },
        sort_keys=True,
    )
    assert SECRET_KEY not in report_payload
    assert SECRET_KEY not in settings_payload
    assert SECRET_KEY not in export_payload
    assert "**********" in settings_payload
