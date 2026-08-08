import pytest
from pydantic import SecretStr

from aidison.providers.gateway import (
    ProviderName,
    ProviderSettings,
    ProviderUnavailableError,
    build_chat_model,
)


def test_bailian_fails_closed_without_key() -> None:
    with pytest.raises(ProviderUnavailableError, match="DASHSCOPE_API_KEY"):
        build_chat_model(
            ProviderSettings(
                provider=ProviderName.BAILIAN,
                dashscope_api_key=None,
            )
        )


def test_bailian_model_uses_explicit_openai_compatible_route() -> None:
    model = build_chat_model(
        ProviderSettings(
            provider=ProviderName.BAILIAN,
            dashscope_api_key=SecretStr("test-key"),
            dashscope_base_url="https://example.invalid/v1",
            dashscope_model="qwen-plus",
        )
    )
    assert model.model_name == "qwen-plus"
    assert model.openai_api_base == "https://example.invalid/v1"
    assert model.max_retries == 0
