"""Configuration for the deterministic evaluation suite and its reporter."""

from __future__ import annotations

from pydantic import AliasChoices, Field, SecretStr

from aidison.config import AidisonSettings
from aidison.evaluation.langsmith_reporter import (
    EvaluationReporter,
    LangSmithEvaluationReporter,
)


class EvaluationSettings(AidisonSettings):
    """Load env secrets (fail-closed unless fully configured).

    No YAML section is used: the reporter defaults live in code and the API key
    stays in the environment, keeping evaluation wiring out of ``config.yaml``.
    """

    langsmith_enabled: bool = Field(
        default=False,
        validation_alias="AIDISON_EVAL_LANGSMITH_ENABLED",
    )
    langsmith_project: str = Field(
        default="aidison-evaluation",
        validation_alias="AIDISON_EVAL_LANGSMITH_PROJECT",
        max_length=200,
    )
    langsmith_api_url: str = Field(
        default="https://api.smith.langchain.com",
        validation_alias="AIDISON_EVAL_LANGSMITH_API_URL",
    )
    langsmith_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("AIDISON_EVAL_LANGSMITH_API_KEY", "LANGSMITH_API_KEY"),
    )


def build_evaluation_reporter(settings: EvaluationSettings) -> EvaluationReporter | None:
    """Return a LangSmith reporter only when explicitly enabled and complete.

    Missing or partial configuration returns ``None`` (disabled), never a client
    that could send traffic.
    """
    if not settings.langsmith_enabled:
        return None
    api_key = settings.langsmith_api_key.get_secret_value() if settings.langsmith_api_key else None
    if not api_key or not settings.langsmith_project.strip():
        return None
    return LangSmithEvaluationReporter(
        project_name=settings.langsmith_project,
        api_key=api_key,
        api_url=settings.langsmith_api_url,
    )
