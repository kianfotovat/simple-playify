"""Single-connection asynchronous persistence for Playify."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .config import Config
from .constants import BACKUP_DIR, DATABASE_PATH, ensure_runtime_dirs
from .models import PendingImport, PlayerSnapshot, ServerSettings, Track

LOGGER = logging.getLogger(__name__)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    channel_move_mode TEXT NOT NULL DEFAULT 'allow'
        CHECK (channel_move_mode IN ('allow', 'protect'))
);
CREATE TABLE IF NOT EXISTS allowlist (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id),
    FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS player_states (
    guild_id INTEGER PRIMARY KEY,
    voice_channel_id INTEGER,
    text_channel_id INTEGER,
    controller_channel_id INTEGER,
    controller_message_id INTEGER,
    current_json TEXT,
    queue_json TEXT NOT NULL,
    history_json TEXT NOT NULL,
    pending_json TEXT NOT NULL,
    playback_position REAL NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0,
    dormant INTEGER NOT NULL DEFAULT 0,
    loop_current INTEGER NOT NULL DEFAULT 0,
    autoplay_enabled INTEGER NOT NULL DEFAULT 0,
    volume INTEGER NOT NULL DEFAULT 100,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deletion_jobs (
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    delete_after TEXT NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY (channel_id, message_id)
);
CREATE TABLE IF NOT EXISTS controller_cleanup (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode_list(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    value = json.loads(raw)
    return value if isinstance(value, list) else []


class Storage:
    """Own the application's sole SQLite connection."""

    def __init__(self, path: Path = DATABASE_PATH) -> None:
        self.path = path
        self.connection: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def open(self) -> None:
        ensure_runtime_dirs()
        try:
            await self._connect_and_check()
        except (aiosqlite.DatabaseError, OSError, ValueError) as exc:
            LOGGER.error("Database is unusable; preserving it and starting fresh: %s", exc)
            await self.close()
            if self.path.exists():
                BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                backup = BACKUP_DIR / f"playify-corrupt-{stamp}.db"
                shutil.move(self.path, backup)
            await self._connect_and_check(fresh=True)

        assert self.connection is not None
        await self.connection.executescript(SCHEMA)
        await self.connection.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', '1')"
        )
        if Config.get("persistence_mode") == "settings":
            await self.connection.execute("DELETE FROM player_states")
        await self.connection.commit()

    async def _connect_and_check(self, *, fresh: bool = False) -> None:
        if fresh:
            self.path.unlink(missing_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        cursor = await self.connection.execute("PRAGMA quick_check")
        row = await cursor.fetchone()
        await cursor.close()
        if row is None or row[0] != "ok":
            raise ValueError(f"SQLite quick_check returned {row[0] if row else 'no result'}")
        await self.connection.execute("PRAGMA journal_mode = WAL")
        await self.connection.execute("PRAGMA synchronous = NORMAL")
        await self.connection.execute("PRAGMA busy_timeout = 5000")

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    def _db(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("Storage is not open")
        return self.connection

    async def load_servers(self) -> dict[int, ServerSettings]:
        db = self._db()
        cursor = await db.execute(
            "SELECT g.guild_id, g.channel_move_mode, a.channel_id "
            "FROM guild_settings g LEFT JOIN allowlist a ON a.guild_id = g.guild_id "
            "ORDER BY g.guild_id, a.channel_id"
        )
        values: dict[int, ServerSettings] = {}
        async for row in cursor:
            state = values.setdefault(
                row["guild_id"],
                ServerSettings(row["guild_id"], channel_move_mode=row["channel_move_mode"]),
            )
            if row["channel_id"] is not None:
                state.allowlist.add(row["channel_id"])
        await cursor.close()
        return values

    async def save_server(self, state: ServerSettings) -> None:
        async with self._write_lock:
            db = self._db()
            await db.execute(
                "INSERT INTO guild_settings(guild_id, channel_move_mode) VALUES(?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_move_mode=excluded.channel_move_mode",
                (state.guild_id, state.channel_move_mode),
            )
            await db.execute("DELETE FROM allowlist WHERE guild_id = ?", (state.guild_id,))
            await db.executemany(
                "INSERT INTO allowlist(guild_id, channel_id) VALUES(?, ?)",
                [(state.guild_id, channel_id) for channel_id in sorted(state.allowlist)],
            )
            await db.commit()

    async def load_players(self) -> dict[int, PlayerSnapshot]:
        if Config.get("persistence_mode") != "full":
            return {}
        cursor = await self._db().execute("SELECT * FROM player_states ORDER BY guild_id")
        values: dict[int, PlayerSnapshot] = {}
        async for row in cursor:
            try:
                current_raw = json.loads(row["current_json"]) if row["current_json"] else None
                values[row["guild_id"]] = PlayerSnapshot(
                    guild_id=row["guild_id"],
                    voice_channel_id=row["voice_channel_id"],
                    text_channel_id=row["text_channel_id"],
                    controller_channel_id=row["controller_channel_id"],
                    controller_message_id=row["controller_message_id"],
                    current=Track.from_dict(current_raw) if current_raw else None,
                    queue=[Track.from_dict(item) for item in _decode_list(row["queue_json"])],
                    history=[Track.from_dict(item) for item in _decode_list(row["history_json"])],
                    pending=[PendingImport.from_dict(item) for item in _decode_list(row["pending_json"])],
                    position=row["playback_position"],
                    paused=bool(row["paused"]),
                    dormant=bool(row["dormant"]),
                    loop_current=bool(row["loop_current"]),
                    autoplay_enabled=bool(row["autoplay_enabled"]),
                    volume=row["volume"],
                    updated_at=row["updated_at"],
                )
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                LOGGER.exception("Skipping invalid persisted player for guild %s", row["guild_id"])
        await cursor.close()
        return values

    async def save_player(self, state: PlayerSnapshot) -> None:
        if Config.get("persistence_mode") != "full":
            return
        state.touch()
        values = (
            state.guild_id,
            state.voice_channel_id,
            state.text_channel_id,
            state.controller_channel_id,
            state.controller_message_id,
            _json(state.current.to_dict()) if state.current else None,
            _json([track.to_dict() for track in state.queue]),
            _json([track.to_dict() for track in state.history]),
            _json([pending.to_dict() for pending in state.pending]),
            state.position,
            state.paused,
            state.dormant,
            state.loop_current,
            state.autoplay_enabled,
            state.volume,
            state.updated_at,
        )
        async with self._write_lock:
            await self._db().execute(
                """INSERT INTO player_states(
                    guild_id, voice_channel_id, text_channel_id, controller_channel_id,
                    controller_message_id, current_json, queue_json, history_json,
                    pending_json, playback_position, paused, dormant, loop_current,
                    autoplay_enabled, volume, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    voice_channel_id=excluded.voice_channel_id,
                    text_channel_id=excluded.text_channel_id,
                    controller_channel_id=excluded.controller_channel_id,
                    controller_message_id=excluded.controller_message_id,
                    current_json=excluded.current_json,
                    queue_json=excluded.queue_json,
                    history_json=excluded.history_json,
                    pending_json=excluded.pending_json,
                    playback_position=excluded.playback_position,
                    paused=excluded.paused,
                    dormant=excluded.dormant,
                    loop_current=excluded.loop_current,
                    autoplay_enabled=excluded.autoplay_enabled,
                    volume=excluded.volume,
                    updated_at=excluded.updated_at""",
                values,
            )
            await self._db().commit()

    async def delete_player(self, guild_id: int) -> None:
        async with self._write_lock:
            await self._db().execute("DELETE FROM player_states WHERE guild_id = ?", (guild_id,))
            await self._db().commit()

    async def set_controller_cleanup(self, guild_id: int, channel_id: int, message_id: int) -> None:
        async with self._write_lock:
            await self._db().execute(
                "INSERT OR REPLACE INTO controller_cleanup(guild_id, channel_id, message_id) "
                "VALUES(?, ?, ?)",
                (guild_id, channel_id, message_id),
            )
            await self._db().commit()

    async def pop_controller_cleanups(self) -> list[tuple[int, int, int]]:
        async with self._write_lock:
            cursor = await self._db().execute(
                "SELECT guild_id, channel_id, message_id FROM controller_cleanup"
            )
            rows = [(row[0], row[1], row[2]) for row in await cursor.fetchall()]
            await cursor.close()
            await self._db().execute("DELETE FROM controller_cleanup")
            await self._db().commit()
            return rows

    async def add_deletion_job(
        self, channel_id: int, message_id: int, delete_after: datetime, kind: str
    ) -> None:
        async with self._write_lock:
            await self._db().execute(
                "INSERT OR REPLACE INTO deletion_jobs(channel_id, message_id, delete_after, kind) "
                "VALUES(?, ?, ?, ?)",
                (channel_id, message_id, delete_after.astimezone(UTC).isoformat(), kind),
            )
            await self._db().commit()

    async def list_deletion_jobs(self) -> list[tuple[int, int, datetime, str]]:
        cursor = await self._db().execute(
            "SELECT channel_id, message_id, delete_after, kind FROM deletion_jobs"
        )
        rows = [
            (row[0], row[1], datetime.fromisoformat(row[2]), row[3])
            for row in await cursor.fetchall()
        ]
        await cursor.close()
        return rows

    async def remove_deletion_job(self, channel_id: int, message_id: int) -> None:
        async with self._write_lock:
            await self._db().execute(
                "DELETE FROM deletion_jobs WHERE channel_id = ? AND message_id = ?",
                (channel_id, message_id),
            )
            await self._db().commit()
