"""Queue, autoplay, voice, and dormant-session orchestration."""

from __future__ import annotations

import asyncio
import logging
import random
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

import discord

from ..constants import BIN_DIR
from ..models import PendingImport, PlayerSnapshot, Track
from ..storage import Storage
from .extractor import Extractor, ResolveError

LOGGER = logging.getLogger(__name__)

ChangeCallback = Callable[["PlayerSession", str], Awaitable[None]]


def ffmpeg_path() -> str:
    local_name = "ffmpeg.exe" if __import__("os").name == "nt" else "ffmpeg"
    local = BIN_DIR / local_name
    if local.is_file():
        return str(local)
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError("FFmpeg is not available in bin/ or PATH")


def _human_count(channel: discord.VoiceChannel | discord.StageChannel) -> int:
    return sum(1 for member in channel.members if not member.bot)


class PlayerSession:
    def __init__(
        self,
        bot: discord.Client,
        storage: Storage,
        extractor: Extractor,
        state: PlayerSnapshot,
        on_change: ChangeCallback | None = None,
    ) -> None:
        self.bot = bot
        self.storage = storage
        self.extractor = extractor
        self.state = state
        self.on_change = on_change
        self.voice: discord.VoiceClient | None = None
        self.lock = asyncio.Lock()
        self.pending_tasks: dict[str, asyncio.Task[None]] = {}
        self.pending_results: dict[str, tuple[list[Track], str | None] | BaseException] = {}
        self.import_outcomes: dict[str, tuple[int, str | None]] = {}
        self.import_waiters: dict[str, asyncio.Future[tuple[int, str | None]]] = {}
        self.priority_anchor: list[str] = []
        self.playback_started_at: float | None = None
        self.start_offset = state.position
        self._playback_generation = 0
        self.idle_task: asyncio.Task[None] | None = None
        self.autoplay_task: asyncio.Task[None] | None = None
        self.voice_recovery_task: asyncio.Task[None] | None = None
        self._stream_retry_ids: set[str] = set()
        self.expected_disconnect_until = 0.0

    @property
    def guild_id(self) -> int:
        return self.state.guild_id

    @property
    def position(self) -> float:
        if (
            self.playback_started_at is not None
            and not self.state.paused
            and not self.state.dormant
        ):
            return self.start_offset + (time.monotonic() - self.playback_started_at)
        return self.state.position

    @property
    def active(self) -> bool:
        return bool(self.voice and self.voice.is_connected() and not self.state.dormant)

    async def changed(self, event: str) -> None:
        self.state.position = max(0.0, self.position)
        await self.storage.save_player(self.state)
        if self.on_change:
            await self.on_change(self, event)

    def start_pending_imports(self) -> None:
        for pending in list(self.state.pending):
            self._start_pending(pending)

    async def enqueue(
        self,
        request: str,
        *,
        requested_by: int | None,
        priority: bool = False,
    ) -> PendingImport:
        async with self.lock:
            if priority and not any(value.priority for value in self.state.pending):
                self.priority_anchor.clear()
            pending = PendingImport(request, priority=priority, requested_by=requested_by)
            self.state.pending.append(pending)
            self.import_waiters[pending.import_id] = asyncio.get_running_loop().create_future()
            await self.changed("import_pending")
            self._start_pending(pending)
            return pending

    async def add_resolved(self, tracks: list[Track], *, priority: bool = False) -> int:
        """Commit already-resolved search selections without re-extracting them."""

        async with self.lock:
            if priority:
                self.state.queue[0:0] = tracks
            else:
                self.state.queue.extend(tracks)
            await self.changed("tracks_added")
            if self.state.current is None and self.active and self.state.queue:
                await self._advance(force=True)
            return len(tracks)

    def _start_pending(self, pending: PendingImport) -> None:
        if pending.import_id in self.pending_tasks:
            return
        task = asyncio.create_task(self._resolve_pending(pending), name=f"import-{pending.import_id}")
        self.pending_tasks[pending.import_id] = task

    async def _resolve_pending(self, pending: PendingImport) -> None:
        try:
            result: tuple[list[Track], str | None] | BaseException = (
                await self.extractor.resolve_request(
                    pending.query, requested_by=pending.requested_by
                )
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            result = exc
        async with self.lock:
            self.pending_results[pending.import_id] = result
            await self._drain_pending()

    async def _drain_pending(self) -> None:
        while self.state.pending:
            pending = self.state.pending[0]
            if pending.import_id not in self.pending_results:
                break
            result = self.pending_results.pop(pending.import_id)
            self.pending_tasks.pop(pending.import_id, None)
            self.state.pending.pop(0)
            if isinstance(result, BaseException):
                if not isinstance(result, asyncio.CancelledError):
                    LOGGER.warning("Import failed for guild %s: %s", self.guild_id, result)
                    self._finish_import(pending.import_id, 0, str(result))
                    await self.changed("import_failed")
                else:
                    self._finish_import(pending.import_id, 0, "cancelled")
                continue
            tracks, partial_error = result
            if pending.priority:
                index = 0
                for occurrence_id in self.priority_anchor:
                    found = next(
                        (
                            queue_index
                            for queue_index, track in enumerate(self.state.queue)
                            if track.occurrence_id == occurrence_id
                        ),
                        None,
                    )
                    if found is not None:
                        index = max(index, found + 1)
                self.state.queue[index:index] = tracks
                self.priority_anchor.extend(track.occurrence_id for track in tracks)
            else:
                self.state.queue.extend(tracks)
            self._finish_import(pending.import_id, len(tracks), partial_error)
            await self.changed("import_partial" if partial_error else "import_complete")
            if self.state.current is None and self.active and self.state.queue:
                await self._advance(force=True)
        if not self.state.pending and self.active and not self.state.queue:
            if self.state.current is not None and self.state.autoplay_enabled:
                self.schedule_autoplay()
            elif self.state.current is None:
                if self.state.autoplay_enabled and self.state.history:
                    self.schedule_autoplay(self.state.history[-1])
                else:
                    self._schedule_idle_disconnect()

    def _finish_import(self, import_id: str, count: int, error: str | None) -> None:
        outcome = (count, error)
        waiter = self.import_waiters.pop(import_id, None)
        if waiter:
            self.import_outcomes[import_id] = outcome
            if not waiter.done():
                waiter.set_result(outcome)

    async def wait_import(self, pending: PendingImport) -> tuple[int, str | None]:
        outcome = self.import_outcomes.pop(pending.import_id, None)
        if outcome is not None:
            return outcome
        waiter = self.import_waiters.get(pending.import_id)
        if waiter is None:
            return (0, "cancelled")
        outcome = await waiter
        self.import_outcomes.pop(pending.import_id, None)
        return outcome

    async def cancel_pending(self) -> int:
        count = len(self.state.pending)
        for task in self.pending_tasks.values():
            task.cancel()
        for import_id in list(self.import_waiters):
            self._finish_import(import_id, 0, "cancelled")
        self.pending_tasks.clear()
        self.pending_results.clear()
        self.state.pending.clear()
        self.priority_anchor.clear()
        if self.autoplay_task:
            self.autoplay_task.cancel()
            self.autoplay_task = None
        return count

    async def connect(
        self,
        channel: discord.VoiceChannel | discord.StageChannel,
        text_channel_id: int,
        *,
        resume: bool = True,
    ) -> None:
        if _human_count(channel) == 0:
            raise ValueError("target voice channel is empty")
        if self.voice and self.voice.is_connected():
            if self.voice.channel.id != channel.id:
                await self.voice.move_to(channel)
        else:
            self.voice = await channel.connect(reconnect=True, timeout=30, self_deaf=True)
        self.state.voice_channel_id = channel.id
        self.state.text_channel_id = text_channel_id
        self.state.dormant = False
        await self.changed("voice_connected")
        try:
            if resume and self.state.current:
                await self._play_current(self.state.position)
            elif resume and self.state.queue:
                await self._advance(force=True)
            elif not resume and self.state.current:
                await self._play_current(self.state.position)
                if self.voice:
                    self.voice.pause()
                self.state.paused = True
                self.state.position = self.start_offset
                self.playback_started_at = None
                await self.changed("reconnected_paused")
            elif not resume:
                self.state.paused = True
                await self.changed("reconnected_paused")
        except Exception:
            await self.become_dormant("voice_start_failed")
            raise

    async def move_to(
        self, channel: discord.VoiceChannel | discord.StageChannel, text_channel_id: int
    ) -> None:
        try:
            await self.connect(channel, text_channel_id)
        except Exception:
            if not self.state.dormant:
                await self.become_dormant("voice_move_failed")
            raise

    async def become_dormant(self, event: str = "dormant") -> None:
        self.state.position = self.position
        self.playback_started_at = None
        self._playback_generation += 1
        if self.voice:
            if self.voice.source:
                self.voice.stop()
            try:
                self.expected_disconnect_until = time.monotonic() + 10
                await self.voice.disconnect(force=True)
            except Exception:
                LOGGER.exception("Voice disconnect failed for guild %s", self.guild_id)
        self.voice = None
        self.state.dormant = True
        self.state.paused = True
        await self.changed(event)

    async def discard_dormant_current(self) -> None:
        async with self.lock:
            if self.state.dormant and self.state.current:
                self.state.history.append(self.state.current)
                self.state.current = None
                self.state.position = 0
                await self.changed("dormant_current_discarded")

    def _audio_source(self, track: Track, position: float) -> discord.AudioSource:
        stream = track.stream_url or track.webpage_url
        before = "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
        if position > 0 and not track.is_live:
            before += f" -ss {position:.3f}"
        source = discord.FFmpegPCMAudio(
            stream,
            executable=ffmpeg_path(),
            before_options=before,
            options="-vn -loglevel warning",
        )
        return discord.PCMVolumeTransformer(source, volume=self.state.volume / 100)

    async def _play_current(self, position: float = 0.0) -> None:
        track = self.state.current
        if track is None or not self.voice or not self.voice.is_connected():
            return
        if not track.stream_url and track.source != "direct":
            track = await self.extractor.refresh_stream(track)
            self.state.current = track
        self._playback_generation += 1
        generation = self._playback_generation
        if self.voice.is_playing() or self.voice.is_paused():
            self.voice.stop()
            await asyncio.sleep(0)
        if self.idle_task:
            self.idle_task.cancel()
            self.idle_task = None
        self.start_offset = 0.0 if track.is_live else max(0.0, position)
        self.playback_started_at = None
        source = self._audio_source(track, self.start_offset)
        self.state.position = self.start_offset
        self.state.paused = False
        self.state.dormant = False
        self.playback_started_at = time.monotonic()
        loop = asyncio.get_running_loop()

        def after(error: Exception | None) -> None:
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self._after_track(generation, error), name=f"after-{self.guild_id}"
                )
            )

        try:
            self.voice.play(source, after=after)
        except Exception:
            source.cleanup()
            self.playback_started_at = None
            self.state.paused = True
            raise
        await self.changed("track_started")

    async def _after_track(self, generation: int, error: Exception | None) -> None:
        async with self.lock:
            if generation != self._playback_generation:
                return
            current = self.state.current
            if error and current and current.occurrence_id not in self._stream_retry_ids:
                resume_position = self.position
                self.state.position = resume_position
                self.playback_started_at = None
                self._stream_retry_ids.add(current.occurrence_id)
                try:
                    self.state.current = await self.extractor.refresh_stream(current)
                    await self._play_current(resume_position)
                    return
                except Exception:
                    LOGGER.exception("Stream refresh failed for guild %s", self.guild_id)
            await self._advance(force=error is not None)

    async def _advance(self, *, force: bool) -> Track | None:
        previous = self.state.current
        if previous and self.state.loop_current and not force:
            self.state.position = 0
            try:
                await self._play_current(0)
                return previous
            except Exception:
                LOGGER.exception("Could not loop track in guild %s; advancing", self.guild_id)
        if previous:
            self.state.history.append(previous)
            self._stream_retry_ids.discard(previous.occurrence_id)
        self.state.current = None
        while self.state.queue:
            self.state.current = self.state.queue.pop(0)
            self.state.position = 0
            self.playback_started_at = None
            if self.active:
                try:
                    await self._play_current(0)
                except Exception:
                    failed = self.state.current
                    if failed:
                        self.state.history.append(failed)
                        self._stream_retry_ids.discard(failed.occurrence_id)
                    LOGGER.exception(
                        "Could not start queued track in guild %s; trying the next one",
                        self.guild_id,
                    )
                    self.state.current = None
                    continue
            else:
                await self.changed("track_selected")
            if (
                self.state.autoplay_enabled
                and not self.state.queue
                and not self.state.pending
            ):
                self.schedule_autoplay()
            return self.state.current
        self.state.position = 0
        self.playback_started_at = None
        self.state.paused = True
        await self.changed("queue_exhausted")
        if self.state.autoplay_enabled and previous and not self.state.pending:
            self.schedule_autoplay(previous)
        elif not self.state.pending:
            self._schedule_idle_disconnect()
        return None

    def _schedule_idle_disconnect(self) -> None:
        if self.idle_task:
            self.idle_task.cancel()

        async def idle() -> None:
            try:
                await asyncio.sleep(60)
                if self.state.current is None and not self.state.queue and not self.state.pending:
                    await self.stop()
            except asyncio.CancelledError:
                pass

        self.idle_task = asyncio.create_task(idle(), name=f"idle-{self.guild_id}")

    async def pause(self) -> bool:
        async with self.lock:
            if self.state.dormant or not self.voice or not self.voice.is_playing():
                return False
            self.state.position = self.position
            self.voice.pause()
            self.state.paused = True
            self.playback_started_at = None
            await self.changed("paused")
            return True

    async def resume(self) -> bool:
        async with self.lock:
            if self.state.dormant:
                return False
            if not self.voice or not self.voice.is_paused():
                return False
            self.voice.resume()
            self.start_offset = self.state.position
            self.playback_started_at = time.monotonic()
            self.state.paused = False
            await self.changed("resumed")
            return True

    async def seek(self, position: float, *, clamp: bool = True) -> float:
        async with self.lock:
            current = self.state.current
            if current is None:
                raise ValueError("nothing is playing")
            if current.is_live:
                raise ValueError("live streams cannot be seeked")
            position = float(position)
            if not clamp and (
                position < 0 or (current.duration is not None and position > current.duration)
            ):
                raise ValueError("timestamp is outside the current track")
            upper = current.duration if current.duration is not None else max(0.0, position)
            position = max(0.0, min(position, upper))
            self.state.position = position
            if self.active:
                was_paused = self.state.paused
                await self._play_current(position)
                if was_paused and self.voice:
                    self.voice.pause()
                    self.state.paused = True
                    self.state.position = position
                    self.playback_started_at = None
                    await self.changed("seeked")
            else:
                await self.changed("seeked")
            return position

    async def replay(self) -> bool:
        if not self.state.current or self.state.current.is_live:
            return False
        await self.seek(0)
        return True

    async def skip(self) -> Track | None:
        async with self.lock:
            if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
                self._playback_generation += 1
                self.voice.stop()
            return await self._advance(force=True)

    async def previous(self) -> Track | None:
        async with self.lock:
            if not self.state.history:
                return None
            if self.state.current:
                self.state.queue.insert(0, self.state.current)
            self.state.current = self.state.history.pop()
            self.state.position = 0
            if self.active:
                await self._play_current(0)
            else:
                await self.changed("previous")
            return self.state.current

    async def stop(self) -> None:
        async with self.lock:
            await self.cancel_pending()
            current_task = asyncio.current_task()
            for task in (self.idle_task, self.autoplay_task, self.voice_recovery_task):
                if task and task is not current_task:
                    task.cancel()
            self._playback_generation += 1
            if self.voice:
                if self.voice.is_playing() or self.voice.is_paused():
                    self.voice.stop()
                self.expected_disconnect_until = time.monotonic() + 10
                try:
                    await self.voice.disconnect(force=True)
                except Exception:
                    LOGGER.exception("Voice disconnect failed while stopping guild %s", self.guild_id)
            self.voice = None
            controller = (self.state.controller_channel_id, self.state.controller_message_id)
            if all(controller):
                await self.storage.set_controller_cleanup(self.guild_id, *controller)  # type: ignore[arg-type]
                if self.on_change:
                    await self.on_change(self, "stopping")
            self.state = PlayerSnapshot(self.guild_id)
            await self.storage.delete_player(self.guild_id)
            if self.on_change:
                await self.on_change(self, "stopped")

    async def clear_queue(self) -> int:
        async with self.lock:
            count = len(self.state.queue) + len(self.state.pending)
            await self.cancel_pending()
            self.state.queue.clear()
            await self.changed("queue_cleared")
            return count

    async def shuffle(self) -> int:
        async with self.lock:
            random.shuffle(self.state.queue)
            self.priority_anchor.clear()
            await self.changed("queue_shuffled")
            return len(self.state.queue)

    async def remove(self, occurrence_id: str) -> Track | None:
        async with self.lock:
            for index, track in enumerate(self.state.queue):
                if track.occurrence_id == occurrence_id:
                    removed = self.state.queue.pop(index)
                    await self.changed("queue_removed")
                    return removed
            return None

    async def jump(self, occurrence_id: str) -> Track | None:
        async with self.lock:
            index = next(
                (index for index, track in enumerate(self.state.queue) if track.occurrence_id == occurrence_id),
                None,
            )
            if index is None:
                return None
            self.state.queue = self.state.queue[index:]
            for pending in self.state.pending:
                pending.priority = False
            self.priority_anchor.clear()
            if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
                self._playback_generation += 1
                self.voice.stop()
            return await self._advance(force=True)

    async def set_volume(self, volume: int) -> int:
        async with self.lock:
            self.state.volume = max(0, min(200, int(volume)))
            if self.voice and isinstance(self.voice.source, discord.PCMVolumeTransformer):
                self.voice.source.volume = self.state.volume / 100
            await self.changed("volume")
            return self.state.volume

    async def toggle_loop(self) -> bool:
        async with self.lock:
            if self.state.current is None:
                raise ValueError("nothing is playing")
            self.state.loop_current = not self.state.loop_current
            await self.changed("loop")
            return self.state.loop_current

    async def set_autoplay(self, enabled: bool) -> bool:
        async with self.lock:
            self.state.autoplay_enabled = enabled
            if not enabled:
                if self.autoplay_task:
                    self.autoplay_task.cancel()
                    self.autoplay_task = None
                self.state.queue = [
                    track for track in self.state.queue if track.provenance != "autoplay"
                ]
            await self.changed("autoplay")
            if enabled and self.active and self.state.current and not self.state.queue and not self.state.pending:
                self.schedule_autoplay()
            elif (
                not enabled
                and self.active
                and self.state.current is None
                and not self.state.queue
                and not self.state.pending
            ):
                self._schedule_idle_disconnect()
            return enabled

    def schedule_autoplay(self, seed: Track | None = None, *, force: bool = False) -> None:
        if self.autoplay_task and not self.autoplay_task.done():
            return
        if self.state.queue and not force:
            return
        if not self.active:
            return
        seed = seed or self.state.current
        if seed and seed.source == "direct":
            seed = next(
                (track for track in reversed(self.state.history) if track.source != "direct"),
                seed,
            )
        if seed is None or not self.state.autoplay_enabled:
            return

        async def generate() -> None:
            try:
                generated = await self.extractor.recommendations(seed)
                async with self.lock:
                    if not self.state.autoplay_enabled:
                        return
                    if not generated:
                        await self.changed("autoplay_failed")
                        if self.state.current is None and not self.state.queue:
                            self._schedule_idle_disconnect()
                        return
                    self.state.queue.extend(generated)
                    await self.changed("autoplay_generated")
                    if self.state.current is None and self.active and self.state.queue:
                        await self._advance(force=True)
            except asyncio.CancelledError:
                pass
            except Exception:
                LOGGER.exception("Autoplay failed for guild %s", self.guild_id)
                await self.changed("autoplay_failed")
                if self.state.current is None and not self.state.queue:
                    self._schedule_idle_disconnect()

        self.autoplay_task = asyncio.create_task(generate(), name=f"autoplay-{self.guild_id}")

    async def autoplay_query(self, query: str, requested_by: int | None) -> Track:
        tracks, _ = await self.extractor.resolve_request(query, requested_by=requested_by)
        if len(tracks) != 1:
            raise ResolveError("autoplay seed must resolve to exactly one track")
        seed = tracks[0]
        async with self.lock:
            self.state.autoplay_enabled = True
            if self.state.current is None:
                self.state.current = seed
                self.state.position = 0
                if self.active:
                    await self._play_current(0)
            else:
                self.state.queue.insert(0, seed)
            await self.changed("autoplay_seed")
            self.schedule_autoplay(seed, force=True)
        return seed

    async def recover_voice(self) -> None:
        if self.voice_recovery_task and not self.voice_recovery_task.done():
            return

        async def recover() -> None:
            deadline = time.monotonic() + 60
            delay = 1.0
            while time.monotonic() < deadline:
                await asyncio.sleep(delay + random.random())
                channel = self.bot.get_channel(self.state.voice_channel_id or 0)
                if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                    member = channel.guild.me
                    permissions = channel.permissions_for(member) if member else None
                    if not permissions or not permissions.connect or not permissions.speak:
                        delay = min(15.0, delay * 2)
                        continue
                    try:
                        await self.connect(channel, self.state.text_channel_id or channel.id)
                        return
                    except Exception:
                        pass
                delay = min(15.0, delay * 2)
            await self.become_dormant("voice_recovery_failed")

        self.voice_recovery_task = asyncio.create_task(recover(), name=f"voice-recover-{self.guild_id}")


class PlayerManager:
    def __init__(
        self,
        bot: discord.Client,
        storage: Storage,
        extractor: Extractor,
        on_change: ChangeCallback | None = None,
    ) -> None:
        self.bot = bot
        self.storage = storage
        self.extractor = extractor
        self.on_change = on_change
        self.sessions: dict[int, PlayerSession] = {}

    async def restore(self) -> None:
        for guild_id, state in (await self.storage.load_players()).items():
            state.dormant = True
            state.paused = True
            session = PlayerSession(
                self.bot, self.storage, self.extractor, state, self.on_change
            )
            self.sessions[guild_id] = session
            session.start_pending_imports()

    def get(self, guild_id: int) -> PlayerSession:
        session = self.sessions.get(guild_id)
        if session is None:
            session = PlayerSession(
                self.bot,
                self.storage,
                self.extractor,
                PlayerSnapshot(guild_id),
                self.on_change,
            )
            self.sessions[guild_id] = session
        return session

    async def auto_resume(self) -> None:
        for session in self.sessions.values():
            channel = self.bot.get_channel(session.state.voice_channel_id or 0)
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                continue
            guild = channel.guild
            member = guild.me
            permissions = channel.permissions_for(member) if member else None
            if (
                _human_count(channel) > 0
                and permissions
                and permissions.connect
                and permissions.speak
            ):
                try:
                    await session.connect(
                        channel,
                        session.state.text_channel_id or channel.id,
                        resume=True,
                    )
                except Exception:
                    LOGGER.exception("Could not auto-resume guild %s", session.guild_id)

    async def shutdown(self) -> None:
        for session in self.sessions.values():
            try:
                session.state.position = session.position
                session.state.paused = True
                session.state.dormant = True
                session.playback_started_at = None
                tasks = [
                    *session.pending_tasks.values(),
                    session.idle_task,
                    session.autoplay_task,
                    session.voice_recovery_task,
                ]
                active_tasks = [task for task in tasks if task and not task.done()]
                for task in active_tasks:
                    task.cancel()
                await session.changed("shutdown")
                if session.voice:
                    session._playback_generation += 1
                    if session.voice.is_playing() or session.voice.is_paused():
                        session.voice.stop()
                    session.expected_disconnect_until = time.monotonic() + 10
                    try:
                        await session.voice.disconnect(force=True)
                    except Exception:
                        LOGGER.exception(
                            "Voice disconnect failed while closing guild %s",
                            session.guild_id,
                        )
                    session.voice = None
                if active_tasks:
                    await asyncio.gather(*active_tasks, return_exceptions=True)
            except Exception:
                LOGGER.exception("Could not close player %s", session.guild_id)
