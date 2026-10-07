import pytest
from pydantic import SecretStr

from aidison.providers.gateway import (
    ProviderName,
    ProviderSettings,
    ProviderUnavailableError,
    build_chat_model,
    build_deepseek_responses_client,
)


def test_deepseek_fails_closed_without_key() -> None:
    with pytest.raises(ProviderUnavailableError, match="DEEPSEEK_API_KEY"):
        build_chat_model(
            ProviderSettings(
                provider=ProviderName.DEEPSEEK,
                deepseek_api_key=None,
            )
        )


def test_deepseek_build_uses_official_route() -> None:
    model = build_chat_model(ProviderSettings(deepseek_api_key=SecretStr("test-key")))
    assert model.model_name == "deepseek-v4-pro"
    assert model.openai_api_base == "https://api.deepseek.com"
    assert model.max_retries == 0
    assert model.request_timeout == 600
    assert model.extra_body == {"thinking": {"type": "disabled"}}


def test_deepseek_research_call_can_explicitly_enable_thinking() -> None:
    model = build_chat_model(
        ProviderSettings(deepseek_api_key=SecretStr("test-key")),
        thinking="enabled",
    )

    assert model.extra_body == {"thinking": {"type": "enabled"}}


def test_deepseek_responses_client_uses_same_pinned_route_and_timeout() -> None:
    client = build_deepseek_responses_client(
        ProviderSettings(
            deepseek_api_key=SecretStr("test-key"),
            deepseek_base_url="https://example.invalid",
            request_timeout_seconds=123,
        )
    )

    assert str(client.base_url) == "https://example.invalid"
    assert client.max_retries == 0


def test_responses_client_rejects_provider_without_responses_contract() -> None:
    with pytest.raises(ProviderUnavailableError, match="Responses API"):
        build_deepseek_responses_client(
            ProviderSettings(
                provider=ProviderName.OPENCODE_GO,
                opencode_go_api_key=SecretStr("test-key"),
            )
        )


def test_deepseek_model_uses_explicit_openai_compatible_route() -> None:
    model = build_chat_model(
        ProviderSettings(
            provider=ProviderName.DEEPSEEK,
            deepseek_api_key=SecretStr("test-key"),
            deepseek_base_url="https://example.invalid/v1",
            deepseek_model="qwen-plus",
        )
    )
    assert model.model_name == "qwen-plus"
    assert model.openai_api_base == "https://example.invalid/v1"
    assert model.max_retries == 0
    assert model.extra_body == {"thinking": {"type": "disabled"}}


def test_opencode_go_fails_closed_without_key() -> None:
    with pytest.raises(ProviderUnavailableError, match="OPENCODE_API_KEY"):
        build_chat_model(
            ProviderSettings(
                provider=ProviderName.OPENCODE_GO,
                opencode_go_api_key=None,
            )
        )


def test_opencode_go_env_overrides_route_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_GO_BASE_URL", "https://mimo.example.invalid/v1")
    monkeypatch.setenv("OPENCODE_GO_MODEL", "mimo-v3-test")

    model = build_chat_model(
        ProviderSettings(
            provider=ProviderName.OPENCODE_GO,
            opencode_go_api_key=SecretStr("test-key"),
        )
    )

    assert model.model_name == "mimo-v3-test"
    assert model.openai_api_base == "https://mimo.example.invalid/v1"


def test_opencode_go_builds_via_env_selected_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDISON_MODEL_PROVIDER", "opencode-go")
    monkeypatch.setenv("OPENCODE_API_KEY", "env-key")

    model = build_chat_model()

    assert model.model_name == "mimo-v2.5"
    assert model.openai_api_base == "https://opencode.ai/zen/go/v1"


def test_opencode_go_mimo_uses_official_openai_compatible_route() -> None:
    model = build_chat_model(
        ProviderSettings(
            provider=ProviderName.OPENCODE_GO,
            opencode_go_api_key=SecretStr("test-key"),
        )
    )

    assert model.model_name == "mimo-v2.5"
    assert model.openai_api_base == "https://opencode.ai/zen/go/v1"
    assert model.max_retries == 0
