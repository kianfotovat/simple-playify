"""Atomic settings and installation metadata stores."""

from __future__ import annotations

import json
import logging
import os
import shutil
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from .constants import INSTALLATION_PATH, SETTINGS_PATH, ensure_runtime_dirs

LOGGER = logging.getLogger(__name__)

DEFAULT_SETTINGS: dict[str, Any] = {
    "persistence_mode": "full",
    "updates_enabled": True,
    "tidal_country": "US",
    "soundcloud_fallback": True,
    "private_media_allowlist": [],
    "ip_mode": "auto",
    "youtube_clients": ["web", "android", "ios"],
    "worker_count": "auto",
    "http_concurrency": "auto",
    "tui_refresh": "auto",
    "color_mode": "auto",
    "symbol_mode": "auto",
    "controller_idle_image": "https://i.imgur.com/vDusBWD.png",
    "bot_status_type": "none",
    "bot_status_text": "",
}

DEFAULT_INSTALLATION: dict[str, Any] = {
    "last_dependency_check": None,
    "dependency_snooze_until": None,
    "last_ffmpeg_check": None,
    "update_remind_after": None,
    "ignored_update_sha": None,
    "previous_update_sha": None,
    "last_update_sha": None,
    "pending_environment": None,
}


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class JsonStore:
    """A JSON store that preserves unknown keys and writes atomically."""

    def __init__(self, path: Path, defaults: Mapping[str, Any]) -> None:
        ensure_runtime_dirs()
        self.path = path
        self.defaults = deepcopy(dict(defaults))
        self._lock = RLock()
        self._values = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return deepcopy(self.defaults)
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if not isinstance(loaded, dict):
                raise ValueError("the JSON root must be an object")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            backup = self.path.with_name(f"{self.path.stem}.invalid-{_timestamp()}.json")
            try:
                shutil.copy2(self.path, backup)
            except OSError:
                LOGGER.exception("Could not back up invalid settings file %s", self.path)
            LOGGER.error("Ignoring invalid JSON in %s: %s", self.path, exc)
            return deepcopy(self.defaults)
        merged = deepcopy(self.defaults)
        merged.update(loaded)
        return merged

    def reload(self) -> None:
        with self._lock:
            self._values = self._load()

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._values)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return deepcopy(self._values.get(key, default))

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._values[key] = deepcopy(value)
            _atomic_json_write(self.path, self._values)

    def update(self, values: Mapping[str, Any]) -> None:
        with self._lock:
            self._values.update(deepcopy(dict(values)))
            _atomic_json_write(self.path, self._values)

    def save(self) -> None:
        with self._lock:
            _atomic_json_write(self.path, self._values)


class SettingsManager(JsonStore):
    def __init__(self, path: Path = SETTINGS_PATH) -> None:
        super().__init__(path, DEFAULT_SETTINGS)

    def validate(self) -> list[str]:
        """Return validation errors while leaving forward-compatible keys alone."""

        errors: list[str] = []
        if self.get("persistence_mode") not in {"full", "settings"}:
            errors.append("persistence_mode must be 'full' or 'settings'")
        if self.get("ip_mode") not in {"auto", "ipv4"}:
            errors.append("ip_mode must be 'auto' or 'ipv4'")
        country = self.get("tidal_country")
        if not isinstance(country, str) or len(country) != 2 or not country.isalpha():
            errors.append("tidal_country must be a two-letter country code")
        private_hosts = self.get("private_media_allowlist")
        if not isinstance(private_hosts, list) or not all(
            isinstance(item, str) for item in private_hosts
        ):
            errors.append("private_media_allowlist must be a list of hosts, IPs, or CIDRs")
        clients = self.get("youtube_clients")
        if not isinstance(clients, list) or not all(isinstance(item, str) for item in clients):
            errors.append("youtube_clients must be a list of yt-dlp client names")
        return errors


Config = SettingsManager()
Installation = JsonStore(INSTALLATION_PATH, DEFAULT_INSTALLATION)
