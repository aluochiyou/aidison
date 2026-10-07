"""Fail-safe Langfuse v4 exporter for deterministic Aidison evaluations.

The reporter sends only version identifiers, summaries and metric outcomes. It
never exports fixture inputs, prompts, source documents, credentials or hidden
reasoning.  Construction is side-effect free and the SDK is imported lazily.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid5

from aidison.evaluation.contracts import EvaluationReport, MetricStatus


class LangfuseEvaluationReporter:
    """Export one local report as an evaluator trace plus named scores."""

    name = "langfuse"

    def __init__(
        self,
        *,
        project_name: str,
        public_key: str,
        secret_key: str,
        base_url: str = "https://cloud.langfuse.com",
        environment: str = "development",
        release: str | None = None,
        client_factory: Any | None = None,
    ) -> None:
        self._project_name = project_name
        self._public_key = public_key
        self._secret_key = secret_key
        self._base_url = base_url
        self._environment = environment
        self._release = release
        self._client_factory = client_factory or _default_client
        self._client: Any = None

    @property
    def enabled(self) -> bool:
        return bool(
            self._project_name.strip()
            and self._public_key.strip()
            and self._secret_key.strip()
        )

    def report(self, report: EvaluationReport) -> bool:
        if not self.enabled:
            return False
        try:
            self._send(report)
        except Exception:
            # The local report is authoritative. Observability failure must not
            # change scores, publication gates or the evaluation exit status.
            return False
        return True

    def _send(self, report: EvaluationReport) -> None:
        client = self._client
        if client is None:
            client = self._client_factory(
                public_key=self._public_key,
                secret_key=self._secret_key,
                base_url=self._base_url,
                environment=self._environment,
                release=self._release,
            )
            self._client = client

        trace_id = report.run_id.hex
        manifest_hash = (
            report.fixture_manifest.content_hash if report.fixture_manifest is not None else None
        )
        fixture_set_key = (
            report.fixture_manifest.fixture_set_key
            if report.fixture_manifest is not None
            else None
        )
        fixture_set_revision = (
            report.fixture_manifest.fixture_set_revision
            if report.fixture_manifest is not None
            else None
        )
        with client.start_as_current_observation(
            trace_context={"trace_id": trace_id},
            name="aidison-evaluation",
            as_type="evaluator",
            input={
                "mode": report.mode.value,
                "schema_version": report.schema_version,
                "runner_version": report.runner_version,
                "fixture_set_key": fixture_set_key,
                "fixture_set_revision": fixture_set_revision,
                "fixture_manifest_hash": manifest_hash,
            },
            output=report.summary.model_dump(mode="json"),
            metadata={
                "project": self._project_name,
                "layers": [layer.value for layer in report.layers_covered],
                "case_count": len(report.cases),
                "fixture_set_key": fixture_set_key,
                "fixture_set_revision": fixture_set_revision,
            },
            version=report.runner_version,
        ):
            for case in report.cases:
                for index, metric in enumerate(case.metrics):
                    score_id = uuid5(
                        report.run_id,
                        f"{case.case_key}:{metric.metric_id}:{index}",
                    ).hex
                    client.create_score(
                        trace_id=trace_id,
                        score_id=score_id,
                        name=f"{case.case_key}.{metric.metric_id}",
                        value=1.0 if metric.status is MetricStatus.PASS else 0.0,
                        data_type="NUMERIC",
                        comment=case.case_key,
                        metadata={
                            "metric_status": metric.status.value,
                            "case_status": case.status.value,
                            "layer": case.layer.value,
                            "fixture_kind": case.fixture_kind.value,
                            "fixture_revision": case.fixture_revision,
                        },
                    )
        client.flush()


def _default_client(
    *,
    public_key: str,
    secret_key: str,
    base_url: str,
    environment: str,
    release: str | None,
) -> Any:
    """Construct the optional SDK with an explicit, type-checked boundary."""

    from langfuse import Langfuse

    return Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        base_url=base_url,
        environment=environment,
        release=release,
    )


__all__ = ["LangfuseEvaluationReporter"]
