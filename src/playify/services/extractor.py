"""Catalog routing and direct-stream yt-dlp extraction."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit, urlunsplit

import yt_dlp
from cachetools import TTLCache

from ..config import Config
from ..constants import COOKIE_DIR
from ..messages import message
from ..models import Track
from .catalogs import CatalogRouter
from .http_client import HttpClient, UnsafeUrlError, validate_url

LOGGER = logging.getLogger(__name__)

DIRECT_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a", ".mp4", ".webm", ".flac"}
CANONICAL_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "music.youtube.com",
    "soundcloud.com",
    "www.soundcloud.com",
    "twitch.tv",
    "www.twitch.tv",
    "bandcamp.com",
}
DEFINITIVE_MARKERS = (
    "private video",
    "video unavailable",
    "unsupported url",
    "not available",
    "does not exist",
    "copyright",
)
DIRECT_EXTENSION_ERROR = "direct media URLs must end with a supported extension"


class ResolveError(RuntimeError):
    pass


def _lower_worker_priority() -> None:
    """Keep metadata extraction from competing with voice playback."""

    try:
        import psutil

        process = psutil.Process()
        if os.name == "nt":
            process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            process.nice(5)
    except (ImportError, OSError, ValueError):
        pass


def automatic_worker_count() -> int:
    return max(1, min(4, (os.cpu_count() or 1) - 1))


def configured_worker_count() -> int:
    value = Config.get("worker_count", "auto")
    if value == "auto":
        return automatic_worker_count()
    try:
        return max(1, min(8, int(value)))
    except (TypeError, ValueError):
        return automatic_worker_count()


def sanitize_query(query: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", "", query)).strip()


def _is_url(value: str) -> bool:
    return urlsplit(value).scheme.lower() in {"http", "https"}


def _direct_extension(url: str) -> str:
    return Path(urlsplit(url).path).suffix.lower()


def _thumbnail_url(info: dict[str, Any]) -> str | None:
    thumbnail = info.get("thumbnail")
    if isinstance(thumbnail, str) and _is_url(thumbnail):
        return thumbnail

    candidates: list[tuple[float, int, str]] = []
    thumbnails = info.get("thumbnails")
    if not isinstance(thumbnails, list):
        return None
    for index, candidate in enumerate(thumbnails):
        if not isinstance(candidate, dict):
            continue
        url = candidate.get("url")
        if not isinstance(url, str) or not _is_url(url):
            continue
        try:
            area = float(candidate.get("width") or 0) * float(candidate.get("height") or 0)
        except (TypeError, ValueError):
            area = 0
        candidates.append((area, index, url))
    return max(candidates)[2] if candidates else None


def public_canonical_link(track: Track) -> str | None:
    """Return only links safe to expose as clickable Discord URLs."""

    parts = urlsplit(track.webpage_url)
    host = (parts.hostname or "").lower()
    if host not in CANONICAL_HOSTS and not host.endswith(".bandcamp.com"):
        return None
    if parts.username or parts.password:
        return None
    query = ""
    if host in {"youtube.com", "www.youtube.com", "music.youtube.com"}:
        video_id = parse_qs(parts.query).get("v", [None])[0]
        if video_id:
            query = f"v={video_id}"
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, query, ""))


class Extractor:
    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.catalogs = CatalogRouter(http)
        self.executor = ProcessPoolExecutor(max_workers=configured_worker_count(), initializer=_lower_worker_priority)
        self.semaphore = asyncio.Semaphore(configured_worker_count())
        self.success_cache: TTLCache[str, list[dict[str, Any]]] = TTLCache(maxsize=2_000, ttl=7_200)
        self.failure_cache: TTLCache[str, str] = TTLCache(maxsize=2_000, ttl=300)
        self._last_cookie_by_domain: dict[str, Path] = {}

    async def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _cookie_files(self, domain: str) -> list[Path]:
        if not COOKIE_DIR.exists():
            return []
        values: list[Path] = []
        for path in COOKIE_DIR.glob("*.txt"):
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as handle:
                    first = handle.readline().strip()
                if first in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}:
                    values.append(path)
            except OSError:
                LOGGER.warning("Could not inspect cookie file %s", path.name)
        random.shuffle(values)
        preferred = self._last_cookie_by_domain.get(domain)
        if preferred in values:
            values.remove(preferred)
            values.insert(0, preferred)
        return values

    def _options(self, cookie_file: Path | None = None, *, flat: bool = False) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist" if flat else False,
            "format": "bestaudio/best",
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 0,
            "ignoreerrors": True,
            "noplaylist": False,
        }
        if Config.get("ip_mode") == "ipv4":
            options["source_address"] = "0.0.0.0"
        # Let yt-dlp track YouTube's working default clients unless the user has explicitly configured an override.
        clients = Config.get("youtube_clients", [])
        if clients:
            options["extractor_args"] = {"youtube": {"player_client": list(clients)}}
        if cookie_file:
            options["cookiefile"] = str(cookie_file)
        return options

    @staticmethod
    def _extract_sync(target: str, options: dict[str, Any]) -> dict[str, Any] | None:
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(target, download=False)

    async def _extract(self, target: str, *, flat: bool = False) -> dict[str, Any]:
        domain = (urlsplit(target).hostname or "search").lower()
        loop = asyncio.get_running_loop()

        async def attempt(cookie_file: Path | None) -> dict[str, Any] | None:
            async with self.semaphore:
                return await loop.run_in_executor(
                    self.executor, self._extract_sync, target, self._options(cookie_file, flat=flat)
                )

        first_error: Exception | None = None
        try:
            value = await attempt(None)
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 - extractor failures are retried with available cookies
            first_error = exc
        for cookie_file in self._cookie_files(domain):
            try:
                value = await attempt(cookie_file)
                if value:
                    self._last_cookie_by_domain[domain] = cookie_file
                    return value
            except Exception as exc:  # noqa: BLE001 - each cookie candidate may fail independently
                first_error = first_error or exc
        if first_error:
            raise ResolveError(str(first_error)) from first_error
        raise ResolveError("yt-dlp returned no playable metadata")

    @staticmethod
    def _track(info: dict[str, Any], *, requested_by: int | None, provenance: str) -> Track:
        webpage_url = info.get("webpage_url") or info.get("original_url") or info.get("url") or ""
        extractor = str(info.get("extractor_key") or info.get("extractor") or "unknown").lower()
        if not _is_url(str(webpage_url)) and "youtube" in extractor and info.get("id"):
            webpage_url = f"https://www.youtube.com/watch?v={info['id']}"
        raw_stream = str(info.get("url") or "")
        stream_url = raw_stream if _is_url(raw_stream) and raw_stream != webpage_url else None
        return Track(
            title=str(info.get("title") or info.get("id") or message("track.unknown_title")),
            webpage_url=str(webpage_url),
            source=extractor,
            uploader=str(info.get("uploader") or info.get("artist") or message("track.unknown_artist")),
            duration=float(info["duration"]) if info.get("duration") is not None else None,
            is_live=bool(info.get("is_live") or info.get("live_status") == "is_live"),
            thumbnail=_thumbnail_url(info),
            stream_url=stream_url,
            requested_by=requested_by,
            provenance="autoplay" if provenance == "autoplay" else "user",
        )

    async def _direct(self, url: str, requested_by: int | None, provenance: str) -> Track:
        if _direct_extension(url) not in DIRECT_EXTENSIONS:
            raise ResolveError(DIRECT_EXTENSION_ERROR)
        await validate_url(url)
        try:
            final = await self.http.resolve(url)
        except Exception as exc:
            raise ResolveError("direct media URL could not be validated") from exc
        if _direct_extension(final) not in DIRECT_EXTENSIONS:
            raise ResolveError("the final direct media URL has no supported extension")
        title = Path(urlsplit(final).path).name or message("media.direct")
        return Track(
            title=title,
            webpage_url=final,
            stream_url=final,
            source="direct",
            requested_by=requested_by,
            provenance="autoplay" if provenance == "autoplay" else "user",
        )

    async def resolve_one(
        self,
        query: str,
        *,
        requested_by: int | None = None,
        provenance: Literal["user", "autoplay"] = "user",
        allow_playlist: bool = True,
    ) -> list[Track]:
        query = sanitize_query(query)
        if not query:
            raise ResolveError("the request is empty")
        if _is_url(query) and _direct_extension(query) in DIRECT_EXTENSIONS:
            return [await self._direct(query, requested_by, provenance)]
        is_url = _is_url(query)
        if is_url:
            host = (urlsplit(query).hostname or "").lower()
            if host != "youtu.be" and not any(
                host == domain or host.endswith("." + domain)
                for domain in ("youtube.com", "soundcloud.com", "twitch.tv", "bandcamp.com")
            ):
                raise ResolveError(DIRECT_EXTENSION_ERROR)
            try:
                await validate_url(query)
            except UnsafeUrlError as exc:
                raise ResolveError(str(exc)) from exc
        cache_key = f"{query}|{allow_playlist}"
        if cache_key in self.failure_cache:
            raise ResolveError(self.failure_cache[cache_key])
        if cache_key in self.success_cache:
            return [
                self._track(info, requested_by=requested_by, provenance=provenance)
                for info in self.success_cache[cache_key]
            ]
        target = query if is_url else f"ytsearch1:{query}"
        first_error: ResolveError | None = None
        try:
            data = await self._extract(target)
        except ResolveError as exc:
            first_error = exc
            if is_url or not Config.get("soundcloud_fallback", True):
                if any(marker in str(exc).lower() for marker in DEFINITIVE_MARKERS):
                    self.failure_cache[cache_key] = str(exc)
                raise
            data = await self._extract(f"scsearch1:{query}")
        entries = data.get("entries") if isinstance(data, dict) else None
        raw = [entry for entry in entries or [data] if isinstance(entry, dict)]
        if not raw and not is_url and Config.get("soundcloud_fallback", True):
            try:
                fallback = await self._extract(f"scsearch1:{query}")
                entries = fallback.get("entries") if isinstance(fallback, dict) else None
                raw = [entry for entry in entries or [fallback] if isinstance(entry, dict)]
            except ResolveError:
                pass
        if not allow_playlist and raw:
            raw = raw[:1]
        if not raw:
            if first_error:
                raise first_error
            raise ResolveError("no playable tracks were found")
        self.success_cache[cache_key] = raw
        return [self._track(info, requested_by=requested_by, provenance=provenance) for info in raw]

    async def resolve_request(
        self,
        request: str,
        *,
        requested_by: int | None = None,
        provenance: Literal["user", "autoplay"] = "user",
    ) -> tuple[list[Track], str | None]:
        request = sanitize_query(request)
        if _is_url(request) and self.catalogs.supports(request):
            catalog = await self.catalogs.resolve(request)
            tracks: list[Track] = []
            error = catalog.partial_error
            for item in catalog.items:
                try:
                    resolved = await self.resolve_one(
                        item.search_query,
                        requested_by=requested_by,
                        provenance=provenance,
                        allow_playlist=False,
                    )
                    tracks.extend(resolved[:1])
                except ResolveError as exc:
                    error = str(exc)
                    break
            return tracks, error
        return await self.resolve_one(request, requested_by=requested_by, provenance=provenance), None

    async def search(self, query: str, limit: int = 10) -> list[Track]:
        data = await self._extract(f"ytsearch{max(1, min(25, limit))}:{sanitize_query(query)}", flat=True)
        entries = [entry for entry in data.get("entries", []) if isinstance(entry, dict)]
        return [self._track(entry, requested_by=None, provenance="user") for entry in entries[:limit]]

    async def refresh_stream(self, track: Track) -> Track:
        cache_key = f"{sanitize_query(track.webpage_url)}|False"
        self.success_cache.pop(cache_key, None)
        self.failure_cache.pop(cache_key, None)
        tracks = await self.resolve_one(
            track.webpage_url,
            requested_by=track.requested_by,
            provenance=track.provenance,
            allow_playlist=False,
        )
        refreshed = tracks[0]
        refreshed.occurrence_id = track.occurrence_id
        return refreshed

    async def recommendations(self, seed: Track) -> list[Track]:
        primary: str | None = None
        parts = urlsplit(seed.webpage_url)
        host = (parts.hostname or "").lower()
        if host in {"youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be"}:
            video_id = parts.path.strip("/") if host == "youtu.be" else parse_qs(parts.query).get("v", [None])[0]
            if video_id:
                primary = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"

        seed_link = public_canonical_link(seed) or seed.webpage_url

        async def collect(target: str) -> list[Track]:
            try:
                data = await self._extract(target, flat=True)
            except ResolveError:
                LOGGER.exception("Recommendation source failed")
                return []
            entries = [entry for entry in data.get("entries", []) if isinstance(entry, dict)]
            generated: list[Track] = []
            seen: set[str] = set()
            for entry in entries[:50]:
                track = self._track(entry, requested_by=None, provenance="autoplay")
                link = public_canonical_link(track) or track.webpage_url
                if link and link != seed_link and link not in seen:
                    seen.add(link)
                    generated.append(track)
            return generated

        if primary:
            generated = await collect(primary)
            if generated:
                return generated
        if not Config.get("soundcloud_fallback", True):
            return []
        try:
            if "soundcloud" in seed.source:
                soundcloud_seed = await self._extract(seed.webpage_url, flat=True)
            else:
                soundcloud_seed = await self._extract(f"scsearch1:{seed.title} {seed.uploader}", flat=True)
            entries = soundcloud_seed.get("entries") or [soundcloud_seed]
            first = next((entry for entry in entries if isinstance(entry, dict)), None)
            if first and first.get("id"):
                return await collect(f"https://soundcloud.com/discover/sets/track-stations:{first['id']}")
        except ResolveError:
            LOGGER.info("SoundCloud recommendation fallback had no compatible seed")
        return []
