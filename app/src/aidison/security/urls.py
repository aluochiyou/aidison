"""Fail-closed HTTPS destination policy for T1 outbound reads."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit


class UnsafeUrlError(ValueError):
    pass


class HostResolver(Protocol):
    async def resolve(self, *, hostname: str, port: int) -> tuple[str, ...]: ...


class SystemHostResolver:
    async def resolve(self, *, hostname: str, port: int) -> tuple[str, ...]:
        try:
            parsed = ipaddress.ip_address(hostname)
            return (str(parsed),)
        except ValueError:
            pass
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise UnsafeUrlError("source hostname did not resolve") from exc
        return tuple(sorted({str(item[4][0]) for item in infos}))


@dataclass(frozen=True)
class ValidatedPublicUrl:
    normalized_url: str
    hostname: str
    port: int
    resolved_addresses: tuple[str, ...]


class PublicHttpsUrlPolicy:
    """Validate scheme, authority, canonical hostname and every DNS answer."""

    def __init__(self, *, resolver: HostResolver | None = None) -> None:
        self._resolver = resolver or SystemHostResolver()

    async def validate(self, url: str) -> ValidatedPublicUrl:
        if not url or len(url) > 4_000 or any(ord(char) < 32 for char in url):
            raise UnsafeUrlError("source URL is outside the bounded contract")
        parsed = urlsplit(url)
        hostname = self._canonical_hostname(parsed)
        port = parsed.port or 443
        if parsed.scheme.lower() != "https" or port != 443:
            raise UnsafeUrlError("source must use HTTPS on port 443")
        if parsed.username or parsed.password:
            raise UnsafeUrlError("source URL cannot contain credentials")
        addresses = await self._resolver.resolve(hostname=hostname, port=port)
        if not addresses:
            raise UnsafeUrlError("source hostname did not resolve")
        if any(not self._is_public_address(address) for address in addresses):
            raise UnsafeUrlError("source resolves to a non-public address")
        normalized = self._normalized_url(parsed, hostname=hostname)
        return ValidatedPublicUrl(
            normalized_url=normalized,
            hostname=hostname,
            port=port,
            resolved_addresses=tuple(sorted(addresses)),
        )

    @staticmethod
    def _canonical_hostname(parsed: SplitResult) -> str:
        if not parsed.hostname:
            raise UnsafeUrlError("source must include a hostname")
        try:
            return parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise UnsafeUrlError("source hostname is invalid") from exc

    @staticmethod
    def _is_public_address(address: str) -> bool:
        try:
            return ipaddress.ip_address(address).is_global
        except ValueError:
            return False

    @staticmethod
    def _normalized_url(parsed: SplitResult, *, hostname: str) -> str:
        host = f"[{hostname}]" if ":" in hostname else hostname
        path = parsed.path or "/"
        return urlunsplit(("https", host, path, parsed.query, ""))


__all__ = [
    "HostResolver",
    "PublicHttpsUrlPolicy",
    "SystemHostResolver",
    "UnsafeUrlError",
    "ValidatedPublicUrl",
]
