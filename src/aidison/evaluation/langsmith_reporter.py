"""Optional LangSmith evaluation reporter (fail-closed by default).

Construction is side-effect free: it never imports the ``langsmith`` SDK eagerly
and never opens a network connection.  A real client is only built inside
``report()``, which the caller explicitly enables with a complete configuration
(``langsmith_enabled`` + project + API key).  Missing or partial configuration
keeps the reporter disabled, and any reporting failure is swallowed so a broken
observability adapter can never fail the evaluation itself.  API keys are held on
the instance only and never appear in evaluation reports or config dumps.
"""

from __future__ import annotations

from typing import Any, Protocol

from aidison.evaluation.contracts import EvaluationReport


class EvaluationReporter(Protocol):
    """Persistence boundary for one completed evaluation report."""

    name: str

    @property
    def enabled(self) -> bool: ...

    def report(self, report: EvaluationReport) -> None: ...


class NoopEvaluationReporter:
    """Sink used whenever no enabled reporter is available."""

    name = "none"

    @property
    def enabled(self) -> bool:
        return False

    def report(self, report: EvaluationReport) -> None:
        return None


class LangSmithEvaluationReporter:
    """Report one evaluation run to LangSmith as a run plus per-case feedback.

    The optional ``client_factory`` lets tests inject a fake client so the real
    network path is never exercised without an explicit call.
    """

    name = "langsmith"

    def __init__(
        self,
        *,
        project_name: str,
        api_key: str,
        api_url: str = "https://api.smith.langchain.com",
        client_factory: Any | None = None,
    ) -> None:
        self._project_name = project_name
        self._api_key = api_key
        self._api_url = api_url
        self._client_factory = client_factory if client_factory is not None else _default_client
        self._client: Any = None

    @property
    def enabled(self) -> bool:
        return bool(self._api_key and self._project_name.strip())

    def report(self, report: EvaluationReport) -> None:
        if not self.enabled:
            return
        try:
            self._send(report)
        except Exception:
            # Fail-closed: evaluation results must never be lost to observability.
            return

    def _send(self, report: EvaluationReport) -> None:
        client = (
            self._client
            if self._client is not None
            else self._client_factory(api_key=self._api_key, api_url=self._api_url)
        )
        self._client = client
        run_id = str(report.run_id)
        summary_payload = report.summary.model_dump(mode="json")
        client.create_run(
            name="aidison-evaluation",
            id=run_id,
            run_type="chain",
            project_name=self._project_name,
            inputs={
                "mode": report.mode.value,
                "schema_version": report.schema_version,
                "runner_version": report.runner_version,
            },
            output={"summary": summary_payload},
            tags=["aidison-evaluation", f"mode:{report.mode.value}"],
        )
        for case in report.cases:
            client.create_feedback(
                run_id=run_id,
                key="case_status",
                score=1.0 if case.status.value == "passed" else 0.0,
                value=case.status.value,
                comment=case.case_key,
            )
        client.flush()


def _default_client(*, api_key: str, api_url: str) -> Any:
    from langsmith import Client  # lazy import: no import-time network

    return Client(api_key=api_key, api_url=api_url, hide_outputs=True)
