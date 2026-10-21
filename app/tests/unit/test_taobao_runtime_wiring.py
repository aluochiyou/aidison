"""Tests for Taobao search-only runtime wiring and integration-health declaration."""

from __future__ import annotations

import httpx
import pytest

from aidison.api.app import build_default_shopping_provider, create_app
from aidison.providers.taobao import TaobaoAffiliateAdapter


def _write_config(tmp_path, *, provider: str = "none", adzone_id: str = "") -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "shopping:\n"
        f"  provider: {provider}\n"
        "taobao:\n"
        f'  adzone_id: "{adzone_id}"\n'
        "  search_timeout_seconds: 3.0\n"
        "  max_search_results: 5\n",
        encoding="utf-8",
    )
    assert config.is_file()


def test_default_provider_selection_is_none(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path, provider="none", adzone_id="zone-1")

    assert build_default_shopping_provider() is None


def test_taobao_selection_builds_search_only_provider(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path, provider="taobao", adzone_id="zone-1")
    monkeypatch.setenv("TAOBAO_APP_KEY", "env-key")
    monkeypatch.setenv("TAOBAO_APP_SECRET", "env-secret")

    provider = build_default_shopping_provider()
    assert isinstance(provider, TaobaoAffiliateAdapter)
    assert provider.available is True
    caps = provider.capabilities
    assert caps.search is True
    assert caps.handoff_kinds == frozenset()


def test_taobao_selection_without_secrets_is_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path, provider="taobao", adzone_id="zone-1")
    monkeypatch.delenv("TAOBAO_APP_KEY", raising=False)
    monkeypatch.delenv("TAOBAO_APP_SECRET", raising=False)

    provider = build_default_shopping_provider()
    assert isinstance(provider, TaobaoAffiliateAdapter)
    assert provider.available is False


@pytest.mark.asyncio
async def test_integration_health_declares_search_only_state() -> None:
    api = create_app(
        shopping_provider=TaobaoAffiliateAdapter(app_key="k", app_secret="s", adzone_id="a")
    )
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/integration-health")
        assert response.status_code == 200
        shopping = response.json()["shopping"]
        assert shopping["provider"] == "taobao"
        assert shopping["available"] is True
        assert shopping["search"] is True
        assert shopping["handoff_kinds"] == []


@pytest.mark.asyncio
async def test_integration_health_declares_disabled_without_provider() -> None:
    api = create_app()
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/integration-health")
        assert response.status_code == 200
        shopping = response.json()["shopping"]
        assert shopping["provider"] == "none"
        assert shopping["available"] is False
        assert shopping["search"] is False
        assert shopping["handoff_kinds"] == []
