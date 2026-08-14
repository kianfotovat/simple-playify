"""Collaborative, short-lived Discord interaction views."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence

import discord

from ..discord_utils import Responses, format_time, safe_text
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


class QueueView(discord.ui.View):
    def __init__(
        self,
        session: PlayerSession,
        responses: Responses,
        action: str = "view",
    ) -> None:
        super().__init__(timeout=120)
        self.session = session
        self.responses = responses
        self.action = action
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
                placeholder="Choose a queued track",
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
                self._rebuild()
                if self.message:
                    await self.message.edit(embed=self.embed(), view=self)

            select.callback = selected
            self.add_item(select)

        previous = discord.ui.Button(label="Previous", disabled=self.page == 0, row=1)
        refresh = discord.ui.Button(label="Refresh", style=discord.ButtonStyle.secondary, row=1)
        following = discord.ui.Button(label="Next", disabled=self.page >= pages - 1, row=1)

        async def go_previous(interaction: discord.Interaction) -> None:
            self.page -= 1
            self._rebuild()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        async def do_refresh(interaction: discord.Interaction) -> None:
            self._rebuild()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        async def go_next(interaction: discord.Interaction) -> None:
            self.page += 1
            self._rebuild()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        previous.callback = go_previous
        refresh.callback = do_refresh
        following.callback = go_next
        self.add_item(previous)
        self.add_item(refresh)
        self.add_item(following)

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
            select.disabled = True
            await interaction.response.edit_message(view=self)
            await on_pick(interaction, self.tracks[int(select.values[0])])

        select.callback = selected
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await allowed_interaction(self.responses, interaction)


class SeekView(discord.ui.View):
    def __init__(self, session: PlayerSession, responses: Responses) -> None:
        super().__init__(timeout=120)
        self.session = session
        self.responses = responses
        self.message: discord.Message | None = None
        self.ticker: asyncio.Task[None] | None = None
        for delta, label in ((-60, "−60s"), (-15, "−15s"), (15, "+15s"), (60, "+60s")):
            button = discord.ui.Button(label=label)

            async def move(interaction: discord.Interaction, amount: int = delta) -> None:
                await self.session.seek(self.session.position + amount)
                await interaction.response.edit_message(embed=self.embed(), view=self)

            button.callback = move
            self.add_item(button)
        close = discord.ui.Button(label="Close", style=discord.ButtonStyle.danger)

        async def close_view(interaction: discord.Interaction) -> None:
            self.stop()
            await interaction.response.edit_message(view=None)

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
        return discord.Embed(title="Seek", description=f"**{label}**", color=0x5865F2)

    def start_ticker(self) -> None:
        async def tick() -> None:
            try:
                while not self.is_finished():
                    await asyncio.sleep(2)
                    if self.message:
                        await self.message.edit(embed=self.embed(), view=self)
            except (asyncio.CancelledError, discord.NotFound, discord.Forbidden):
                pass

        self.ticker = asyncio.create_task(tick(), name=f"seek-view-{self.session.guild_id}")

    async def on_timeout(self) -> None:
        if self.ticker:
            self.ticker.cancel()


class ChannelPaginator(discord.ui.View):
    def __init__(self, channels: Sequence[discord.abc.GuildChannel]) -> None:
        super().__init__(timeout=120)
        self.channels = list(channels)
        self.page = 0
        previous = discord.ui.Button(label="Previous")
        following = discord.ui.Button(label="Next")

        async def go_previous(interaction: discord.Interaction) -> None:
            self.page = max(0, self.page - 1)
            await interaction.response.edit_message(embed=self.embed(), view=self)

        async def go_next(interaction: discord.Interaction) -> None:
            self.page = min(max(0, math.ceil(len(self.channels) / 10) - 1), self.page + 1)
            await interaction.response.edit_message(embed=self.embed(), view=self)

        previous.callback = go_previous
        following.callback = go_next
        self.add_item(previous)
        self.add_item(following)

    def embed(self) -> discord.Embed:
        pages = max(1, math.ceil(len(self.channels) / 10))
        visible = self.channels[self.page * 10 : self.page * 10 + 10]
        description = "\n".join(channel.mention for channel in visible) or "No channels."
        embed = discord.Embed(title="Playify channels", description=description, color=0x5865F2)
        embed.set_footer(text=f"Page {self.page + 1}/{pages}")
        return embed
