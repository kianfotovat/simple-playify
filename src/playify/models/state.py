"""Serializable playback and server state."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

Provenance = Literal["user", "autoplay"]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return uuid4().hex


def safe_descriptor(value: str) -> str:
    """Strip credentials, query strings, and fragments before persistence."""

    if not value.lower().startswith(("http://", "https://")):
        return value
    parts = urlsplit(value)
    hostname = parts.hostname or ""
    netloc = hostname
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


@dataclass(slots=True)
class Track:
    title: str
    webpage_url: str
    source: str = "unknown"
    uploader: str = "Unknown artist"
    duration: float | None = None
    is_live: bool = False
    thumbnail: str | None = None
    stream_url: str | None = None
    requested_by: int | None = None
    provenance: Provenance = "user"
    occurrence_id: str = field(default_factory=new_id)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        # Stream URLs are short-lived and commonly contain credentials.
        value["stream_url"] = None
        value["webpage_url"] = safe_descriptor(self.webpage_url)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Track":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value[key] for key in allowed if key in value})


@dataclass(slots=True)
class PendingImport:
    query: str
    priority: bool = False
    requested_by: int | None = None
    import_id: str = field(default_factory=new_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": safe_descriptor(self.query),
            "priority": self.priority,
            "requested_by": self.requested_by,
            "import_id": self.import_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PendingImport":
        return cls(
            query=str(value.get("query", "")),
            priority=bool(value.get("priority", False)),
            requested_by=value.get("requested_by"),
            import_id=str(value.get("import_id") or new_id()),
        )


@dataclass(slots=True)
class PlayerSnapshot:
    guild_id: int
    voice_channel_id: int | None = None
    text_channel_id: int | None = None
    controller_channel_id: int | None = None
    controller_message_id: int | None = None
    current: Track | None = None
    queue: list[Track] = field(default_factory=list)
    history: list[Track] = field(default_factory=list)
    pending: list[PendingImport] = field(default_factory=list)
    position: float = 0.0
    paused: bool = False
    dormant: bool = False
    loop_current: bool = False
    autoplay_enabled: bool = False
    volume: int = 100
    updated_at: str = field(default_factory=utc_now)

    def touch(self) -> None:
        self.updated_at = utc_now()


@dataclass(slots=True)
class ServerSettings:
    guild_id: int
    allowlist: set[int] = field(default_factory=set)
    channel_move_mode: Literal["allow", "protect"] = "allow"
