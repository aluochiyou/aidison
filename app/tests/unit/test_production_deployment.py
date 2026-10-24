from __future__ import annotations

from pathlib import Path

import yaml

APP_ROOT = Path(__file__).resolve().parents[2]


def _production_compose() -> dict[str, object]:
    value = yaml.safe_load((APP_ROOT / "compose.production.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_production_compose_exposes_only_loopback_gateway() -> None:
    compose = _production_compose()
    services = compose["services"]
    assert isinstance(services, dict)

    for service_name in ("postgres", "api", "worker", "web"):
        service = services[service_name]
        assert isinstance(service, dict)
        assert "ports" not in service

    gateway = services["gateway"]
    assert isinstance(gateway, dict)
    assert gateway["ports"] == ["127.0.0.1:${AIDISON_EDGE_PORT:-8080}:8080"]
    assert gateway["cap_drop"] == ["ALL"]
    assert gateway["cap_add"] == ["NET_BIND_SERVICE"]

    networks = compose["networks"]
    assert isinstance(networks, dict)
    assert networks["app-internal"] == {"internal": True}


def test_public_connector_reaches_the_gateway() -> None:
    compose = _production_compose()
    services = compose["services"]
    assert isinstance(services, dict)
    cloudflared = services["cloudflared"]
    assert isinstance(cloudflared, dict)
    assert cloudflared["profiles"] == ["cloudflare"]
    assert cloudflared["depends_on"] == {"gateway": {"condition": "service_healthy"}}
    assert cloudflared["networks"] == ["edge"]
    assert "--token" not in cloudflared["command"]
    assert cloudflared["environment"] == {"TUNNEL_TOKEN": "${CLOUDFLARE_TUNNEL_TOKEN:-}"}

    caddyfile = (APP_ROOT / "ops" / "Caddyfile.production").read_text(encoding="utf-8")
    assert "reverse_proxy web:3000" in caddyfile
    assert 'X-Frame-Options "DENY"' in caddyfile


def test_caddy_keeps_the_only_health_route_outside_the_business_handler() -> None:
    caddyfile = (APP_ROOT / "ops" / "Caddyfile.production").read_text(encoding="utf-8")

    # The loopback health probe is intentionally the only unauthenticated path.
    assert "@edge_health path /edge-health" in caddyfile
    assert "@business not path /edge-health" in caddyfile
    assert "basic_auth @business" in caddyfile
    assert "handle @edge_health" in caddyfile
    assert caddyfile.count("\thandle ") == 3
    assert "@api path /api/* /health" in caddyfile
    assert "reverse_proxy api:8000" in caddyfile
    assert caddyfile.count("reverse_proxy ") == 2

    business_handler = caddyfile.split("\thandle {", maxsplit=1)[1]
    assert "basic_auth" not in business_handler
    assert "reverse_proxy web:3000" in business_handler


def test_production_initializer_requires_an_explicit_access_mode_choice() -> None:
    initializer = (APP_ROOT / "ops" / "initialize-production.ps1").read_text(encoding="utf-8")

    assert '[ValidateSet("CloudflareAccess", "OriginBasicAuth")]' in initializer
    assert '[string]$AccessMode = "CloudflareAccess"' in initializer
    assert 'if ($AccessMode -eq "CloudflareAccess")' in initializer
    assert "OriginBasicAuth mode is for a single trusted operator only" in initializer


def test_production_scripts_support_private_tailscale_without_a_tunnel_token() -> None:
    initializer = (APP_ROOT / "ops" / "initialize-production.ps1").read_text(encoding="utf-8")
    deployer = (APP_ROOT / "ops" / "deploy-production.ps1").read_text(encoding="utf-8")

    assert '[ValidateSet("Tailscale", "CloudflareTunnel")]' in initializer
    assert 'if ($PublishMode -eq "CloudflareTunnel")' in initializer
    assert '[ValidateSet("Tailscale", "CloudflareTunnel")]' in deployer
    assert '$requiredKeys += "CLOUDFLARE_TUNNEL_TOKEN"' in deployer


def test_deployer_resolves_relative_files_from_the_app_directory() -> None:
    deployer = (APP_ROOT / "ops" / "deploy-production.ps1").read_text(encoding="utf-8")

    assert "$appRoot = Split-Path -Parent $PSScriptRoot" in deployer
    assert '$composeFile = Join-Path $appRoot "compose.production.yaml"' in deployer
    assert "$EnvFile = Join-Path $appRoot $EnvFile" in deployer


def test_existing_private_deployment_can_add_cloudflare_tunnel_without_reinitializing() -> None:
    enabler = (APP_ROOT / "ops" / "enable-cloudflare-tunnel.ps1").read_text(encoding="utf-8-sig")

    assert "Cloudflare Tunnel Token（不回显）" in enabler
    assert "CLOUDFLARE_TUNNEL_TOKEN=$tunnelToken" in enabler
    assert "-PublishMode CloudflareTunnel" in enabler


def test_admin_password_reset_recreates_only_the_gateway() -> None:
    resetter = (APP_ROOT / "ops" / "reset-production-admin-password.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "新管理员密码（至少 16 个字符，不回显）" in resetter
    assert "AIDISON_ADMIN_PASSWORD_HASH='$hash'" in resetter
    assert "--no-deps --force-recreate gateway" in resetter
    assert (
        '[IO.File]::WriteAllText($hashInputPath, "$password`n", [Text.UTF8Encoding]::new($false))'
        in resetter
    )
    assert "/run/secrets/aidison-admin-password:ro" in resetter
    assert "Remove-Item -LiteralPath $hashInputPath -Force" in resetter
    assert "Get-Command curl.exe" in resetter
    assert 'url = "http://127.0.0.1:8080/"' in resetter
    assert 'noproxy = "*"' in resetter
    assert "last HTTP status" in resetter
    assert "Administrator password updated and verified locally" in resetter


def test_module_visual_key_script_uses_hidden_prompt_and_restarts_only_its_worker() -> None:
    configurator = (
        APP_ROOT / "ops" / "configure-module-visual-key.ps1"
    ).read_text(encoding="utf-8-sig")

    assert 'Read-Host "DashScope API Key（不回显）" -AsSecureString' in configurator
    assert "Write-Host $apiKey" not in configurator
    assert "--force-recreate module-visual-worker" in configurator
    assert "No secret value was printed" in configurator


def test_firecrawl_search_script_uses_hidden_prompt_and_restarts_only_research_worker() -> None:
    configurator = (APP_ROOT / "ops" / "configure-firecrawl-search.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert 'Read-Host "Firecrawl API Key（不回显）" -AsSecureString' in configurator
    assert "Write-Host $apiKey" not in configurator
    assert "AIDISON_WEB_SEARCH_PROVIDER=firecrawl" in configurator
    assert "--force-recreate worker" in configurator
    assert "No secret value was printed" in configurator
