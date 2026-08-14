"""The session-scoped Voice/Stage controller."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

import discord

from ..config import Config
from ..discord_utils import Responses, format_time, safe_text
from ..services.extractor import public_canonical_link
from ..services.player import PlayerManager, PlayerSession
from ..storage import Storage
from .views import QueueView

LOGGER = logging.getLogger(__name__)


class AddTrackModal(discord.ui.Modal, title="Add a track"):
    query = discord.ui.TextInput(label="Link or search", max_length=500)

    def __init__(self, session: PlayerSession, responses: Responses) -> None:
        super().__init__()
        self.session = session
        self.responses = responses

    async def on_submit(self, interaction: discord.Interaction) -> None:
        progress = await self.responses.progress(interaction, "Resolving your request…")
        pending = await self.session.enqueue(
            str(self.query), requested_by=interaction.user.id, priority=False
        )
        count, error = await self.session.wait_import(pending)
        if count:
            suffix = f" ({error})" if error else ""
            await self.responses.finish_progress(progress, f"Added {count} track(s){suffix}.")
        else:
            await self.responses.finish_progress(
                progress, "No playable tracks were found.", failed=True
            )


class ControllerView(discord.ui.View):
    def __init__(
        self, manager: "ControllerManager", session: PlayerSession
    ) -> None:
        super().__init__(timeout=None)
        self.manager = manager
        self.session = session
        self._add_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        current = self.session.state.controller_message_id
        if interaction.message is None or interaction.message.id != current:
            await self.manager.responses.send(
                interaction, "That controller is stale.", lifetime="error"
            )
            return False
        return True

    def _button(
        self,
        label: str,
        row: int,
        callback,
        *,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ) -> None:
        button = discord.ui.Button(label=label, row=row, style=style)
        button.callback = callback
        self.add_item(button)

    def _add_buttons(self) -> None:
        async def previous(interaction: discord.Interaction) -> None:
            track = await self.session.previous()
            await self.manager.responses.send(
                interaction,
                f"Playing **{safe_text(track.title)}**." if track else "History is empty.",
                lifetime="success" if track else "error",
            )

        async def pause_resume(interaction: discord.Interaction) -> None:
            if self.session.state.paused:
                changed = await self.session.resume()
                text = "Playback resumed." if changed else "Playback is dormant."
            else:
                changed = await self.session.pause()
                text = "Playback paused." if changed else "Nothing is playing."
            await self.manager.responses.send(
                interaction, text, lifetime="success" if changed else "error"
            )

        async def skip(interaction: discord.Interaction) -> None:
            track = await self.session.skip()
            await self.manager.responses.send(
                interaction,
                f"Playing **{safe_text(track.title)}**." if track else "The queue is empty.",
            )

        async def stop(interaction: discord.Interaction) -> None:
            await self.session.stop()
            await self.manager.responses.send(interaction, "Stopped playback and cleared the session.")

        async def add(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(AddTrackModal(self.session, self.manager.responses))

        async def shuffle(interaction: discord.Interaction) -> None:
            count = await self.session.shuffle()
            await self.manager.responses.send(interaction, f"Shuffled {count} queued tracks.")

        async def loop(interaction: discord.Interaction) -> None:
            try:
                enabled = await self.session.toggle_loop()
                await self.manager.responses.send(interaction, f"Loop is {'on' if enabled else 'off'}.")
            except ValueError:
                await self.manager.responses.send(interaction, "Nothing is playing.", lifetime="error")

        async def autoplay(interaction: discord.Interaction) -> None:
            enabled = await self.session.set_autoplay(not self.session.state.autoplay_enabled)
            await self.manager.responses.send(interaction, f"Autoplay is {'on' if enabled else 'off'}.")

        async def volume_down(interaction: discord.Interaction) -> None:
            volume = await self.session.set_volume(self.session.state.volume - 10)
            await self.manager.responses.send(interaction, f"Volume is {volume}%.")

        async def volume_up(interaction: discord.Interaction) -> None:
            volume = await self.session.set_volume(self.session.state.volume + 10)
            await self.manager.responses.send(interaction, f"Volume is {volume}%.")

        async def queue(interaction: discord.Interaction) -> None:
            view = QueueView(self.session, self.manager.responses)
            sent = await self.manager.responses.send(
                interaction, embed=view.embed(), view=view, lifetime="interactive"
            )
            view.message = sent

        async def jump(interaction: discord.Interaction) -> None:
            view = QueueView(self.session, self.manager.responses, action="jump")
            sent = await self.manager.responses.send(
                interaction, embed=view.embed(), view=view, lifetime="interactive"
            )
            view.message = sent

        self._button("Previous", 0, previous)
        self._button("Pause / Resume", 0, pause_resume, style=discord.ButtonStyle.primary)
        self._button("Skip", 0, skip)
        self._button("Stop", 0, stop, style=discord.ButtonStyle.danger)
        self._button("Add", 0, add, style=discord.ButtonStyle.success)
        self._button("Shuffle", 1, shuffle)
        self._button("Loop", 1, loop)
        self._button("Autoplay", 1, autoplay)
        self._button("Vol −", 1, volume_down)
        self._button("Vol +", 1, volume_up)
        self._button("Queue", 2, queue)
        self._button("Jump", 2, jump)


class ControllerManager:
    def __init__(
        self,
        bot: discord.Client,
        players: PlayerManager,
        storage: Storage,
        responses: Responses,
    ) -> None:
        self.bot = bot
        self.players = players
        self.storage = storage
        self.responses = responses
        self.edit_tasks: dict[int, asyncio.Task[None]] = {}
        self.dirty: set[int] = set()

    async def startup_cleanup(self) -> None:
        pointers = await self.storage.pop_controller_cleanups()
        pointers.extend(
            (
                session.guild_id,
                session.state.controller_channel_id,
                session.state.controller_message_id,
            )
            for session in self.players.sessions.values()
            if session.state.controller_channel_id and session.state.controller_message_id
        )
        for guild_id, channel_id, message_id in pointers:
            await self._delete_message(channel_id, message_id)
            session = self.players.sessions.get(guild_id)
            if session:
                session.state.controller_channel_id = None
                session.state.controller_message_id = None
                await session.changed("controller_cleaned")

    async def _delete_message(self, channel_id: int, message_id: int) -> None:
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            if hasattr(channel, "fetch_message"):
                message = await channel.fetch_message(message_id)  # type: ignore[union-attr]
                await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    def embed(self, session: PlayerSession) -> discord.Embed:
        current = session.state.current
        if current:
            title = safe_text(current.title, 150)
            link = public_canonical_link(current)
            now = f"[{title}]({link})" if link else title
            now += f"\n{safe_text(current.uploader, 100)} • {format_time(session.position)}"
            color = 0x57F287 if not session.state.paused else 0xFEE75C
        else:
            now = "Waiting for a track."
            color = 0x5865F2
        next_track = session.state.queue[0] if session.state.queue else None
        up_next = (
            f"{safe_text(next_track.title, 150)} — {safe_text(next_track.uploader, 100)}"
            if next_track
            else "Nothing queued."
        )
        embed = discord.Embed(title="Playify", color=color)
        embed.add_field(name="Now Playing", value=now, inline=False)
        embed.add_field(name="Up Next", value=up_next, inline=False)
        source = current.source if current else "idle"
        embed.set_footer(
            text=(
                f"{len(session.state.queue)} upcoming • {len(session.state.pending)} pending • "
                f"{source} • {session.state.volume}% • "
                f"loop {'on' if session.state.loop_current else 'off'} • "
                f"autoplay {'on' if session.state.autoplay_enabled else 'off'}"
            )
        )
        if not current:
            image = str(Config.get("controller_idle_image", "")).strip()
            parts = urlsplit(image)
            if image and image.lower() != "none" and parts.scheme in {"http", "https"} and parts.hostname:
                embed.set_image(url=image)
        return embed

    async def ensure(self, session: PlayerSession) -> None:
        if not session.active or (session.state.current is None and not session.state.queue):
            return
        channel = self.bot.get_channel(session.state.text_channel_id or 0)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return
        if session.state.controller_message_id:
            self.request_update(session)
            return
        try:
            view = ControllerView(self, session)
            sent = await channel.send(
                embed=self.embed(session),
                view=view,
                silent=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            session.state.controller_channel_id = channel.id
            session.state.controller_message_id = sent.id
            await self.storage.set_controller_cleanup(session.guild_id, channel.id, sent.id)
            await session.changed("controller_created")
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Could not create controller for guild %s", session.guild_id)

    def request_update(self, session: PlayerSession) -> None:
        self.dirty.add(session.guild_id)
        task = self.edit_tasks.get(session.guild_id)
        if task and not task.done():
            return

        async def edit_latest() -> None:
            try:
                while session.guild_id in self.dirty:
                    self.dirty.discard(session.guild_id)
                    await asyncio.sleep(0.15)
                    channel = self.bot.get_channel(session.state.controller_channel_id or 0)
                    if not channel or not hasattr(channel, "fetch_message"):
                        return
                    try:
                        sent = await channel.fetch_message(session.state.controller_message_id)  # type: ignore[union-attr]
                        await sent.edit(embed=self.embed(session), view=ControllerView(self, session))
                    except discord.NotFound:
                        session.state.controller_channel_id = None
                        session.state.controller_message_id = None
                        await self.ensure(session)
                    except (discord.Forbidden, discord.HTTPException):
                        return
            finally:
                self.edit_tasks.pop(session.guild_id, None)

        self.edit_tasks[session.guild_id] = asyncio.create_task(
            edit_latest(), name=f"controller-{session.guild_id}"
        )

    async def on_player_change(self, session: PlayerSession, event: str) -> None:
        if event == "external_move" and session.state.controller_channel_id and session.state.controller_message_id:
            await self._delete_message(
                session.state.controller_channel_id, session.state.controller_message_id
            )
            session.state.controller_channel_id = None
            session.state.controller_message_id = None
        if event in {"stopping", "stopped", "dormant", "idle_timeout", "voice_move_failed", "voice_recovery_failed"}:
            if session.state.controller_channel_id and session.state.controller_message_id:
                await self._delete_message(
                    session.state.controller_channel_id, session.state.controller_message_id
                )
                session.state.controller_channel_id = None
                session.state.controller_message_id = None
                await self.storage.clear_controller_cleanup(session.guild_id)
            return
        if session.active:
            await self.ensure(session)
            self.request_update(session)

    async def raw_delete(self, channel_id: int, message_id: int) -> None:
        for session in self.players.sessions.values():
            if (
                session.state.controller_channel_id == channel_id
                and session.state.controller_message_id == message_id
            ):
                session.state.controller_channel_id = None
                session.state.controller_message_id = None
                await session.changed("controller_deleted")
                await self.ensure(session)
                return

    async def shutdown(self) -> None:
        for task in self.edit_tasks.values():
            task.cancel()
        for session in self.players.sessions.values():
            if session.state.controller_channel_id and session.state.controller_message_id:
                await self._delete_message(
                    session.state.controller_channel_id, session.state.controller_message_id
                )
                await self.storage.clear_controller_cleanup(session.guild_id)
