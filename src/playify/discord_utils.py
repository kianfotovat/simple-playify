"""Safe, public Discord responses with persistent expiration jobs."""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import discord

from .constants import ISSUES_URL
from .logging_utils import redact
from .messages import message
from .storage import Storage

LOGGER = logging.getLogger(__name__)

Lifetime = Literal["success", "interactive", "error", "controller", "none"]
LIFETIMES: dict[Lifetime, int | None] = {
    "success": 30,
    "interactive": 120,
    "error": 300,
    "controller": None,
    "none": None,
}


def safe_text(value: Any, maximum: int = 1_900) -> str:
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))
    return text if len(text) <= maximum else text[: maximum - 1] + "…"


def format_time(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3_600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def progress_bar(fraction: float) -> str:
    filled = max(0, min(10, round(fraction * 10)))
    return "█" * filled + "░" * (10 - filled)


class Responses:
    def __init__(self, bot: discord.Client, storage: Storage) -> None:
        self.bot = bot
        self.storage = storage
        self.tasks: dict[tuple[int, int], asyncio.Task[None]] = {}

    async def restore_deletions(self) -> None:
        for channel_id, message_id, delete_after, kind in await self.storage.list_deletion_jobs():
            self._spawn_deletion(channel_id, message_id, delete_after, kind)

    async def close(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    async def send(
        self,
        interaction: discord.Interaction,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        lifetime: Lifetime = "success",
    ) -> discord.InteractionMessage | discord.WebhookMessage:
        kwargs: dict[str, Any] = {
            "silent": True,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        if interaction.response.is_done():
            sent = await interaction.followup.send(wait=True, **kwargs)
        else:
            await interaction.response.send_message(**kwargs)
            sent = await interaction.original_response()
        await self.expire(sent, lifetime)
        return sent

    async def progress(
        self, interaction: discord.Interaction, label: str
    ) -> discord.InteractionMessage | discord.WebhookMessage:
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True, ephemeral=False)
        return await self.send(
            interaction,
            f"{safe_text(label)}\n`[{progress_bar(0)}]`",
            lifetime="none",
        )

    async def finish_progress(
        self,
        sent: discord.InteractionMessage | discord.WebhookMessage,
        content: str,
        *,
        failed: bool = False,
    ) -> None:
        try:
            await sent.edit(
                content=f"{content}\n`[{progress_bar(1)}]`",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.expire(sent, "error" if failed else "success")
        except (discord.NotFound, discord.Forbidden):
            pass

    async def unexpected(self, interaction: discord.Interaction, exc: BaseException) -> None:
        incident = secrets.token_hex(4)
        LOGGER.error(
            "Incident %s: %s",
            incident,
            redact(exc),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        await self.send(
            interaction,
            message("error.incident", incident=incident, issues_url=ISSUES_URL),
            lifetime="error",
        )

    async def expire(
        self,
        sent: discord.Message,
        lifetime: Lifetime,
    ) -> None:
        seconds = LIFETIMES[lifetime]
        if seconds is None:
            return
        delete_after = datetime.now(UTC) + timedelta(seconds=seconds)
        await self.storage.add_deletion_job(
            sent.channel.id, sent.id, delete_after, lifetime
        )
        self._spawn_deletion(sent.channel.id, sent.id, delete_after, lifetime)

    def _spawn_deletion(
        self, channel_id: int, message_id: int, delete_after: datetime, kind: str
    ) -> None:
        key = (channel_id, message_id)
        existing = self.tasks.pop(key, None)
        if existing:
            existing.cancel()

        async def delete_once() -> None:
            delay = max(0.0, (delete_after.astimezone(UTC) - datetime.now(UTC)).total_seconds())
            completed = False
            try:
                await asyncio.sleep(delay)
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        channel = None
                if channel and hasattr(channel, "fetch_message"):
                    try:
                        sent = await channel.fetch_message(message_id)  # type: ignore[union-attr]
                        await sent.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
                completed = True
            finally:
                if completed:
                    await self.storage.remove_deletion_job(channel_id, message_id)
                self.tasks.pop(key, None)

        self.tasks[key] = asyncio.create_task(delete_once(), name=f"delete-{message_id}")
