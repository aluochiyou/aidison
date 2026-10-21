"""Configuration for the deterministic evaluation suite and its reporter."""

from __future__ import annotations

from pydantic import AliasChoices, Field, SecretStr

from aidison.config import AidisonSettings
from aidison.evaluation.langfuse_reporter import LangfuseEvaluationReporter
from aidison.evaluation.langsmith_reporter import (
    LangSmithEvaluationReporter,
)
from aidison.evaluation.reporter import EvaluationReporter


class EvaluationSettings(AidisonSettings):
    """Load env secrets (fail-closed unless fully configured).

    No YAML section is used: the reporter defaults live in code and the API key
    stays in the environment, keeping evaluation wiring out of ``config.yaml``.
    """

    langfuse_enabled: bool = Field(
        default=False,
        validation_alias="AIDISON_EVAL_LANGFUSE_ENABLED",
    )
    langfuse_project: str = Field(
        default="aidison-evaluation",
        validation_alias="AIDISON_EVAL_LANGFUSE_PROJECT",
        max_length=200,
    )
    langfuse_public_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AIDISON_EVAL_LANGFUSE_PUBLIC_KEY",
            "LANGFUSE_PUBLIC_KEY",
        ),
    )
    langfuse_secret_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AIDISON_EVAL_LANGFUSE_SECRET_KEY",
            "LANGFUSE_SECRET_KEY",
        ),
    )
    langfuse_base_url: str = Field(
        default="https://cloud.langfuse.com",
        validation_alias=AliasChoices(
            "AIDISON_EVAL_LANGFUSE_BASE_URL",
            "LANGFUSE_BASE_URL",
        ),
    )
    langfuse_environment: str = Field(
        default="development",
        validation_alias="AIDISON_EVAL_LANGFUSE_ENVIRONMENT",
        max_length=100,
    )
    langfuse_release: str | None = Field(
        default=None,
        validation_alias="AIDISON_EVAL_LANGFUSE_RELEASE",
        max_length=200,
    )

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
    """Return an explicitly enabled reporter with Langfuse as the primary adapter.

    Missing or partial configuration returns ``None`` (disabled), never a client
    that could send traffic.
    """
    if settings.langfuse_enabled:
        public_key = (
            settings.langfuse_public_key.get_secret_value()
            if settings.langfuse_public_key
            else None
        )
        secret_key = (
            settings.langfuse_secret_key.get_secret_value()
            if settings.langfuse_secret_key
            else None
        )
        if not public_key or not secret_key or not settings.langfuse_project.strip():
            return None
        return LangfuseEvaluationReporter(
            project_name=settings.langfuse_project,
            public_key=public_key,
            secret_key=secret_key,
            base_url=settings.langfuse_base_url,
            environment=settings.langfuse_environment,
            release=settings.langfuse_release,
        )
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
