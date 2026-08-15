"""Discord client lifecycle for the V2 migration."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import discord
import psutil
from discord import app_commands
from dotenv import load_dotenv

from .commands import CommandSuite
from .config import Config
from .constants import PROJECT_ROOT, TEMP_DIR, display_version
from .discord_utils import Responses
from .logging_utils import configure_logging
from .messages import message
from .models import ServerSettings
from .services.extractor import Extractor
from .services.http_client import HttpClient
from .services.player import PlayerManager, PlayerSession, _human_count, ffmpeg_path
from .storage import Storage
from .ui.controller import ControllerManager

LOGGER = logging.getLogger(__name__)


class PlayifyCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        suite = getattr(self.client, "commands", None)
        return await suite.interaction_check(interaction) if suite else False

    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        await self.client.handle_command_error(interaction, error)  # type: ignore[attr-defined]


class PlayifyClient(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.tree = PlayifyCommandTree(self)
        self.storage = Storage()
        # ``discord.Client.http`` is Discord.py's authentication/REST client.
        # Keep Playify's media client under a distinct name so login can use it.
        self.media_http = HttpClient()
        self.extractor = Extractor(self.media_http)
        self.server_settings: dict[int, ServerSettings] = {}
        self.responses = Responses(self, self.storage)
        self.players = PlayerManager(
            self, self.storage, self.extractor, self._player_changed
        )
        self.controllers = ControllerManager(
            self, self.players, self.storage, self.responses
        )
        self.commands = CommandSuite(self)
        self.started_at = time.monotonic()
        self.ready_task: asyncio.Task[None] | None = None
        self.heartbeat_task: asyncio.Task[None] | None = None
        self.control_task: asyncio.Task[None] | None = None
        self._closing = False

    async def setup_hook(self) -> None:
        await self.storage.open()
        await self.media_http.open()
        self.server_settings = await self.storage.load_servers()
        await self.players.restore()
        control_id = os.getenv("PLAYIFY_CONTROL_ID", "").strip().lower()
        if len(control_id) == 32 and all(character in "0123456789abcdef" for character in control_id):
            control_path = TEMP_DIR / f"control-{control_id}.stop"
            self.control_task = asyncio.create_task(
                self._watch_control_file(control_path), name="supervisor-control"
            )

    async def on_ready(self) -> None:
        if self.ready_task is None:
            self.ready_task = asyncio.create_task(self._finish_startup(), name="finish-startup")

    async def _finish_startup(self) -> None:
        LOGGER.info("Logged in as %s with %s", self.user, display_version())
        await self.responses.restore_deletions()
        await self.controllers.startup_cleanup()
        for guild in self.guilds:
            settings = self.server_settings.setdefault(guild.id, ServerSettings(guild.id))
            await self.commands.prune_allowlist(guild, settings)
        if not await self._sync_commands():
            LOGGER.error("Discord command sync failed for 60 seconds; exiting cleanly")
            await self.close()
            return
        await self._set_presence()
        await self.players.auto_resume()
        self.heartbeat_task = asyncio.create_task(self._heartbeat(), name="heartbeat")

    async def _sync_commands(self) -> bool:
        deadline = asyncio.get_running_loop().time() + 60
        delay = 1.0
        while True:
            try:
                synced = await self.tree.sync()
                LOGGER.info("Synced %s global application commands", len(synced))
                return True
            except (discord.HTTPException, OSError) as exc:
                if asyncio.get_running_loop().time() + delay > deadline:
                    LOGGER.error("Command sync failed: %s", exc)
                    return False
                await asyncio.sleep(delay)
                delay = min(10.0, delay * 2)

    async def _set_presence(self) -> None:
        text = str(Config.get("bot_status_text", "")).strip()
        kind = str(Config.get("bot_status_type", "none")).lower()
        types = {
            "playing": discord.ActivityType.playing,
            "listening": discord.ActivityType.listening,
            "watching": discord.ActivityType.watching,
            "competing": discord.ActivityType.competing,
        }
        activity = discord.Activity(type=types[kind], name=text) if text and kind in types else None
        await self.change_presence(activity=activity)

    async def _player_changed(self, session: PlayerSession, event: str) -> None:
        await self.controllers.on_player_change(session, event)
        self._emit(
            {
                "type": "player",
                "event": event,
                "guild_id": session.guild_id,
                "active": session.active,
                "dormant": session.state.dormant,
                "queued": len(session.state.queue),
                "pending": len(session.state.pending),
                "track": session.state.current.title if session.state.current else None,
            }
        )

    def _emit(self, payload: dict[str, Any]) -> None:
        try:
            print("PLAYIFY_EVENT " + json.dumps(payload, ensure_ascii=False), flush=True)
        except (OSError, ValueError):
            # The bot may briefly outlive an interrupted TUI or terminal pipe.
            # File logging and Discord operation should continue independently.
            pass

    async def _heartbeat(self) -> None:
        process = psutil.Process()
        while not self.is_closed():
            try:
                active = sum(1 for session in self.players.sessions.values() if session.active)
                dormant = sum(1 for session in self.players.sessions.values() if session.state.dormant)
                queued = sum(len(session.state.queue) for session in self.players.sessions.values())
                try:
                    ffmpeg = ffmpeg_path()
                except RuntimeError:
                    ffmpeg = None
                self._emit(
                    {
                        "type": "heartbeat",
                        "version": display_version(),
                        "uptime": int(time.monotonic() - self.started_at),
                        "memory": process.memory_info().rss,
                        "servers": len(self.guilds),
                        "players": active,
                        "dormant": dormant,
                        "queued": queued,
                        "ffmpeg": ffmpeg,
                        "cache": len(self.extractor.success_cache),
                    }
                )
            except Exception:
                LOGGER.exception("Could not emit heartbeat")
            await asyncio.sleep(30)

    async def _watch_control_file(self, path: Path) -> None:
        try:
            while not self.is_closed():
                if path.exists():
                    path.unlink(missing_ok=True)
                    await self.close()
                    return
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            pass
        except OSError:
            LOGGER.exception("Supervisor control file failed")
        finally:
            path.unlink(missing_ok=True)

    async def handle_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandInvokeError):
            original = error.original
        else:
            original = error
        if isinstance(original, (discord.NotFound, app_commands.CheckFailure)):
            if not interaction.response.is_done():
                await self.responses.send(
                    interaction, message("command.expired"), lifetime="error"
                )
            return
        if isinstance(original, ValueError):
            LOGGER.warning("Rejected command input: %s", original)
            key = "voice.empty" if "empty" in str(original).lower() else "player.request_failed"
            await self.responses.send(interaction, message(key), lifetime="error")
            return
        await self.responses.unexpected(interaction, original)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self.controllers.raw_delete(payload.channel_id, payload.message_id)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if not self.user:
            return
        session = self.players.sessions.get(member.guild.id)
        if session is None:
            return
        if member.id != self.user.id:
            active_channel = session.voice.channel if session.voice else None
            if (
                isinstance(active_channel, (discord.VoiceChannel, discord.StageChannel))
                and before.channel is not None
                and before.channel.id == active_channel.id
                and (after.channel is None or after.channel.id != active_channel.id)
                and _human_count(active_channel) == 0
            ):
                await session.become_dormant("external_empty_move")
            return
        if after.channel is None:
            if time.monotonic() <= session.expected_disconnect_until:
                session.expected_disconnect_until = 0.0
                return
            await session.become_dormant("external_kick")
            return
        if before.channel and before.channel.id != after.channel.id:
            if _human_count(after.channel) == 0:
                await session.become_dormant("external_empty_move")
                return
            session.state.voice_channel_id = after.channel.id
            session.state.text_channel_id = after.channel.id
            await session.changed("external_move")
        me = member.guild.me
        permissions = after.channel.permissions_for(me) if me else None
        if not permissions or not permissions.connect or not permissions.speak:
            if session.voice and session.voice.is_playing():
                await session.pause()
            await session.changed("voice_permission_lost")
            await session.recover_voice()

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        settings = self.server_settings.get(channel.guild.id)
        if settings and channel.id in settings.allowlist:
            settings.allowlist.discard(channel.id)
            await self.storage.save_server(settings)

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        current = asyncio.current_task()
        lifecycle_tasks = [
            task
            for task in (self.ready_task, self.heartbeat_task, self.control_task)
            if task is not None and task is not current and not task.done()
        ]
        for task in lifecycle_tasks:
            task.cancel()
        if lifecycle_tasks:
            await asyncio.gather(*lifecycle_tasks, return_exceptions=True)
        await self.players.shutdown()
        await self.controllers.shutdown()
        await self.responses.close()
        await self.extractor.close()
        await self.media_http.close()
        await self.storage.close()
        await super().close()


def run_bot() -> None:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    configure_logging(bot_process=True)
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        LOGGER.error("DISCORD_TOKEN is missing")
        raise SystemExit(2)
    client = PlayifyClient()
    client.run(token, log_handler=None)
