"""The single English message catalog used by Discord and the TUI."""

from __future__ import annotations

from string import Formatter
from typing import Any

MESSAGES: dict[str, str] = {
    "app.sync.failed": "Discord command sync did not succeed within 60 seconds.",
    "app.token.missing": "DISCORD_TOKEN is missing. Open Config from the TUI to add it.",
    "command.guild_only": "This command can only be used in a server.",
    "command.voice_chat_only": "Use this command in the chat attached to a Voice or Stage channel.",
    "command.allowed_only": "This channel is not allowed to control Playify.",
    "command.manager_only": "You need Manage Server to change this setting.",
    "command.expired": "That control expired. Run the command again.",
    "voice.empty": "At least one person must be in the target Voice or Stage channel.",
    "voice.connecting": "Connecting to {channel}…",
    "voice.dormant": "Playback is dormant. Use a playback command in an occupied Voice or Stage chat to resume it.",
    "voice.recovery": "Voice permissions were lost. Playback is paused while Playify retries for 60 seconds.",
    "player.added": "Added **{title}** to the queue.",
    "player.added_many": "Added {count} tracks to the queue.",
    "player.pending": "Resolving {count} pending item(s)…",
    "player.empty": "Nothing is playing and the queue is empty.",
    "player.paused": "Paused **{title}**.",
    "player.resumed": "Resumed **{title}**.",
    "player.stopped": "Stopped playback and cleared the session.",
    "player.skipped": "Skipped **{title}**.",
    "player.replayed": "Replaying **{title}** from the beginning.",
    "player.live_seek": "Live streams cannot be replayed or seeked.",
    "player.seeked": "Moved playback to {position}.",
    "player.volume": "Volume is now {volume}%.",
    "player.loop": "Loop is {state}.",
    "player.shuffle": "Shuffled {count} queued tracks.",
    "player.queue_cleared": "Cleared {count} queued tracks and cancelled pending imports.",
    "player.queue_already_empty": "The queue is already empty.",
    "player.not_found": "I could not find anything playable for that request.",
    "player.import_failed": "The import stopped after {count} track(s): {reason}",
    "player.import_interrupted": "Playify stopped before this import finished; it will continue next start.",
    "autoplay.enabled": "Autoplay is enabled.",
    "autoplay.disabled": "Autoplay is disabled; unplayed autoplay tracks were removed.",
    "autoplay.armed": "Autoplay is armed and will generate tracks when playback starts.",
    "autoplay.failed": "Autoplay could not generate recommendations, but it remains enabled.",
    "setup.allowlist.unrestricted": "The allowlist is empty, so all accessible channels are allowed.",
    "setup.allowlist.updated": "Allowlist updated: {added} added, {removed} removed, {unchanged} unchanged.",
    "setup.channelmove": "Channel move mode is **{mode}**.",
    "status.summary": "{version} • {players} player(s) • {queued} queued track(s)",
    "error.incident": "Something unexpected happened (incident `{incident}`). Please report it at {issues_url}.",
    "error.redacted": "The request failed. Sensitive details were kept out of Discord; incident `{incident}` is in the local logs.",
}


def _required_fields(template: str) -> set[str]:
    return {
        name.split(".", 1)[0].split("[", 1)[0]
        for _, name, _, _ in Formatter().parse(template)
        if name
    }


def message(key: str, **fields: Any) -> str:
    """Format a catalog message, failing loudly on missing or extra fields."""

    try:
        template = MESSAGES[key]
    except KeyError as exc:
        raise KeyError(f"Unknown message key: {key}") from exc
    required = _required_fields(template)
    supplied = set(fields)
    if required != supplied:
        missing = sorted(required - supplied)
        extra = sorted(supplied - required)
        raise ValueError(f"Invalid fields for {key}: missing={missing}, extra={extra}")
    return template.format(**fields)


def validate_catalog() -> None:
    for key, template in MESSAGES.items():
        try:
            list(Formatter().parse(template))
        except ValueError as exc:
            raise ValueError(f"Invalid format string for {key}: {exc}") from exc
