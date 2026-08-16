"""Collaborative, short-lived Discord interaction views."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence

import discord

from ..discord_utils import Responses, format_time, progress_bar, safe_text
from ..messages import message
from ..models import Track
from ..services.player import PlayerSession


async def allowed_interaction(responses: Responses, interaction: discord.Interaction) -> bool:
    """Apply the current server allowlist to collaborative component interactions."""

    settings = getattr(responses.bot, "server_settings", {}).get(interaction.guild_id)
    manager = (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.manage_guild
    )
    if settings and settings.allowlist and interaction.channel_id not in settings.allowlist and not manager:
        await responses.send(interaction, message("command.allowed_only"), lifetime="error")
        return False
    return True


async def dismiss_message(
    view: discord.ui.View,
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
        on_finish: Callable[["QueueView"], None] | None = None,
    ) -> None:
        super().__init__(timeout=120)
        self.session = session
        self.responses = responses
        self.action = action
        self.on_finish = on_finish
        self.page = 0
        self.message: discord.Message | None = None
        self._rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await allowed_interaction(self.responses, interaction)

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
                    "Choose a queued track to remove"
                    if self.action == "remove"
                    else "Choose a queued track to jump to"
                ),
                options=[
                    discord.SelectOption(
                        label=track.title[:100],
                        description=f"#{start + index + 1} • {track.uploader}"[:100],
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
                    await self.responses.send(
                        interaction, message("queue.missing"), lifetime="error"
                    )
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

        previous = discord.ui.Button(
            label="⬅️ Previous", disabled=self.page == 0, row=1
        )
        following = discord.ui.Button(
            label="Next ➡️", disabled=self.page >= pages - 1, row=1
        )
        close = discord.ui.Button(
            label="Close", emoji="✖️", style=discord.ButtonStyle.danger, row=1
        )

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
                f"`{start + index + 1:>3}` {safe_text(track.title, 90)} — {safe_text(track.uploader, 50)}"
                for index, track in enumerate(visible)
            ]
        else:
            lines = ["The committed queue is empty."]
        pending = len(self.session.state.pending)
        embed = discord.Embed(title="Queue", description="\n".join(lines), color=0x5865F2)
        embed.set_footer(
            text=f"Page {self.page + 1}/{pages} • {len(tracks)} queued • {pending} pending"
        )
        return embed


class SearchView(discord.ui.View):
    def __init__(
        self,
        tracks: Sequence[Track],
        on_pick: Callable[[discord.Interaction, Track], Awaitable[None]],
        responses: Responses,
    ) -> None:
        super().__init__(timeout=120)
        self.tracks = list(tracks[:10])
        self.responses = responses
        self.message: discord.Message | None = None
        select = discord.ui.Select(
            placeholder="Choose a result",
            options=[
                discord.SelectOption(
                    label=track.title[:100],
                    description=track.uploader[:100],
                    value=str(index),
                )
                for index, track in enumerate(self.tracks)
            ],
        )

        async def selected(interaction: discord.Interaction) -> None:
            await dismiss_message(self, self.responses, interaction, self.message)
            await on_pick(interaction, self.tracks[int(select.values[0])])

        select.callback = selected
        self.add_item(select)
        close = discord.ui.Button(
            label="Close", emoji="✖️", style=discord.ButtonStyle.danger, row=1
        )

        async def close_view(interaction: discord.Interaction) -> None:
            await dismiss_message(self, self.responses, interaction, self.message)

        close.callback = close_view
        self.add_item(close)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await allowed_interaction(self.responses, interaction)


def _parse_timestamp(value: str) -> float:
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError("format")
    try:
        numbers = [int(part) for part in parts]
    except ValueError as exc:
        raise ValueError("format") from exc
    if any(number < 0 for number in numbers) or any(
        number >= 60 for number in numbers[1:]
    ):
        raise ValueError("format")
    return float(
        sum(number * (60**index) for index, number in enumerate(reversed(numbers)))
    )


class SeekTimestampModal(discord.ui.Modal, title="Jump To"):
    timestamp = discord.ui.TextInput(
        label="Timestamp",
        placeholder="For example: 1:23 or 45",
        max_length=8,
    )

    def __init__(self, view: "SeekView") -> None:
        super().__init__()
        self.seek_view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            await self.seek_view.session.seek(
                _parse_timestamp(str(self.timestamp)), clamp=False
            )
        except ValueError as exc:
            key = {
                "format": "player.seek_format",
                "live streams cannot be seeked": "player.live_seek",
                "timestamp is outside the current track": "player.seek_range",
            }.get(str(exc), "player.nothing_playing")
            await self.seek_view.responses.send(
                interaction, message(key), lifetime="error"
            )
            return
        await interaction.response.edit_message(
            embed=self.seek_view.embed(), view=self.seek_view
        )


class SeekView(discord.ui.View):
    def __init__(self, session: PlayerSession, responses: Responses) -> None:
        super().__init__(timeout=120)
        self.session = session
        self.responses = responses
        self.message: discord.Message | None = None
        self.ticker: asyncio.Task[None] | None = None
        for delta, label, emoji in (
            (-30, "30s", "⏪"),
            (-10, "10s", "◀️"),
            (10, "10s", "▶️"),
            (30, "30s", "⏩"),
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
        jump = discord.ui.Button(label="Jump To", emoji="✏️", row=1)

        async def jump_to(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(SeekTimestampModal(self))

        jump.callback = jump_to
        self.add_item(jump)

        close = discord.ui.Button(
            label="Close", emoji="✖️", style=discord.ButtonStyle.danger, row=1
        )

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
        return await allowed_interaction(self.responses, interaction)

    def embed(self) -> discord.Embed:
        track = self.session.state.current
        position = self.session.position
        duration = track.duration if track else None
        label = format_time(position)
        if duration is not None:
            label += f" / {format_time(duration)}"
        if duration:
            bar = progress_bar(position / duration, segments=30)
        else:
            bar = progress_bar(0, segments=30)
        title = safe_text(track.title, 150) if track else "Nothing playing"
        return discord.Embed(
            title="Seek",
            description=f"**{title}**\n\n`[{bar}]`\n**{label}**",
            color=0x5865F2,
        )

    def start_ticker(self) -> None:
        async def tick() -> None:
            try:
                while True:
                    await asyncio.sleep(1)
                    if self.is_finished():
                        break
                    if self.message and not (
                        self.session.state.paused or self.session.state.dormant
                    ):
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
        super().__init__(timeout=120)
        self.channels = list(channels)
        self.responses = responses
        self.message: discord.Message | None = None
        self.page = 0
        previous = discord.ui.Button(label="⬅️ Previous")
        following = discord.ui.Button(label="Next ➡️")
        close = discord.ui.Button(
            label="Close", emoji="✖️", style=discord.ButtonStyle.danger
        )

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
        return await allowed_interaction(self.responses, interaction)

    def embed(self) -> discord.Embed:
        pages = max(1, math.ceil(len(self.channels) / 10))
        visible = self.channels[self.page * 10 : self.page * 10 + 10]
        description = "\n".join(channel.mention for channel in visible) or "No channels."
        embed = discord.Embed(title="Playify channels", description=description, color=0x5865F2)
        embed.set_footer(text=f"Page {self.page + 1}/{pages}")
        return embed
