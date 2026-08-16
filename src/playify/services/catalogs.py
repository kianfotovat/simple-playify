"""Metadata-only adapters for services that yt-dlp does not resolve directly."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlsplit

from ..config import Config
from ..messages import message
from .http_client import BROWSER_USER_AGENT, HttpClient

LOGGER = logging.getLogger(__name__)
UNKNOWN_ARTIST = message("track.unknown_artist")
UNKNOWN_TITLE = message("track.unknown_title")

SPOTIFY_HOSTS = {"open.spotify.com"}
DEEZER_HOSTS = {"deezer.com", "www.deezer.com", "link.deezer.com"}
APPLE_HOSTS = {"music.apple.com"}
TIDAL_HOSTS = {"tidal.com", "www.tidal.com", "listen.tidal.com"}
AMAZON_DOMAINS = {
    "music.amazon.com",
    "music.amazon.co.uk",
    "music.amazon.de",
    "music.amazon.fr",
    "music.amazon.it",
    "music.amazon.es",
    "music.amazon.co.jp",
    "music.amazon.ca",
    "music.amazon.com.au",
    "music.amazon.com.br",
    "music.amazon.com.mx",
    "music.amazon.in",
}
AMAZON_REGION = {
    "music.amazon.com": ("en_US", "USD", "America/Los_Angeles"),
    "music.amazon.co.uk": ("en_GB", "GBP", "Europe/London"),
    "music.amazon.de": ("de_DE", "EUR", "Europe/Berlin"),
    "music.amazon.fr": ("fr_FR", "EUR", "Europe/Paris"),
    "music.amazon.it": ("it_IT", "EUR", "Europe/Rome"),
    "music.amazon.es": ("es_ES", "EUR", "Europe/Madrid"),
    "music.amazon.co.jp": ("ja_JP", "JPY", "Asia/Tokyo"),
    "music.amazon.ca": ("en_CA", "CAD", "America/Toronto"),
    "music.amazon.com.au": ("en_AU", "AUD", "Australia/Sydney"),
    "music.amazon.com.br": ("pt_BR", "BRL", "America/Sao_Paulo"),
    "music.amazon.com.mx": ("es_MX", "MXN", "America/Mexico_City"),
    "music.amazon.in": ("en_IN", "INR", "Asia/Kolkata"),
}


class CatalogError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class CatalogItem:
    title: str
    artist: str
    source: str

    @property
    def search_query(self) -> str:
        return f"{self.title} {self.artist}".strip()


@dataclass(slots=True)
class CatalogResult:
    items: list[CatalogItem] = field(default_factory=list)
    partial_error: str | None = None


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def _clean(value: Any, fallback: str) -> str:
    text = html.unescape(str(value or "")).strip()
    return text or fallback


def _artist(value: dict[str, Any]) -> str:
    artists = value.get("artists") or []
    if artists and isinstance(artists[0], dict):
        return _clean(artists[0].get("name"), UNKNOWN_ARTIST)
    artist = value.get("artist") or value.get("byArtist") or {}
    if isinstance(artist, dict):
        return _clean(artist.get("name"), UNKNOWN_ARTIST)
    return _clean(value.get("artistName"), UNKNOWN_ARTIST)


class CatalogRouter:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def supports(self, url: str) -> bool:
        host = _host(url)
        return host in (
            SPOTIFY_HOSTS | DEEZER_HOSTS | APPLE_HOSTS | TIDAL_HOSTS | AMAZON_DOMAINS
        )

    async def resolve(self, url: str) -> CatalogResult:
        host = _host(url)
        if host in SPOTIFY_HOSTS:
            result = await self._spotify(url)
        elif host in DEEZER_HOSTS:
            result = await self._deezer(url)
        elif host in APPLE_HOSTS:
            result = await self._apple(url)
        elif host in TIDAL_HOSTS:
            result = await self._tidal(url)
        elif host in AMAZON_DOMAINS:
            result = await self._amazon(url)
        else:
            raise CatalogError("unsupported catalog URL")
        seen: set[tuple[str, str]] = set()
        unique: list[CatalogItem] = []
        for item in result.items:
            key = (item.title.casefold(), item.artist.casefold())
            if key not in seen:
                seen.add(key)
                unique.append(item)
        result.items = unique
        return result

    async def _spotify(self, url: str) -> CatalogResult:
        clean_url = url.split("?", 1)[0]
        items: list[CatalogItem] = []
        client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
        client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
        if client_id and client_secret:
            try:
                items = await asyncio.to_thread(
                    self._spotify_official, clean_url, client_id, client_secret
                )
                if items:
                    return CatalogResult(items)
            except Exception as exc:
                LOGGER.warning("Spotify API failed; trying HTTP scraper: %s", exc)
        try:
            fallback = await asyncio.to_thread(self._spotify_fallback, clean_url)
            if fallback:
                return CatalogResult(fallback)
        except Exception as exc:
            if items:
                return CatalogResult(items, str(exc))
            raise CatalogError("Spotify metadata could not be retrieved") from exc
        raise CatalogError("Spotify returned no tracks")

    @staticmethod
    def _spotify_official(
        url: str, client_id: str, client_secret: str
    ) -> list[CatalogItem]:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials

        client = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=client_id, client_secret=client_secret
            ),
            requests_timeout=30,
            retries=3,
        )
        path = urlsplit(url).path.strip("/").split("/")
        if path and path[0].startswith("intl-"):
            path = path[1:]
        if len(path) < 2:
            raise CatalogError("invalid Spotify URL")
        kind, identifier = path[0], path[1]
        result: list[CatalogItem] = []

        def append(track: dict[str, Any] | None) -> None:
            if track:
                result.append(
                    CatalogItem(_clean(track.get("name"), UNKNOWN_TITLE), _artist(track), "spotify")
                )

        if kind == "track":
            append(client.track(identifier))
        elif kind == "playlist":
            page = client.playlist_items(identifier, limit=100)
            while page:
                for value in page.get("items", []):
                    append(value.get("track") if value else None)
                page = client.next(page) if page.get("next") else None
        elif kind == "album":
            page = client.album_tracks(identifier, limit=50)
            while page:
                for value in page.get("items", []):
                    append(value)
                page = client.next(page) if page.get("next") else None
        elif kind == "artist":
            for track in client.artist_top_tracks(identifier).get("tracks", []):
                append(track)
        else:
            raise CatalogError(f"unsupported Spotify resource: {kind}")
        return result

    @staticmethod
    def _spotify_fallback(url: str) -> list[CatalogItem]:
        """Use spotifyscraper's HTTP client without browser/Selenium extras."""

        try:
            from spotify_scraper import SpotifyClient
        except ImportError:
            try:
                from spotifyscraper import SpotifyClient  # type: ignore[no-redef]
            except ImportError as exc:
                raise CatalogError("spotifyscraper is not installed") from exc

        client = SpotifyClient()
        path = urlsplit(url).path.strip("/").split("/")
        if path and path[0].startswith("intl-"):
            path = path[1:]
        if len(path) < 2:
            raise CatalogError("invalid Spotify URL")
        kind = path[0]
        if kind == "playlist":
            raw = client.get_playlist(url, max_tracks=None)
        elif kind == "album":
            raw = client.get_album(url)
        elif kind == "track":
            raw = client.get_track(url)
        else:
            raise CatalogError(f"Spotify scraper does not support {kind}")
        data = raw.to_dict() if hasattr(raw, "to_dict") else raw
        tracks = data.get("tracks", []) if isinstance(data, dict) else []
        if kind == "track" and isinstance(data, dict):
            tracks = [data]
        result: list[CatalogItem] = []
        for entry in tracks:
            track = entry.get("track", entry) if isinstance(entry, dict) else None
            if isinstance(track, dict):
                result.append(
                    CatalogItem(_clean(track.get("name"), UNKNOWN_TITLE), _artist(track), "spotify")
                )
        return result

    async def _paged_json(
        self,
        url: str,
        consume: Callable[[dict[str, Any]], list[CatalogItem]],
        next_key: str = "next",
    ) -> CatalogResult:
        items: list[CatalogItem] = []
        current: str | None = url
        try:
            while current:
                page = await self.http.get_json(current)
                if not isinstance(page, dict):
                    raise CatalogError("catalog returned invalid JSON")
                items.extend(consume(page))
                current = page.get(next_key)
        except Exception as exc:
            if items:
                return CatalogResult(items, str(exc))
            raise
        return CatalogResult(items)

    async def _deezer(self, url: str) -> CatalogResult:
        if _host(url) == "link.deezer.com":
            url = await self.http.resolve(url)
        path = [part for part in urlsplit(url).path.strip("/").split("/") if part]
        if path and len(path[0]) == 2:
            path = path[1:]
        if len(path) < 2:
            raise CatalogError("invalid Deezer URL")
        kind, identifier = path[0], path[1]
        api = "https://api.deezer.com"

        def consume(page: dict[str, Any]) -> list[CatalogItem]:
            return [
                CatalogItem(
                    _clean(track.get("title"), UNKNOWN_TITLE),
                    _artist(track),
                    "deezer",
                )
                for track in page.get("data", [])
                if isinstance(track, dict)
            ]

        if kind == "track":
            track = await self.http.get_json(f"{api}/track/{quote(identifier)}")
            if not isinstance(track, dict) or track.get("error"):
                raise CatalogError("Deezer track was not found")
            return CatalogResult(
                [CatalogItem(_clean(track.get("title"), UNKNOWN_TITLE), _artist(track), "deezer")]
            )
        if kind == "playlist":
            return await self._paged_json(f"{api}/playlist/{quote(identifier)}/tracks", consume)
        if kind == "album":
            return await self._paged_json(f"{api}/album/{quote(identifier)}/tracks", consume)
        if kind == "artist":
            return await self._paged_json(f"{api}/artist/{quote(identifier)}/top?limit=100", consume)
        raise CatalogError(f"unsupported Deezer resource: {kind}")

    async def _apple(self, url: str) -> CatalogResult:
        document = await self.http.get_text(url)
        match = re.search(
            r'<script[^>]+id=["\']serialized-server-data["\'][^>]*>(.*?)</script>',
            document,
            re.DOTALL | re.IGNORECASE,
        )
        if not match:
            raise CatalogError("Apple Music page contained no serialized metadata")
        data = json.loads(html.unescape(match.group(1)))
        target = parse_qs(urlsplit(url).query).get("i", [None])[0]
        found: list[CatalogItem] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                title = value.get("title") or value.get("name")
                artist = value.get("artistName")
                descriptor = value.get("contentDescriptor", {})
                identifier = (
                    descriptor.get("identifiers", {}).get("storeAdamID")
                    if isinstance(descriptor, dict)
                    else None
                )
                if title and artist and (target is None or str(identifier) == str(target)):
                    found.append(CatalogItem(_clean(title, UNKNOWN_TITLE), _clean(artist, UNKNOWN_ARTIST), "apple"))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(data)
        if not found:
            raise CatalogError("Apple Music returned no tracks")
        return CatalogResult(found)

    async def _tidal(self, url: str) -> CatalogResult:
        parts = [part for part in urlsplit(url).path.strip("/").split("/") if part]
        supported = {"playlist", "album", "mix", "track"}
        kind = identifier = None
        for index, part in enumerate(parts[:-1]):
            if part in supported:
                kind, identifier = part, parts[index + 1]
                break
        if not kind or not identifier:
            raise CatalogError("invalid Tidal URL")
        country = str(Config.get("tidal_country", "US")).upper()
        headers = {"x-tidal-token": "txNoH4kkV41MfH25", "Accept": "application/json"}
        items: list[CatalogItem] = []
        offset = 0
        try:
            while True:
                if kind == "track":
                    endpoint = f"https://api.tidal.com/v1/tracks/{quote(identifier)}?countryCode={country}"
                else:
                    plural = "mixes" if kind == "mix" else f"{kind}s"
                    endpoint = (
                        f"https://api.tidal.com/v1/{plural}/{quote(identifier)}/items"
                        f"?offset={offset}&limit=100&countryCode={country}"
                    )
                data = await self.http.get_json(endpoint, headers=headers)
                if kind == "track":
                    raw_items = [data]
                else:
                    raw_items = data.get("items", []) if isinstance(data, dict) else []
                if not raw_items:
                    break
                for value in raw_items:
                    track = value.get("item", value) if isinstance(value, dict) else {}
                    if track.get("title"):
                        items.append(CatalogItem(_clean(track.get("title"), UNKNOWN_TITLE), _artist(track), "tidal"))
                if kind == "track":
                    break
                offset += len(raw_items)
                total = data.get("totalNumberOfItems", offset)
                if offset >= total:
                    break
        except Exception as exc:
            if items:
                return CatalogResult(items, str(exc))
            raise CatalogError("Tidal metadata could not be retrieved") from exc
        if not items:
            raise CatalogError("Tidal returned no tracks")
        return CatalogResult(items)

    async def _amazon(self, url: str) -> CatalogResult:
        host = _host(url)
        if host not in AMAZON_DOMAINS:
            raise CatalogError("unsupported Amazon Music domain")
        locale, currency, timezone = AMAZON_REGION[host]
        config = await self.http.get_json(
            f"https://{host}/config.json",
            headers={"Accept": "application/json", "Referer": f"https://{host}/"},
        )
        csrf = config.get("csrf", {})
        path = urlsplit(url).path
        if urlsplit(url).query:
            path += "?" + urlsplit(url).query
        inner = {
            "x-amzn-authentication": json.dumps(
                {
                    "interface": "ClientAuthenticationInterface.v1_0.ClientTokenElement",
                    "accessToken": "",
                }
            ),
            "x-amzn-device-model": "WEBPLAYER",
            "x-amzn-device-family": "WebPlayer",
            "x-amzn-device-id": config.get("deviceId", ""),
            "x-amzn-user-agent": BROWSER_USER_AGENT,
            "x-amzn-session-id": config.get("sessionId", ""),
            "x-amzn-request-id": str(uuid.uuid4()),
            "x-amzn-device-language": locale,
            "x-amzn-currency-of-preference": currency,
            "x-amzn-device-time-zone": timezone,
            "x-amzn-application-version": config.get("version", "1.0"),
            "x-amzn-timestamp": str(int(time.time() * 1000)),
            "x-amzn-csrf": json.dumps(
                {
                    "interface": "CSRFInterface.v1_0.CSRFHeaderElement",
                    "token": csrf.get("token", ""),
                    "timestamp": csrf.get("ts", csrf.get("timestamp", "")),
                    "rndNonce": csrf.get("rnd", csrf.get("rndNonce", "")),
                }
            ),
            "x-amzn-music-domain": host,
            "x-amzn-page-url": url,
        }
        payload = json.dumps(
            {
                "deeplink": json.dumps(
                    {
                        "interface": "DeeplinkInterface.v1_0.DeeplinkClientInformation",
                        "deeplink": path,
                    }
                ),
                "headers": json.dumps(inner),
            }
        )
        result = await self.http.request(
            "POST",
            "https://eu.web.skill.music.a2z.com/api/showHome",
            data=payload,
            headers={
                "Content-Type": "text/plain;charset=UTF-8",
                "Accept": "*/*",
                "Origin": f"https://{host}",
                "Referer": f"https://{host}/",
            },
        )
        data = result.json()
        items: list[CatalogItem] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                title = value.get("primaryText") or value.get("trackName")
                artist = value.get("secondaryText1") or value.get("artistName")
                if isinstance(title, dict):
                    title = title.get("text")
                if isinstance(artist, dict):
                    artist = artist.get("text")
                if title and artist:
                    items.append(
                        CatalogItem(
                            re.sub(r"\s*\[Explicit\]\s*", "", _clean(title, UNKNOWN_TITLE), flags=re.I),
                            _clean(artist, UNKNOWN_ARTIST),
                            "amazon",
                        )
                    )
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
            elif isinstance(value, str) and value.startswith('{"@context":"https://schema.org"'):
                try:
                    walk(json.loads(value))
                except json.JSONDecodeError:
                    pass

        walk(data)
        if not items:
            raise CatalogError("Amazon Music returned no tracks")
        return CatalogResult(items)
