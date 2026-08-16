"""Atomic settings and installation metadata stores."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any

from .constants import INSTALLATION_PATH, SETTINGS_PATH
from .messages import message


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
    """A runtime JSON store for files initialized by bootstrap.py."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()
        self._values = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if not isinstance(loaded, dict):
                raise ValueError("the JSON root must be an object")  # noqa: TRY004 - invalid JSON value shape
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(message("config.unavailable", path=self.path)) from exc
        return loaded

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
        super().__init__(path)

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
        if not isinstance(private_hosts, list) or not all(isinstance(item, str) for item in private_hosts):
            errors.append("private_media_allowlist must be a list of hosts, IPs, or CIDRs")
        clients = self.get("youtube_clients")
        if not isinstance(clients, list) or not all(isinstance(item, str) for item in clients):
            errors.append("youtube_clients must be a list of yt-dlp client names")
        return errors


Config = SettingsManager()
Installation = JsonStore(INSTALLATION_PATH)
