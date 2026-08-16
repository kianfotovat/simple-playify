"""The session-scoped Voice/Stage controller."""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlsplit

import discord

from ..config import Config
from ..discord_utils import Responses, format_time, safe_text, source_text
from ..messages import message
from ..services.extractor import public_canonical_link
from ..services.player import PlayerManager, PlayerSession, _human_count
from ..storage import Storage
from .views import QueueView, allowed_interaction

LOGGER = logging.getLogger(__name__)


class AddTrackModal(discord.ui.Modal, title=message("controller.add.title")):
    query = discord.ui.TextInput(
        label=message("controller.add.query"), max_length=500
    )

    def __init__(self, session: PlayerSession, responses: Responses) -> None:
        super().__init__()
        self.session = session
        self.responses = responses

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = self.session.voice.channel if self.session.voice else None
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)) or _human_count(channel) == 0:
            await self.responses.send(
                interaction,
                message("voice.empty"),
                lifetime="error",
            )
            return
        progress = await self.responses.progress(interaction, message("progress.resolving"))
        pending = await self.session.enqueue(
            str(self.query), requested_by=interaction.user.id, priority=False
        )
        count, error = await self.session.wait_import(pending)
        if count:
            key = "player.import_partial_public" if error else "player.import_complete"
            await self.responses.finish_progress(progress, message(key, count=count))
        else:
            await self.responses.finish_progress(
                progress, message("player.not_found"), failed=True
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
        if not await allowed_interaction(self.manager.responses, interaction):
            return False
        current = self.session.state.controller_message_id
        if interaction.message is None or interaction.message.id != current:
            if not interaction.response.is_done():
                await interaction.response.defer()
            return False
        return True

    def _button(
        self,
        label: str,
        row: int,
        callback,
        *,
        custom_id: str,
        emoji: str | None = None,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ) -> None:
        button = discord.ui.Button(
            label=label,
            emoji=emoji,
            row=row,
            style=style,
            custom_id=f"playify:controller:{custom_id}",
        )
        button.callback = callback
        self.add_item(button)

    def _add_buttons(self) -> None:
        async def acknowledge(interaction: discord.Interaction) -> None:
            if not interaction.response.is_done():
                await interaction.response.defer()

        async def previous(interaction: discord.Interaction) -> None:
            await acknowledge(interaction)
            await self.session.previous()

        async def pause_resume(interaction: discord.Interaction) -> None:
            await acknowledge(interaction)
            if self.session.state.paused:
                await self.session.resume()
            else:
                await self.session.pause()

        async def skip(interaction: discord.Interaction) -> None:
            await acknowledge(interaction)
            await self.session.skip()

        async def stop(interaction: discord.Interaction) -> None:
            await acknowledge(interaction)
            await self.session.stop()

        async def add(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(AddTrackModal(self.session, self.manager.responses))

        async def shuffle(interaction: discord.Interaction) -> None:
            await acknowledge(interaction)
            await self.session.shuffle()

        async def loop(interaction: discord.Interaction) -> None:
            await acknowledge(interaction)
            try:
                await self.session.toggle_loop()
            except ValueError:
                pass

        async def autoplay(interaction: discord.Interaction) -> None:
            await acknowledge(interaction)
            await self.session.set_autoplay(
                not self.session.state.autoplay_enabled
            )

        async def volume_down(interaction: discord.Interaction) -> None:
            await acknowledge(interaction)
            await self.session.set_volume(self.session.state.volume - 10)

        async def volume_up(interaction: discord.Interaction) -> None:
            await acknowledge(interaction)
            await self.session.set_volume(self.session.state.volume + 10)

        async def queue(interaction: discord.Interaction) -> None:
            view = self.manager.queue_view(self.session)
            sent = await self.manager.responses.send(
                interaction, embed=view.embed(), view=view, lifetime="interactive"
            )
            view.message = sent

        async def jump(interaction: discord.Interaction) -> None:
            view = self.manager.queue_view(self.session, action="jump")
            sent = await self.manager.responses.send(
                interaction, embed=view.embed(), view=view, lifetime="interactive"
            )
            view.message = sent

        paused = self.session.state.paused
        self._button(
            message("controller.button.previous"),
            0,
            previous,
            custom_id="previous",
            emoji="⏮️",
            style=discord.ButtonStyle.primary,
        )
        self._button(
            message("controller.button.play" if paused else "controller.button.pause"),
            0,
            pause_resume,
            custom_id="play_pause",
            emoji="▶️" if paused else "⏸️",
            style=discord.ButtonStyle.success if paused else discord.ButtonStyle.secondary,
        )
        self._button(
            message("controller.button.skip"),
            0,
            skip,
            custom_id="skip",
            emoji="⏭️",
            style=discord.ButtonStyle.primary,
        )
        self._button(
            message("controller.button.stop"),
            0,
            stop,
            custom_id="stop",
            emoji="⏹️",
            style=discord.ButtonStyle.danger,
        )
        self._button(
            message("controller.button.add"),
            0,
            add,
            custom_id="add",
            style=discord.ButtonStyle.success,
        )
        self._button(
            message("controller.button.volume_down"),
            1,
            volume_down,
            custom_id="volume_down",
            emoji="🔉",
        )
        self._button(
            message("controller.button.volume_up"),
            1,
            volume_up,
            custom_id="volume_up",
            emoji="🔊",
        )
        self._button(
            message("controller.button.shuffle"),
            1,
            shuffle,
            custom_id="shuffle",
            emoji="🔀",
        )
        self._button(
            message("controller.button.loop"),
            1,
            loop,
            custom_id="loop",
            emoji="🔁",
            style=(
                discord.ButtonStyle.success
                if self.session.state.loop_current
                else discord.ButtonStyle.secondary
            ),
        )
        self._button(
            message("controller.button.autoplay"),
            1,
            autoplay,
            custom_id="autoplay",
            emoji="➡️",
            style=(
                discord.ButtonStyle.success
                if self.session.state.autoplay_enabled
                else discord.ButtonStyle.secondary
            ),
        )
        self._button(
            message("controller.button.queue"),
            2,
            queue,
            custom_id="queue",
            emoji="📜",
            style=discord.ButtonStyle.primary,
        )
        self._button(
            message("button.jump"),
            2,
            jump,
            custom_id="jump",
            emoji="⤵️",
        )


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
        self.ticker_tasks: dict[int, asyncio.Task[None]] = {}
        self.queue_views: dict[int, set[QueueView]] = {}
        self.dirty: set[int] = set()
        self.view_dirty: set[int] = set()
        self.expected_deletions: dict[tuple[int, int], float] = {}

    def queue_view(self, session: PlayerSession, action: str = "view") -> QueueView:
        view = QueueView(
            session,
            self.responses,
            action=action,
            on_finish=self._discard_queue_view,
        )
        self.queue_views.setdefault(session.guild_id, set()).add(view)
        return view

    def _discard_queue_view(self, view: QueueView) -> None:
        views = self.queue_views.get(view.session.guild_id)
        if not views:
            return
        views.discard(view)
        if not views:
            self.queue_views.pop(view.session.guild_id, None)

    async def recreate(self, session: PlayerSession) -> bool:
        """Delete and recreate the active controller at the bottom of its channel."""

        self._stop_ticker(session.guild_id)
        edit_task = self.edit_tasks.pop(session.guild_id, None)
        if edit_task and not edit_task.done():
            edit_task.cancel()
            await asyncio.gather(edit_task, return_exceptions=True)
        self.dirty.discard(session.guild_id)
        self.view_dirty.discard(session.guild_id)
        if session.state.controller_channel_id and session.state.controller_message_id:
            await self._delete_message(
                session.state.controller_channel_id,
                session.state.controller_message_id,
            )
        session.state.controller_channel_id = None
        session.state.controller_message_id = None
        await self.storage.clear_controller_cleanup(session.guild_id)
        if not session.active:
            await self.storage.save_player(session.state)
            return False
        await self.ensure(session)
        return session.state.controller_message_id is not None

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
        now = time.monotonic()
        self.expected_deletions = {
            key: marked
            for key, marked in self.expected_deletions.items()
            if now - marked < 60
        }
        self.expected_deletions[(channel_id, message_id)] = now
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
            display_title = (
                message("controller.current.linked", title=title, link=link)
                if link
                else title
            )
            now = message(
                "controller.current.details",
                title=display_title,
                uploader=safe_text(current.uploader, 100),
                position=format_time(session.position),
            )
            color = 0x57F287 if not session.state.paused else 0xFEE75C
        else:
            now = message("controller.waiting")
            color = 0x5865F2
        next_track = session.state.queue[0] if session.state.queue else None
        up_next = (
            message(
                "controller.up_next.track",
                title=safe_text(next_track.title, 150),
                uploader=safe_text(next_track.uploader, 100),
            )
            if next_track
            else message("controller.up_next.empty")
        )
        up_next += "\n" + message(
            "controller.up_next.counts",
            upcoming=len(session.state.queue),
            pending=len(session.state.pending),
        )
        embed = discord.Embed(
            title=message(
                "controller.title.playing" if current else "controller.title.waiting"
            ),
            description=now,
            color=color,
        )
        embed.add_field(
            name=message("controller.field.up_next"), value=up_next, inline=False
        )
        source = current.source if current else "idle"
        embed.set_footer(
            text=message(
                "controller.footer",
                source=source_text(source),
                volume=session.state.volume,
            )
        )
        if current and current.thumbnail:
            embed.set_thumbnail(url=current.thumbnail)
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
        if (
            session.state.controller_message_id
            and session.state.controller_channel_id != channel.id
        ):
            if session.state.controller_channel_id:
                await self._delete_message(
                    session.state.controller_channel_id,
                    session.state.controller_message_id,
                )
            session.state.controller_channel_id = None
            session.state.controller_message_id = None
            await self.storage.clear_controller_cleanup(session.guild_id)
        if session.state.controller_message_id:
            self.request_update(session)
            self._ensure_ticker(session)
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
            self._ensure_ticker(session)
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Could not create controller for guild %s", session.guild_id)

    def request_update(
        self, session: PlayerSession, *, refresh_view: bool = False
    ) -> None:
        self.dirty.add(session.guild_id)
        if refresh_view:
            self.view_dirty.add(session.guild_id)
        task = self.edit_tasks.get(session.guild_id)
        if task and not task.done():
            return

        async def edit_latest() -> None:
            try:
                while session.guild_id in self.dirty:
                    self.dirty.discard(session.guild_id)
                    await asyncio.sleep(0.15)
                    if session.guild_id in self.dirty:
                        continue
                    refresh_view = session.guild_id in self.view_dirty
                    self.view_dirty.discard(session.guild_id)
                    channel = self.bot.get_channel(session.state.controller_channel_id or 0)
                    if not channel or not hasattr(channel, "fetch_message"):
                        return
                    try:
                        sent = await channel.fetch_message(session.state.controller_message_id)  # type: ignore[union-attr]
                        kwargs = {"embed": self.embed(session)}
                        if refresh_view:
                            kwargs["view"] = ControllerView(self, session)
                        await sent.edit(**kwargs)
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

    def _ensure_ticker(self, session: PlayerSession) -> None:
        if not (
            session.active
            and session.state.current
            and not session.state.paused
            and not session.state.dormant
            and session.state.controller_message_id
        ):
            return
        task = self.ticker_tasks.get(session.guild_id)
        if task and not task.done():
            return

        async def tick() -> None:
            try:
                while (
                    session.active
                    and session.state.current
                    and not session.state.paused
                    and not session.state.dormant
                    and session.state.controller_message_id
                ):
                    await asyncio.sleep(1)
                    if session.state.paused or session.state.dormant:
                        break
                    self.request_update(session)
            except asyncio.CancelledError:
                pass
            finally:
                if self.ticker_tasks.get(session.guild_id) is asyncio.current_task():
                    self.ticker_tasks.pop(session.guild_id, None)

        self.ticker_tasks[session.guild_id] = asyncio.create_task(
            tick(), name=f"controller-ticker-{session.guild_id}"
        )

    def _stop_ticker(self, guild_id: int) -> None:
        task = self.ticker_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()

    async def on_player_change(self, session: PlayerSession, event: str) -> None:
        for view in list(self.queue_views.get(session.guild_id, ())):
            await view.refresh()
        if event == "external_move" and session.state.controller_channel_id and session.state.controller_message_id:
            await self._delete_message(
                session.state.controller_channel_id, session.state.controller_message_id
            )
            session.state.controller_channel_id = None
            session.state.controller_message_id = None
        if session.state.dormant or event in {
            "stopping",
            "stopped",
            "idle_timeout",
            "voice_move_failed",
            "voice_recovery_failed",
        }:
            self._stop_ticker(session.guild_id)
            if session.state.controller_channel_id and session.state.controller_message_id:
                await self._delete_message(
                    session.state.controller_channel_id, session.state.controller_message_id
                )
                session.state.controller_channel_id = None
                session.state.controller_message_id = None
                await self.storage.clear_controller_cleanup(session.guild_id)
                await self.storage.save_player(session.state)
            return
        if session.active:
            await self.ensure(session)
            self.request_update(
                session,
                refresh_view=event
                in {
                    "autoplay",
                    "loop",
                    "paused",
                    "reconnected_paused",
                    "resumed",
                    "track_selected",
                    "track_started",
                },
            )
            if session.state.paused:
                self._stop_ticker(session.guild_id)
            else:
                self._ensure_ticker(session)

    async def raw_delete(self, channel_id: int, message_id: int) -> None:
        if self.expected_deletions.pop((channel_id, message_id), None) is not None:
            return
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
        for views in self.queue_views.values():
            for view in views:
                view.stop()
        self.queue_views.clear()
        tasks = list(self.edit_tasks.values()) + list(self.ticker_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.ticker_tasks.clear()
        self.view_dirty.clear()
        for session in self.players.sessions.values():
            if session.state.controller_channel_id and session.state.controller_message_id:
                await self._delete_message(
                    session.state.controller_channel_id, session.state.controller_message_id
                )
                await self.storage.clear_controller_cleanup(session.guild_id)
                session.state.controller_channel_id = None
                session.state.controller_message_id = None
