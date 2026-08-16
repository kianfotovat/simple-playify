"""Shared bounded HTTP client with retry and SSRF protections."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import random
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

import aiohttp

from ..config import Config, Installation
from ..constants import HTTP_USER_AGENT

LOGGER = logging.getLogger(__name__)

CHROME_STABLE_VERSION_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_STABLE"
)
FALLBACK_CHROME_MAJOR = 151
CHROME_VERSION_TIMEOUT_SECONDS = 5


def browser_user_agent(chrome_major: int) -> str:
    """Construct Chrome's reduced desktop user-agent string."""
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_major}.0.0.0 Safari/537.36"
    )


def cached_chrome_major() -> int:
    value = Installation.get("last_chrome_major", FALLBACK_CHROME_MAJOR)
    if isinstance(value, int) and 100 <= value <= 999:
        return value
    return FALLBACK_CHROME_MAJOR


async def current_browser_user_agent() -> str:
    """Resolve the current stable Chrome major without making startup depend on it."""
    chrome_major = cached_chrome_major()
    try:
        timeout = aiohttp.ClientTimeout(total=CHROME_VERSION_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": HTTP_USER_AGENT},
            raise_for_status=True,
            trust_env=False,
        ) as session:
            async with session.get(CHROME_STABLE_VERSION_URL) as response:
                version = (await response.text()).strip()
        parts = version.split(".")
        if len(parts) != 4 or not all(part.isdigit() for part in parts):
            raise ValueError(f"invalid Chrome version: {version!r}")
        resolved_major = int(parts[0])
        if not 100 <= resolved_major <= 999:
            raise ValueError(f"invalid Chrome major version: {resolved_major}")
        chrome_major = resolved_major
        if chrome_major != cached_chrome_major():
            try:
                Installation.set("last_chrome_major", chrome_major)
            except OSError as exc:
                LOGGER.warning("Could not cache Chrome %s: %s", chrome_major, exc)
        LOGGER.info("Using Chrome %s user agent from the stable release feed", chrome_major)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        LOGGER.warning(
            "Could not resolve the stable Chrome version; using Chrome %s: %s",
            chrome_major,
            exc,
        )
    return browser_user_agent(chrome_major)


TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
NEVER_ALLOWED = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("100.100.100.200"),
}


class UnsafeUrlError(ValueError):
    pass


class HttpStatusError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = "") -> None:
        super().__init__(f"HTTP {status} from {url}")
        self.status = status
        self.url = url
        self.body = body


def automatic_http_concurrency() -> int:
    cores = os.cpu_count() or 1
    return 2 if cores <= 2 else 4 if cores <= 6 else 8


def configured_http_concurrency() -> int:
    value = Config.get("http_concurrency", "auto")
    if value == "auto":
        return automatic_http_concurrency()
    try:
        return max(1, min(16, int(value)))
    except (TypeError, ValueError):
        return automatic_http_concurrency()


def _allow_entries(values: Iterable[str]) -> tuple[set[str], list[ipaddress._BaseNetwork]]:
    hosts: set[str] = set()
    networks: list[ipaddress._BaseNetwork] = []
    for raw in values:
        value = raw.strip().lower().rstrip(".")
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            hosts.add(value)
    return hosts, networks


def _always_forbidden(address: ipaddress._BaseAddress) -> bool:
    return (
        address in NEVER_ALLOWED
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    )


def _address_allowed(
    address: ipaddress._BaseAddress,
    *,
    host_allowlisted: bool,
    networks: list[ipaddress._BaseNetwork],
) -> bool:
    if _always_forbidden(address):
        return False
    if address.is_global:
        return True
    return host_allowlisted or any(address in network for network in networks)


async def validate_url(
    url: str, allowlist: Iterable[str] | None = None
) -> tuple[str, tuple[ipaddress._BaseAddress, ...]]:
    """Reject unsafe schemes, credentials, and any disallowed DNS result."""

    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise UnsafeUrlError("only HTTP(S) URLs are supported")
    if parts.username or parts.password:
        raise UnsafeUrlError("URL credentials are not allowed")
    if not parts.hostname:
        raise UnsafeUrlError("URL has no hostname")

    hostname = parts.hostname.lower().rstrip(".")
    hosts, networks = _allow_entries(
        allowlist if allowlist is not None else Config.get("private_media_allowlist", [])
    )
    host_allowlisted = hostname in hosts
    try:
        addresses = (ipaddress.ip_address(hostname),)
    except ValueError:
        family = socket.AF_INET if Config.get("ip_mode") == "ipv4" else socket.AF_UNSPEC
        try:
            records = await asyncio.get_running_loop().getaddrinfo(
                hostname,
                parts.port or (443 if parts.scheme == "https" else 80),
                family=family,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise UnsafeUrlError(f"hostname could not be resolved: {hostname}") from exc
        addresses = tuple(
            dict.fromkeys(ipaddress.ip_address(record[4][0]) for record in records)
        )
    if not addresses:
        raise UnsafeUrlError("hostname resolved to no addresses")
    rejected = [
        address
        for address in addresses
        if not _address_allowed(address, host_allowlisted=host_allowlisted, networks=networks)
    ]
    if rejected:
        raise UnsafeUrlError(
            "hostname resolves to a blocked address: " + ", ".join(map(str, rejected))
        )
    return hostname, addresses


def _retry_delay(headers: aiohttp.typedefs.LooseHeaders, attempt: int) -> float:
    value = headers.get("Retry-After") if hasattr(headers, "get") else None
    if value:
        try:
            return min(60.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(value))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                delay = (retry_at - datetime.now(UTC)).total_seconds()
                return min(60.0, max(0.0, delay))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(8.0, (2**attempt) + random.random())


@dataclass(slots=True)
class HttpResult:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        import json

        return json.loads(self.body)


class HttpClient:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None
        self.semaphore = asyncio.Semaphore(configured_http_concurrency())
        self.browser_user_agent = browser_user_agent(cached_chrome_major())
        self._browser_user_agent_resolved = False

    async def open(self) -> None:
        if self.session is not None and not self.session.closed:
            return
        if not self._browser_user_agent_resolved:
            self.browser_user_agent = await current_browser_user_agent()
            self._browser_user_agent_resolved = True
        family = socket.AF_INET if Config.get("ip_mode") == "ipv4" else socket.AF_UNSPEC
        connector = aiohttp.TCPConnector(family=family, ttl_dns_cache=60)
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(connect=10, total=30),
            headers={"User-Agent": self.browser_user_agent},
            raise_for_status=False,
            trust_env=False,
        )

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def request(
        self,
        method: str,
        url: str,
        *,
        validate: bool = True,
        headers: dict[str, str] | None = None,
        data: Any = None,
        json_data: Any = None,
        max_redirects: int = 5,
    ) -> HttpResult:
        await self.open()
        assert self.session is not None
        current = url
        redirects = 0
        for attempt in range(4):
            try:
                while True:
                    if validate:
                        await validate_url(current)
                    async with self.semaphore:
                        async with self.session.request(
                            method,
                            current,
                            headers=headers,
                            data=data,
                            json=json_data,
                            allow_redirects=False,
                        ) as response:
                            body = await response.content.read(MAX_RESPONSE_BYTES + 1)
                            if len(body) > MAX_RESPONSE_BYTES:
                                raise HttpStatusError(
                                    response.status,
                                    str(response.url),
                                    "response exceeded 16 MiB",
                                )
                            response_headers = {key: value for key, value in response.headers.items()}
                            status = response.status
                            final_url = str(response.url)

                    if status in {301, 302, 303, 307, 308}:
                        location = response_headers.get("Location")
                        if not location:
                            raise HttpStatusError(status, final_url, body[:500].decode(errors="replace"))
                        redirects += 1
                        if redirects > max_redirects:
                            raise UnsafeUrlError("too many redirects")
                        current = urljoin(final_url, location)
                        if status == 303:
                            method, data, json_data = "GET", None, None
                        continue

                    if status in TRANSIENT_STATUS and attempt < 3:
                        await asyncio.sleep(_retry_delay(response_headers, attempt))
                        break
                    if status < 200 or status >= 300:
                        raise HttpStatusError(status, final_url, body[:500].decode(errors="replace"))
                    if validate:
                        await validate_url(final_url)
                    return HttpResult(final_url, status, response_headers, body)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt >= 3:
                    raise
                LOGGER.warning("Transient HTTP failure for %s: %s", current, exc)
                await asyncio.sleep(min(8.0, (2**attempt) + random.random()))
        raise RuntimeError("unreachable HTTP retry state")

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        return (await self.request("GET", url, **kwargs)).json()

    async def get_text(self, url: str, **kwargs: Any) -> str:
        return (await self.request("GET", url, **kwargs)).text()

    async def resolve(self, url: str, **kwargs: Any) -> str:
        return (await self.request("HEAD", url, **kwargs)).url
