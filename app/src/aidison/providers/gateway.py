from __future__ import annotations

from enum import StrEnum
from typing import Literal

from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI
from pydantic import AliasChoices, Field, SecretStr

from aidison.config import AidisonSettings
from aidison.providers.model_gateway import ModelTarget


class ProviderName(StrEnum):
    DEEPSEEK = "deepseek"
    OPENCODE_GO = "opencode-go"


class ProviderUnavailableError(RuntimeError):
    pass


class ProviderSettings(AidisonSettings):
    yaml_section = "model"

    provider: ProviderName = Field(
        default=ProviderName.DEEPSEEK,
        validation_alias="AIDISON_MODEL_PROVIDER",
    )
    deepseek_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"),
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices("DEEPSEEK_BASE_URL", "DASHSCOPE_BASE_URL"),
    )
    deepseek_model: str = Field(
        default="deepseek-v4-pro",
        validation_alias=AliasChoices("DEEPSEEK_MODEL", "DASHSCOPE_MODEL"),
    )
    deepseek_thinking: Literal["enabled", "disabled"] = Field(
        default="disabled",
        validation_alias="DEEPSEEK_THINKING",
    )
    request_timeout_seconds: float = Field(
        default=600,
        validation_alias="MODEL_REQUEST_TIMEOUT_SECONDS",
        ge=5,
        le=600,
    )
    opencode_go_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENCODE_API_KEY", "Opencode_API_KEY"),
    )
    opencode_go_base_url: str = Field(
        default="https://opencode.ai/zen/go/v1",
        validation_alias="OPENCODE_GO_BASE_URL",
    )
    opencode_go_model: str = Field(
        default="mimo-v2.5",
        validation_alias="OPENCODE_GO_MODEL",
    )


def build_chat_model(
    settings: ProviderSettings | None = None,
    *,
    temperature: float = 0,
    max_tokens: int | None = None,
    thinking: Literal["enabled", "disabled"] | None = None,
) -> ChatOpenAI:
    resolved = settings or ProviderSettings()
    if resolved.provider is ProviderName.DEEPSEEK:
        if resolved.deepseek_api_key is None:
            raise ProviderUnavailableError("DEEPSEEK_API_KEY is required for DeepSeek")
        api_key = resolved.deepseek_api_key
        base_url = resolved.deepseek_base_url
        model = resolved.deepseek_model
    elif resolved.provider is ProviderName.OPENCODE_GO:
        if resolved.opencode_go_api_key is None:
            raise ProviderUnavailableError("OPENCODE_API_KEY is required for OpenCode Go")
        api_key = resolved.opencode_go_api_key
        base_url = resolved.opencode_go_base_url
        model = resolved.opencode_go_model
    else:  # pragma: no cover - StrEnum and Pydantic prevent unsupported providers.
        raise ProviderUnavailableError(f"unsupported model provider: {resolved.provider}")
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        # LangChain supports max_tokens; the installed type stub omits it.
        max_tokens=max_tokens,  # type: ignore[call-arg]
        timeout=resolved.request_timeout_seconds,
        max_retries=0,
        extra_body={"thinking": {"type": thinking or resolved.deepseek_thinking}}
        if resolved.provider is ProviderName.DEEPSEEK
        else None,
    )


def build_opencode_go_model() -> ChatOpenAI:
    """Build the independent MiMo model used for read-only proposal review."""
    return build_chat_model(ProviderSettings(provider=ProviderName.OPENCODE_GO))


def build_deepseek_responses_client(
    settings: ProviderSettings | None = None,
) -> AsyncOpenAI:
    """Build the DeepSeek Responses client used by schema-constrained planning.

    OpenCode Go remains on the chat-compatible path because its Responses API
    compatibility is not part of Aidison's provider contract.
    """

    resolved = settings or ProviderSettings()
    if resolved.provider is not ProviderName.DEEPSEEK:
        raise ProviderUnavailableError(
            "structured research planning requires the DeepSeek Responses API"
        )
    if resolved.deepseek_api_key is None:
        raise ProviderUnavailableError("DEEPSEEK_API_KEY is required for DeepSeek")
    return AsyncOpenAI(
        api_key=resolved.deepseek_api_key.get_secret_value(),
        base_url=resolved.deepseek_base_url,
        timeout=resolved.request_timeout_seconds,
        max_retries=0,
    )


def build_model_target(
    settings: ProviderSettings | None = None,
    *,
    quota_group: str,
) -> ModelTarget:
    """Freeze one configured provider/model pair for a durable invocation."""

    resolved = settings or ProviderSettings()
    model = (
        resolved.deepseek_model
        if resolved.provider is ProviderName.DEEPSEEK
        else resolved.opencode_go_model
    )
    return ModelTarget(
        provider=resolved.provider.value,
        model=model,
        revision="configured-v1",
        credential_pool_id=f"{resolved.provider.value}-default",
        quota_group=quota_group,
        capabilities=("structured_output",),
    )
