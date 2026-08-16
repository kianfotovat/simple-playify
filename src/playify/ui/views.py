"""Collaborative, short-lived Discord interaction views."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence

import discord

from ..discord_utils import Responses, duration_text, format_time, progress_bar, safe_text
from ..messages import message
from ..models import Track
from ..services.player import LIVE_SEEK_ERROR, SEEK_RANGE_ERROR, PlayerSession


async def allowed_interaction(responses: Responses, interaction: discord.Interaction) -> bool:
    """Apply the current server allowlist to collaborative component interactions."""

    settings = getattr(responses.bot, "server_settings", {}).get(interaction.guild_id)
    manager = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild
    if settings and settings.allowlist and interaction.channel_id not in settings.allowlist and not manager:
        await responses.send(interaction, message("command.allowed_only"), lifetime="error")
        return False
    return True


async def active_interaction(responses: Responses, interaction: discord.Interaction) -> bool:
    """Apply channel policy and renew a short-lived view's inactivity timer."""

    if not await allowed_interaction(responses, interaction):
        return False
    if interaction.message is not None:
        await responses.expire(interaction.message, "interactive")
    return True


async def dismiss_message(
    view: discord.ui.View | discord.ui.LayoutView,
    responses: Responses,
    interaction: discord.Interaction,
    message_pointer: discord.Message | None,
) -> None:
    """Acknowledge a component and delete its complete interaction message."""

    if not interaction.response.is_done():
        await interaction.response.defer()
    view.stop()
    sent = interaction.message or message_pointer
    if sent is not None:
        try:
            await sent.delete()
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException):
            return
        await responses.cancel_expiration(sent)
        return
    try:
        await interaction.delete_original_response()
    except discord.NotFound:
        pass


class QueueView(discord.ui.View):
    def __init__(
        self,
        session: PlayerSession,
        responses: Responses,
        action: str = "view",
        on_finish: Callable[[QueueView], None] | None = None,
    ) -> None:
        super().__init__(timeout=60)
        self.session = session
        self.responses = responses
        self.action = action
        self.on_finish = on_finish
        self.page = 0
        self.message: discord.Message | None = None
        self._rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await active_interaction(self.responses, interaction)

    def _tracks(self) -> list[Track]:
        return list(self.session.state.queue)

    def _rebuild(self) -> None:
        self.clear_items()
        tracks = self._tracks()
        pages = max(1, math.ceil(len(tracks) / 10))
        self.page = max(0, min(self.page, pages - 1))
        start = self.page * 10
        visible = tracks[start : start + 10]
        if self.action in {"remove", "jump"} and visible:
            select = discord.ui.Select(
                placeholder=(
                    message("queue.select.remove") if self.action == "remove" else message("queue.select.jump")
                ),
                options=[
                    discord.SelectOption(
                        label=track.title[:100],
                        description=message(
                            "queue.select.description",
                            number=start + index + 1,
                            uploader=track.uploader,
                        )[:100],
                        value=track.occurrence_id,
                    )
                    for index, track in enumerate(visible)
                ],
                row=0,
            )

            async def selected(interaction: discord.Interaction) -> None:
                occurrence_id = select.values[0]
                self._finish()
                await dismiss_message(self, self.responses, interaction, self.message)
                if self.action == "remove":
                    changed = await self.session.remove(occurrence_id)
                else:
                    changed = await self.session.jump(occurrence_id)
                if changed is None:
                    await self.responses.send(interaction, message("queue.missing"), lifetime="error")
                else:
                    await self.responses.send(
                        interaction,
                        message(
                            "queue.removed" if self.action == "remove" else "queue.jumped",
                            title=safe_text(changed.title),
                        ),
                    )

            select.callback = selected
            self.add_item(select)

        previous = discord.ui.Button(label=message("button.previous_page"), disabled=self.page == 0, row=1)
        following = discord.ui.Button(label=message("button.next_page"), disabled=self.page >= pages - 1, row=1)
        close = discord.ui.Button(label=message("button.close"), style=discord.ButtonStyle.danger, row=1)

        async def go_previous(interaction: discord.Interaction) -> None:
            self.page -= 1
            self._rebuild()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        async def go_next(interaction: discord.Interaction) -> None:
            self.page += 1
            self._rebuild()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        async def close_view(interaction: discord.Interaction) -> None:
            self._finish()
            await dismiss_message(self, self.responses, interaction, self.message)

        previous.callback = go_previous
        following.callback = go_next
        close.callback = close_view
        self.add_item(previous)
        self.add_item(following)
        self.add_item(close)

    def _finish(self) -> None:
        self.stop()
        if self.on_finish:
            callback = self.on_finish
            self.on_finish = None
            callback(self)

    async def refresh(self) -> None:
        if self.is_finished() or self.message is None:
            return
        self._rebuild()
        try:
            await self.message.edit(embed=self.embed(), view=self)
        except (discord.NotFound, discord.Forbidden):
            self._finish()

    async def on_timeout(self) -> None:
        self._finish()

    def embed(self) -> discord.Embed:
        tracks = self._tracks()
        pages = max(1, math.ceil(len(tracks) / 10))
        start = self.page * 10
        visible = tracks[start : start + 10]
        if visible:
            lines = [
                message(
                    "queue.line",
                    number=start + index + 1,
                    title=safe_text(track.title, 90),
                    uploader=safe_text(track.uploader, 50),
                )
                for index, track in enumerate(visible)
            ]
        else:
            lines = [message("queue.empty")]
        pending = len(self.session.state.pending)
        embed = discord.Embed(
            title=message("queue.title"),
            description="\n".join(lines),
            color=0x5865F2,
        )
        embed.set_footer(
            text=message(
                "queue.footer",
                page=self.page + 1,
                pages=pages,
                queued=len(tracks),
                pending=pending,
            )
        )
        return embed


class SearchView(discord.ui.LayoutView):
    def __init__(
        self,
        tracks: Sequence[Track],
        on_pick: Callable[[discord.Interaction, Track], Awaitable[None]],
        responses: Responses,
    ) -> None:
        super().__init__(timeout=60)
        self.tracks = list(tracks[:10])
        self.responses = responses
        self.message: discord.Message | None = None
        container = discord.ui.Container(accent_color=0x5865F2)
        container.add_item(discord.ui.TextDisplay(message("search.title")))
        for index, track in enumerate(self.tracks):
            details = message(
                "search.result",
                number=index + 1,
                title=safe_text(track.title, 100),
                uploader=safe_text(track.uploader, 60),
                duration=duration_text(track.duration, live=track.is_live),
            )
            if track.thumbnail:
                container.add_item(
                    discord.ui.Section(
                        details,
                        accessory=discord.ui.Thumbnail(
                            track.thumbnail,
                            description=message("search.artwork", title=safe_text(track.title, 80)),
                        ),
                    )
                )
            else:
                container.add_item(discord.ui.TextDisplay(details))
        select = discord.ui.Select(
            placeholder=message("search.select"),
            options=[
                discord.SelectOption(
                    label=message("search.option", number=index + 1, title=track.title)[:100],
                    description=message(
                        "search.option_description",
                        uploader=track.uploader,
                        duration=duration_text(track.duration, live=track.is_live),
                    )[:100],
                    value=str(index),
                )
                for index, track in enumerate(self.tracks)
            ],
        )

        async def selected(interaction: discord.Interaction) -> None:
            await dismiss_message(self, self.responses, interaction, self.message)
            await on_pick(interaction, self.tracks[int(select.values[0])])

        select.callback = selected
        container.add_item(discord.ui.ActionRow(select))
        close = discord.ui.Button(label=message("button.close"), style=discord.ButtonStyle.danger)

        async def close_view(interaction: discord.Interaction) -> None:
            await dismiss_message(self, self.responses, interaction, self.message)

        close.callback = close_view
        container.add_item(discord.ui.ActionRow(close))
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await active_interaction(self.responses, interaction)


def _parse_timestamp(value: str) -> float:
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError("format")
    try:
        numbers = [int(part) for part in parts]
    except ValueError as exc:
        raise ValueError("format") from exc
    if any(number < 0 for number in numbers) or any(number >= 60 for number in numbers[1:]):
        raise ValueError("format")
    return float(sum(number * (60**index) for index, number in enumerate(reversed(numbers))))


class SeekTimestampModal(discord.ui.Modal, title=message("button.jump")):
    timestamp = discord.ui.TextInput(
        label=message("seek.modal.label"),
        placeholder=message("seek.modal.placeholder"),
        max_length=8,
    )

    def __init__(self, view: SeekView) -> None:
        super().__init__()
        self.seek_view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            await self.seek_view.session.seek(_parse_timestamp(str(self.timestamp)), clamp=False)
        except ValueError as exc:
            key = {
                "format": "player.seek_format",
                LIVE_SEEK_ERROR: "player.live_seek",
                SEEK_RANGE_ERROR: "player.seek_range",
            }.get(str(exc), "player.nothing_playing")
            await self.seek_view.responses.send(
                interaction,
                message(key),
                lifetime="success" if key == "player.nothing_playing" else "error",
            )
            return
        await interaction.response.edit_message(embed=self.seek_view.embed(), view=self.seek_view)


class SeekView(discord.ui.View):
    def __init__(self, session: PlayerSession, responses: Responses) -> None:
        super().__init__(timeout=60)
        self.session = session
        self.responses = responses
        self.message: discord.Message | None = None
        self.ticker: asyncio.Task[None] | None = None
        for delta, label, emoji in (
            (-30, message("seek.button.thirty"), "⏪"),
            (-10, message("seek.button.ten"), "◀️"),
            (10, message("seek.button.ten"), "▶️"),
            (30, message("seek.button.thirty"), "⏩"),
        ):
            button = discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.primary,
                row=0,
            )

            async def move(interaction: discord.Interaction, amount: int = delta) -> None:
                await self.session.seek(self.session.position + amount)
                await interaction.response.edit_message(embed=self.embed(), view=self)

            button.callback = move
            self.add_item(button)
        jump = discord.ui.Button(label=message("button.jump"), emoji="✏️", row=1)

        async def jump_to(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(SeekTimestampModal(self))

        jump.callback = jump_to
        self.add_item(jump)

        close = discord.ui.Button(label=message("button.close"), style=discord.ButtonStyle.danger, row=1)

        async def close_view(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            self.stop()
            await self.stop_ticker()
            sent = interaction.message or self.message
            if sent is not None:
                try:
                    await sent.delete()
                except discord.NotFound:
                    pass
                await self.responses.cancel_expiration(sent)
            else:
                await interaction.delete_original_response()

        close.callback = close_view
        self.add_item(close)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await active_interaction(self.responses, interaction)

    def embed(self) -> discord.Embed:
        track = self.session.state.current
        position = self.session.position
        duration = track.duration if track else None
        label = format_time(position)
        if duration is not None:
            label = message("seek.position", position=label, duration=format_time(duration))
        if duration:
            bar = progress_bar(position / duration, segments=30)
        else:
            bar = progress_bar(0, segments=30)
        title = safe_text(track.title, 150) if track else message("seek.nothing")
        return discord.Embed(
            title=message("seek.title"),
            description=message("seek.description", title=title, bar=bar, position=label),
            color=0x5865F2,
        )

    def start_ticker(self) -> None:
        async def tick() -> None:
            try:
                while True:
                    await asyncio.sleep(1)
                    if self.is_finished():
                        break
                    if self.message and not (self.session.state.paused or self.session.state.dormant):
                        await self.message.edit(embed=self.embed(), view=self)
            except (asyncio.CancelledError, discord.NotFound, discord.Forbidden):
                pass

        self.ticker = asyncio.create_task(tick(), name=f"seek-view-{self.session.guild_id}")

    async def stop_ticker(self) -> None:
        ticker = self.ticker
        self.ticker = None
        if ticker and ticker is not asyncio.current_task() and not ticker.done():
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)

    async def on_timeout(self) -> None:
        await self.stop_ticker()


class ChannelPaginator(discord.ui.View):
    def __init__(
        self,
        channels: Sequence[discord.abc.GuildChannel],
        responses: Responses,
    ) -> None:
        super().__init__(timeout=60)
        self.channels = list(channels)
        self.responses = responses
        self.message: discord.Message | None = None
        self.page = 0
        previous = discord.ui.Button(label=message("button.previous_page"))
        following = discord.ui.Button(label=message("button.next_page"))
        close = discord.ui.Button(label=message("button.close"), style=discord.ButtonStyle.danger)

        def update_disabled() -> None:
            pages = max(1, math.ceil(len(self.channels) / 10))
            previous.disabled = self.page == 0
            following.disabled = self.page >= pages - 1

        async def go_previous(interaction: discord.Interaction) -> None:
            self.page = max(0, self.page - 1)
            update_disabled()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        async def go_next(interaction: discord.Interaction) -> None:
            self.page = min(max(0, math.ceil(len(self.channels) / 10) - 1), self.page + 1)
            update_disabled()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        async def close_view(interaction: discord.Interaction) -> None:
            await dismiss_message(self, self.responses, interaction, self.message)

        previous.callback = go_previous
        following.callback = go_next
        close.callback = close_view
        self.add_item(previous)
        self.add_item(following)
        self.add_item(close)
        update_disabled()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await active_interaction(self.responses, interaction)

    def embed(self) -> discord.Embed:
        pages = max(1, math.ceil(len(self.channels) / 10))
        visible = self.channels[self.page * 10 : self.page * 10 + 10]
        description = "\n".join(channel.mention for channel in visible) or message("channels.empty")
        embed = discord.Embed(
            title=message("channels.title"),
            description=description,
            color=0x5865F2,
        )
        embed.set_footer(text=message("channels.footer", page=self.page + 1, pages=pages))
        return embed
