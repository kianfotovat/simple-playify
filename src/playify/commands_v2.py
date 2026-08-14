"""Slash-only command surface and channel policy."""

from __future__ import annotations

import logging
from typing import Any, Literal

import discord
from discord import app_commands

from .constants import display_version
from .discord_utils import format_time, safe_text
from .messages import message
from .models import ServerSettings, Track
from .services.player import PlayerSession, _human_count
from .ui.views import ChannelPaginator, QueueView, SearchView, SeekView

LOGGER = logging.getLogger(__name__)

VoiceChat = discord.VoiceChannel | discord.StageChannel
AllowedChannel = discord.TextChannel | discord.VoiceChannel | discord.StageChannel

READ_ONLY = {"queue", "nowplaying"}
SETUP_PREFIX = "setup"
GENERAL = {"status"}
MUTATIONS = {
    "play",
    "playnext",
    "search",
    "pause",
    "resume",
    "replay",
    "seek",
    "skip",
    "previous",
    "stop",
    "reconnect",
    "remove",
    "jumpto",
    "clearqueue",
    "shuffle",
    "loop",
    "autoplay",
    "volume",
}
WAKES_DORMANT = {"resume", "replay", "seek", "skip", "previous", "jumpto", "reconnect"}


def is_manager(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild


def voice_chat(interaction: discord.Interaction) -> VoiceChat | None:
    return interaction.channel if isinstance(interaction.channel, (discord.VoiceChannel, discord.StageChannel)) else None


def parse_timestamp(value: str) -> float:
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError("format")
    try:
        numbers = [int(part) for part in parts]
    except ValueError as exc:
        raise ValueError("format") from exc
    if any(number < 0 for number in numbers) or any(number >= 60 for number in numbers[1:]):
        raise ValueError("format")
    return float(sum(number * (60 ** index) for index, number in enumerate(reversed(numbers))))


class CommandSuite:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.tree: app_commands.CommandTree = app.tree
        self._register_commands()

    def _command(self, name: str, description: str, callback) -> None:
        command = self.tree.command(name=name, description=description)(callback)
        app_commands.guild_only()(command)
        if hasattr(app_commands, "allowed_installs"):
            app_commands.allowed_installs(guilds=True, users=False)(command)

    def _register_commands(self) -> None:
        definitions = (
            ("play", "Play a link or search, or append it to the queue", self.play),
            ("playnext", "Put a link or search next in the queue", self.playnext),
            ("search", "Choose a track from search results", self.search),
            ("pause", "Pause playback", self.pause),
            ("resume", "Resume playback, including a dormant session", self.resume),
            ("replay", "Replay the current finite track", self.replay),
            ("seek", "Seek to a timestamp or open collaborative seek controls", self.seek),
            ("skip", "Skip the current track", self.skip),
            ("previous", "Return to the previous track once", self.previous),
            ("stop", "Stop playback and clear the whole session", self.stop),
            ("reconnect", "Reconnect a dormant session without resuming", self.reconnect),
            ("queue", "Show the live committed queue", self.queue),
            ("remove", "Remove a committed queue entry", self.remove),
            ("jumpto", "Jump to a committed queue entry", self.jumpto),
            ("clearqueue", "Clear upcoming tracks and cancel pending imports", self.clearqueue),
            ("shuffle", "Shuffle committed upcoming tracks", self.shuffle),
            ("loop", "Toggle looping the current track", self.loop),
            ("autoplay", "Toggle autoplay or seed it with one track", self.autoplay),
            ("volume", "Set the current session volume from 0 to 200", self.volume),
            ("nowplaying", "Show the current or dormant track", self.nowplaying),
            ("status", "Show Playify's current local status", self.status),
        )
        for definition in definitions:
            self._command(*definition)

        setup = app_commands.Group(
            name="setup",
            description="Manage this server's Playify policy",
            default_permissions=discord.Permissions(manage_guild=True),
        )
        allowlist = app_commands.Group(
            name="allowlist", description="Manage allowed Playify channels", parent=setup
        )
        allowlist.command(name="set", description="Replace the channel allowlist")(self.allowlist_set)
        allowlist.command(name="add", description="Add up to five allowed channels")(self.allowlist_add)
        allowlist.command(name="remove", description="Remove up to five allowed channels")(self.allowlist_remove)
        allowlist.command(name="clear", description="Clear the allowlist and allow every channel")(self.allowlist_clear)
        allowlist.command(name="show", description="Show the effective allowed channels")(self.allowlist_show)
        channelmove = app_commands.Group(
            name="channelmove", description="Configure cross-channel playback moves", parent=setup
        )
        channelmove.command(name="show", description="Show the channel move mode")(self.channelmove_show)
        channelmove.command(name="set", description="Set the channel move mode")(self.channelmove_set)
        app_commands.guild_only()(setup)
        if hasattr(app_commands, "allowed_installs"):
            app_commands.allowed_installs(guilds=True, users=False)(setup)
        self.tree.add_command(setup)

    def server(self, guild_id: int) -> ServerSettings:
        return self.app.server_settings.setdefault(guild_id, ServerSettings(guild_id))

    def session(self, guild_id: int) -> PlayerSession:
        return self.app.players.get(guild_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.command is None:
            if not interaction.response.is_done():
                await self.app.responses.send(interaction, message("command.guild_only"), lifetime="error")
            return False
        name = interaction.command.qualified_name
        settings = self.server(interaction.guild.id)
        manager = is_manager(interaction)
        if name.startswith(SETUP_PREFIX) and not manager:
            await self.app.responses.send(interaction, message("command.manager_only"), lifetime="error")
            return False
        await self.prune_allowlist(interaction.guild, settings)
        if settings.allowlist and interaction.channel_id not in settings.allowlist and not manager:
            channels = [
                channel
                for channel_id in sorted(settings.allowlist)
                if (channel := interaction.guild.get_channel(channel_id)) is not None
            ]
            view = ChannelPaginator(channels)
            await self.app.responses.send(
                interaction,
                message("command.allowed_only"),
                embed=view.embed(),
                view=view,
                lifetime="interactive",
            )
            return False
        base = name.split(" ", 1)[0]
        if base in GENERAL or name.startswith(SETUP_PREFIX) or base in READ_ONLY:
            return isinstance(interaction.channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel))
        if base == "stop":
            existing = self.app.players.sessions.get(interaction.guild.id)
            if existing is None or existing.state.dormant:
                return True
        if base in MUTATIONS and voice_chat(interaction) is None:
            await self.app.responses.send(
                interaction, message("command.voice_chat_only"), lifetime="error"
            )
            return False
        existing = self.app.players.sessions.get(interaction.guild.id)
        target = voice_chat(interaction)
        if (
            base in WAKES_DORMANT
            and existing is not None
            and existing.state.dormant
            and target is not None
            and _human_count(target) == 0
        ):
            await self.app.responses.send(
                interaction, message("voice.empty"), lifetime="error"
            )
            return False
        return True

    async def prune_allowlist(self, guild: discord.Guild, settings: ServerSettings) -> None:
        retained: set[int] = set()
        member = guild.me
        for channel_id in settings.allowlist:
            channel = guild.get_channel(channel_id)
            if channel is None or not isinstance(
                channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)
            ):
                continue
            permissions = channel.permissions_for(member) if member else None
            if permissions and permissions.view_channel and permissions.send_messages:
                retained.add(channel_id)
        if retained != settings.allowlist:
            settings.allowlist = retained
            await self.app.storage.save_server(settings)

    async def _move_allowed(
        self,
        interaction: discord.Interaction,
        session: PlayerSession,
        target: VoiceChat,
    ) -> bool:
        if not session.voice or session.voice.channel.id == target.id:
            return True
        settings = self.server(interaction.guild_id)
        source = session.voice.channel
        if (
            settings.channel_move_mode == "protect"
            and isinstance(source, (discord.VoiceChannel, discord.StageChannel))
            and _human_count(source) > 0
            and not is_manager(interaction)
        ):
            return False
        return True

    async def _connect_fresh(
        self, interaction: discord.Interaction, session: PlayerSession, *, resume: bool
    ) -> VoiceChat:
        target = voice_chat(interaction)
        if target is None:
            raise ValueError("voice chat required")
        if _human_count(target) == 0:
            raise ValueError("voice channel is empty")
        await session.connect(target, interaction.channel_id, resume=resume)
        return target

    async def _enqueue(
        self,
        interaction: discord.Interaction,
        query: str,
        *,
        priority: bool,
        play_semantics: bool,
    ) -> None:
        assert interaction.guild_id is not None
        session = self.session(interaction.guild_id)
        target = voice_chat(interaction)
        if target is None:
            return
        progress = await self.app.responses.progress(
            interaction, message("progress.resolving")
        )
        if _human_count(target) == 0:
            await self.app.responses.finish_progress(
                progress, message("voice.empty"), failed=True
            )
            return
        dormant_play = session.state.dormant and play_semantics
        try:
            if session.state.dormant:
                if dormant_play:
                    await session.discard_dormant_current()
                    await self._connect_fresh(interaction, session, resume=False)
                    priority = True
                else:
                    await self._connect_fresh(interaction, session, resume=True)
            elif not session.active:
                session.state.volume = 100
                await self._connect_fresh(interaction, session, resume=True)

            pending = await session.enqueue(
                query,
                requested_by=interaction.user.id,
                priority=priority,
            )
            move_failed = False
            if session.voice and session.voice.channel.id != target.id:
                if await self._move_allowed(interaction, session, target):
                    try:
                        await session.move_to(target, interaction.channel_id)
                    except Exception:
                        move_failed = True
                else:
                    move_failed = True
            count, error = await session.wait_import(pending)
            if dormant_play and count and session.state.dormant:
                await session.connect(target, interaction.channel_id, resume=True)
            if not count:
                await self.app.responses.finish_progress(
                    progress, message("player.not_found"), failed=True
                )
                return
            text = message("player.import_complete", count=count)
            if error:
                text = message("player.import_partial_public", count=count)
            if move_failed:
                text += " " + message("player.move_failed")
            await self.app.responses.finish_progress(progress, text, failed=move_failed)
        except ValueError:
            await self.app.responses.finish_progress(
                progress, message("player.request_failed"), failed=True
            )
        except Exception as exc:
            await self.app.responses.finish_progress(
                progress, message("player.request_failed"), failed=True
            )
            LOGGER.exception("Track import failed: %s", exc)

    async def _wake(self, interaction: discord.Interaction, session: PlayerSession) -> None:
        if session.state.dormant:
            await self._connect_fresh(interaction, session, resume=True)

    async def play(self, interaction: discord.Interaction, query: str) -> None:
        await self._enqueue(interaction, query, priority=False, play_semantics=True)

    async def playnext(self, interaction: discord.Interaction, query: str) -> None:
        await self._enqueue(interaction, query, priority=True, play_semantics=False)

    async def search(self, interaction: discord.Interaction, query: str) -> None:
        progress = await self.app.responses.progress(
            interaction, message("progress.searching")
        )
        target = voice_chat(interaction)
        if target is None or _human_count(target) == 0:
            await self.app.responses.finish_progress(progress, message("voice.empty"), failed=True)
            return
        try:
            tracks = await self.app.extractor.search(query, 10)
            if not tracks:
                await self.app.responses.finish_progress(
                    progress, message("player.search_empty"), failed=True
                )
                return

            async def picked(selection: discord.Interaction, track: Track) -> None:
                assert selection.guild_id is not None
                session = self.session(selection.guild_id)
                target = voice_chat(selection)
                if target is None:
                    return
                if _human_count(target) == 0:
                    await self.app.responses.send(selection, message("voice.empty"), lifetime="error")
                    return
                track.requested_by = selection.user.id
                move_failed = False
                if session.state.dormant:
                    await session.discard_dormant_current()
                    await session.connect(target, selection.channel_id, resume=False)
                    await session.add_resolved([track], priority=True)
                else:
                    if not session.active:
                        session.state.volume = 100
                        await session.connect(target, selection.channel_id, resume=True)
                    await session.add_resolved([track], priority=False)
                    if session.voice and session.voice.channel.id != target.id:
                        if await self._move_allowed(selection, session, target):
                            try:
                                await session.move_to(target, selection.channel_id)
                            except Exception:
                                move_failed = True
                        else:
                            move_failed = True
                await self.app.responses.send(
                    selection,
                    message("player.added", title=safe_text(track.title))
                    + (" " + message("player.move_failed") if move_failed else ""),
                    lifetime="error" if move_failed else "success",
                )

            view = SearchView(tracks, picked, self.app.responses)
            embed = discord.Embed(
                title="Search results",
                description="\n".join(
                    f"`{index + 1:>2}` {safe_text(track.title, 100)} — {safe_text(track.uploader, 60)}"
                    for index, track in enumerate(tracks)
                ),
                color=0x5865F2,
            )
            await progress.edit(content=None, embed=embed, view=view)
            await self.app.responses.expire(progress, "interactive")
        except Exception as exc:
            LOGGER.exception("Search failed: %s", exc)
            await self.app.responses.finish_progress(
                progress, message("player.search_failed"), failed=True
            )

    async def pause(self, interaction: discord.Interaction) -> None:
        session = self.session(interaction.guild_id)
        if session.state.dormant:
            await self.app.responses.send(interaction, message("voice.dormant"), lifetime="error")
            return
        changed = await session.pause()
        track = session.state.current
        await self.app.responses.send(
            interaction,
            message("player.paused", title=safe_text(track.title))
            if changed and track
            else message("player.nothing_playing"),
            lifetime="success" if changed else "error",
        )

    async def resume(self, interaction: discord.Interaction) -> None:
        session = self.session(interaction.guild_id)
        if session.state.dormant:
            await self._wake(interaction, session)
            changed = True
        else:
            changed = await session.resume()
        track = session.state.current
        await self.app.responses.send(
            interaction,
            message("player.resumed", title=safe_text(track.title))
            if changed and track
            else message("player.not_paused"),
            lifetime="success" if changed else "error",
        )

    async def replay(self, interaction: discord.Interaction) -> None:
        session = self.session(interaction.guild_id)
        await self._wake(interaction, session)
        changed = await session.replay()
        track = session.state.current
        await self.app.responses.send(
            interaction,
            message("player.replayed", title=safe_text(track.title))
            if changed and track
            else message("player.live_seek" if track else "player.nothing_playing"),
            lifetime="success" if changed else "error",
        )

    async def seek(self, interaction: discord.Interaction, timestamp: str | None = None) -> None:
        session = self.session(interaction.guild_id)
        await self._wake(interaction, session)
        if not session.state.current:
            await self.app.responses.send(
                interaction, message("player.nothing_playing"), lifetime="error"
            )
            return
        if timestamp is None:
            if session.state.current.is_live:
                await self.app.responses.send(interaction, message("player.live_seek"), lifetime="error")
                return
            view = SeekView(session, self.app.responses)
            sent = await self.app.responses.send(
                interaction, embed=view.embed(), view=view, lifetime="interactive"
            )
            view.message = sent
            view.start_ticker()
            return
        try:
            position = await session.seek(parse_timestamp(timestamp), clamp=False)
            await self.app.responses.send(
                interaction, message("player.seeked", position=format_time(position))
            )
        except ValueError as exc:
            key = {
                "format": "player.seek_format",
                "live streams cannot be seeked": "player.live_seek",
                "timestamp is outside the current track": "player.seek_range",
            }.get(str(exc), "player.nothing_playing")
            await self.app.responses.send(interaction, message(key), lifetime="error")

    async def skip(self, interaction: discord.Interaction) -> None:
        session = self.session(interaction.guild_id)
        await self._wake(interaction, session)
        track = await session.skip()
        await self.app.responses.send(
            interaction,
            message("player.playing", title=safe_text(track.title))
            if track
            else message("player.queue_empty"),
        )

    async def previous(self, interaction: discord.Interaction) -> None:
        session = self.session(interaction.guild_id)
        await self._wake(interaction, session)
        track = await session.previous()
        await self.app.responses.send(
            interaction,
            message("player.playing", title=safe_text(track.title))
            if track
            else message("player.history_empty"),
            lifetime="success" if track else "error",
        )

    async def stop(self, interaction: discord.Interaction) -> None:
        await self.session(interaction.guild_id).stop()
        await self.app.responses.send(interaction, message("player.stopped"))

    async def reconnect(self, interaction: discord.Interaction) -> None:
        session = self.session(interaction.guild_id)
        if not session.state.dormant:
            await self.app.responses.send(
                interaction, message("player.not_dormant"), lifetime="error"
            )
            return
        await self._connect_fresh(interaction, session, resume=False)
        await self.app.responses.send(interaction, message("player.reconnected"))

    async def queue(self, interaction: discord.Interaction) -> None:
        session = self.session(interaction.guild_id)
        view = QueueView(session, self.app.responses)
        sent = await self.app.responses.send(
            interaction, embed=view.embed(), view=view, lifetime="interactive"
        )
        view.message = sent

    async def remove(self, interaction: discord.Interaction) -> None:
        session = self.session(interaction.guild_id)
        view = QueueView(session, self.app.responses, action="remove")
        sent = await self.app.responses.send(
            interaction, embed=view.embed(), view=view, lifetime="interactive"
        )
        view.message = sent

    async def jumpto(self, interaction: discord.Interaction) -> None:
        session = self.session(interaction.guild_id)
        await self._wake(interaction, session)
        view = QueueView(session, self.app.responses, action="jump")
        sent = await self.app.responses.send(
            interaction, embed=view.embed(), view=view, lifetime="interactive"
        )
        view.message = sent

    async def clearqueue(self, interaction: discord.Interaction) -> None:
        count = await self.session(interaction.guild_id).clear_queue()
        await self.app.responses.send(
            interaction,
            message("player.queue_cleared", count=count)
            if count
            else message("player.queue_already_empty"),
        )

    async def shuffle(self, interaction: discord.Interaction) -> None:
        count = await self.session(interaction.guild_id).shuffle()
        await self.app.responses.send(interaction, message("player.shuffle", count=count))

    async def loop(self, interaction: discord.Interaction) -> None:
        try:
            enabled = await self.session(interaction.guild_id).toggle_loop()
            await self.app.responses.send(
                interaction, message("player.loop", state="on" if enabled else "off")
            )
        except ValueError:
            await self.app.responses.send(
                interaction, message("player.nothing_playing"), lifetime="error"
            )

    async def autoplay(self, interaction: discord.Interaction, query: str | None = None) -> None:
        session = self.session(interaction.guild_id)
        if query:
            progress = await self.app.responses.progress(
                interaction, message("progress.autoplay")
            )
            target = voice_chat(interaction)
            if target is None or _human_count(target) == 0:
                await self.app.responses.finish_progress(
                    progress, message("voice.empty"), failed=True
                )
                return
            try:
                if not session.active:
                    session.state.volume = 100
                    await self._connect_fresh(interaction, session, resume=True)
                elif session.voice and session.voice.channel.id != target.id:
                    if not await self._move_allowed(interaction, session, target):
                        await self.app.responses.finish_progress(
                            progress, message("player.move_failed"), failed=True
                        )
                        return
                    await session.move_to(target, interaction.channel_id)
                await session.autoplay_query(query, interaction.user.id)
                await self.app.responses.finish_progress(progress, message("autoplay.enabled"))
            except Exception as exc:
                LOGGER.exception("Autoplay seed failed: %s", exc)
                await session.set_autoplay(True)
                await self.app.responses.finish_progress(
                    progress, message("autoplay.failed"), failed=True
                )
            return
        enabled = await session.set_autoplay(not session.state.autoplay_enabled)
        if enabled and session.state.current is None:
            text = message("autoplay.armed")
        else:
            text = message("autoplay.enabled" if enabled else "autoplay.disabled")
        await self.app.responses.send(interaction, text)

    async def volume(self, interaction: discord.Interaction, value: app_commands.Range[int, 0, 200]) -> None:
        session = self.session(interaction.guild_id)
        if not (session.state.current or session.state.queue or session.state.dormant):
            await self.app.responses.send(
                interaction, message("player.no_session"), lifetime="error"
            )
            return
        volume = await session.set_volume(value)
        await self.app.responses.send(interaction, message("player.volume", volume=volume))

    async def nowplaying(self, interaction: discord.Interaction) -> None:
        session = self.session(interaction.guild_id)
        track = session.state.current
        if not track:
            await self.app.responses.send(interaction, message("player.empty"))
            return
        state = "Dormant" if session.state.dormant else "Paused" if session.state.paused else "Playing"
        embed = discord.Embed(title=state, color=0x5865F2)
        embed.add_field(name="Track", value=safe_text(track.title), inline=False)
        embed.add_field(name="Artist", value=safe_text(track.uploader), inline=False)
        embed.add_field(name="Position", value=format_time(session.position), inline=False)
        if session.state.dormant:
            embed.set_footer(text="Use a playback command in an occupied Voice or Stage chat to resume.")
        await self.app.responses.send(interaction, embed=embed)

    async def status(self, interaction: discord.Interaction) -> None:
        players = sum(1 for session in self.app.players.sessions.values() if session.active or session.state.dormant)
        queued = sum(len(session.state.queue) for session in self.app.players.sessions.values())
        await self.app.responses.send(
            interaction,
            message("status.summary", version=display_version(), players=players, queued=queued),
        )

    @staticmethod
    def _channels(*values: AllowedChannel | None) -> list[AllowedChannel]:
        result: list[AllowedChannel] = []
        seen: set[int] = set()
        for channel in values:
            if channel and channel.id not in seen:
                seen.add(channel.id)
                result.append(channel)
        return result

    async def _apply_allowlist(
        self,
        interaction: discord.Interaction,
        operation: Literal["set", "add", "remove"],
        channels: list[AllowedChannel],
    ) -> None:
        settings = self.server(interaction.guild_id)
        before = set(settings.allowlist)
        selected = {channel.id for channel in channels}
        if operation == "set":
            settings.allowlist = selected
            unchanged = len(selected & before)
        elif operation == "add":
            settings.allowlist |= selected
            unchanged = len(selected & before)
        else:
            settings.allowlist -= selected
            unchanged = len(selected - before)
        await self.app.storage.save_server(settings)
        added = len(settings.allowlist - before)
        removed = len(before - settings.allowlist)
        await self.app.responses.send(
            interaction,
            message(
                "setup.allowlist.updated",
                added=added,
                removed=removed,
                unchanged=unchanged,
            ),
        )

    async def allowlist_set(
        self,
        interaction: discord.Interaction,
        channel1: AllowedChannel,
        channel2: AllowedChannel | None = None,
        channel3: AllowedChannel | None = None,
        channel4: AllowedChannel | None = None,
        channel5: AllowedChannel | None = None,
    ) -> None:
        await self._apply_allowlist(
            interaction, "set", self._channels(channel1, channel2, channel3, channel4, channel5)
        )

    async def allowlist_add(
        self,
        interaction: discord.Interaction,
        channel1: AllowedChannel,
        channel2: AllowedChannel | None = None,
        channel3: AllowedChannel | None = None,
        channel4: AllowedChannel | None = None,
        channel5: AllowedChannel | None = None,
    ) -> None:
        await self._apply_allowlist(
            interaction, "add", self._channels(channel1, channel2, channel3, channel4, channel5)
        )

    async def allowlist_remove(
        self,
        interaction: discord.Interaction,
        channel1: AllowedChannel,
        channel2: AllowedChannel | None = None,
        channel3: AllowedChannel | None = None,
        channel4: AllowedChannel | None = None,
        channel5: AllowedChannel | None = None,
    ) -> None:
        await self._apply_allowlist(
            interaction, "remove", self._channels(channel1, channel2, channel3, channel4, channel5)
        )

    async def allowlist_clear(self, interaction: discord.Interaction) -> None:
        settings = self.server(interaction.guild_id)
        settings.allowlist.clear()
        await self.app.storage.save_server(settings)
        await self.app.responses.send(interaction, message("setup.allowlist.unrestricted"))

    async def allowlist_show(self, interaction: discord.Interaction) -> None:
        settings = self.server(interaction.guild_id)
        assert interaction.guild is not None
        if settings.allowlist:
            channels = [
                channel
                for channel_id in sorted(settings.allowlist)
                if (channel := interaction.guild.get_channel(channel_id)) is not None
            ]
        else:
            member = interaction.guild.me
            channels = []
            for channel in interaction.guild.channels:
                if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)):
                    continue
                permissions = channel.permissions_for(member) if member else None
                if permissions and permissions.view_channel and permissions.send_messages:
                    channels.append(channel)
        view = ChannelPaginator(channels)
        await self.app.responses.send(
            interaction,
            message("setup.allowlist.unrestricted") if not settings.allowlist else None,
            embed=view.embed(),
            view=view,
            lifetime="interactive",
        )

    async def channelmove_show(self, interaction: discord.Interaction) -> None:
        mode = self.server(interaction.guild_id).channel_move_mode
        await self.app.responses.send(interaction, message("setup.channelmove", mode=mode))

    async def channelmove_set(
        self, interaction: discord.Interaction, mode: Literal["allow", "protect"]
    ) -> None:
        settings = self.server(interaction.guild_id)
        settings.channel_move_mode = mode
        await self.app.storage.save_server(settings)
        await self.app.responses.send(interaction, message("setup.channelmove", mode=mode))
