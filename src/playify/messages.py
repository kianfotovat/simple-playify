"""The single English message catalog used by Discord and the TUI."""

from __future__ import annotations

from string import Formatter
from typing import Any

MESSAGES: dict[str, str] = {
    "common.off": "off",
    "common.on": "on",
    "track.unknown_artist": "Unknown artist",
    "track.unknown_title": "Unknown title",
    "track.direct_media": "Direct media",
    "duration.live": "Live",
    "duration.unknown": "Unknown duration",
    "source.youtube": "YouTube",
    "source.soundcloud": "SoundCloud",
    "source.bandcamp": "Bandcamp",
    "source.twitch": "Twitch",
    "source.vimeo": "Vimeo",
    "source.dailymotion": "Dailymotion",
    "source.direct": "Direct media",
    "source.unknown": "Unknown source",
    "source.idle": "Idle",
    "app.sync.failed": "Discord command sync did not succeed within 60 seconds.",
    "app.token.missing": "DISCORD_TOKEN is missing. Open Config from the TUI to add it.",
    "command.guild_only": "This command can only be used in a server.",
    "command.voice_chat_only": "Use this command in the chat attached to a Voice or Stage channel.",
    "command.allowed_only": "This channel is not allowed to control Playify.",
    "command.manager_only": "You need Manage Server to change this setting.",
    "command.expired": "That control expired. Run the command again.",
    "command.description.play": "Play a link or search, or append it to the queue",
    "command.description.playnext": "Put a link or search next in the queue",
    "command.description.search": "Choose a track from search results",
    "command.description.pause": "Pause playback",
    "command.description.resume": "Resume playback, including a dormant session",
    "command.description.replay": "Replay the current finite track",
    "command.description.seek": "Seek to a timestamp or open collaborative seek controls",
    "command.description.skip": "Skip the current track",
    "command.description.previous": "Return to the previous track once",
    "command.description.stop": "Stop playback and clear the whole session",
    "command.description.reconnect": "Reconnect a dormant session without resuming",
    "command.description.queue": "Show the live committed queue",
    "command.description.remove": "Remove a committed queue entry",
    "command.description.jumpto": "Jump to a committed queue entry",
    "command.description.clearqueue": "Clear upcoming tracks and cancel pending imports",
    "command.description.shuffle": "Shuffle committed upcoming tracks",
    "command.description.loop": "Toggle looping the current track",
    "command.description.autoplay": "Toggle autoplay or seed it with one track",
    "command.description.volume": "Set the current session volume from 0 to 200",
    "command.description.nowplaying": "Show the current or dormant track",
    "command.description.status": "Show Playify's current local status",
    "command.description.setup": "Manage this server's Playify policy",
    "command.description.allowlist": "Manage allowed Playify channels",
    "command.description.allowlist_set": "Replace the channel allowlist",
    "command.description.allowlist_add": "Add up to five allowed channels",
    "command.description.allowlist_remove": "Remove up to five allowed channels",
    "command.description.allowlist_clear": "Clear the allowlist and allow every channel",
    "command.description.allowlist_show": "Show the effective allowed channels",
    "command.description.channelmove": "Configure cross-channel playback moves",
    "command.description.channelmove_show": "Show the channel move mode",
    "command.description.channelmove_set": "Set the channel move mode",
    "progress.resolving": "Resolving your request…",
    "progress.searching": "Searching…",
    "progress.autoplay": "Resolving autoplay seed…",
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
    "player.search_empty": "No search results were found.",
    "player.search_failed": "Search failed.",
    "player.request_failed": "The request failed.",
    "player.import_complete": "Added {count} track(s).",
    "player.import_partial_public": "Added {count} track(s). The successful prefix was kept; a later item failed.",
    "player.move_failed": "The channel move failed, so the request remains in the session.",
    "player.playing": "Playing **{title}**.",
    "player.history_empty": "History is empty.",
    "player.queue_empty": "The queue is empty.",
    "player.nothing_playing": "Nothing is playing.",
    "player.not_paused": "Playback is not paused.",
    "player.reconnected": "Reconnected; playback remains paused.",
    "player.not_dormant": "The session is already connected; reconnect is only for dormant sessions.",
    "player.no_session": "There is no session to adjust.",
    "player.seek_format": "Use seconds, MM:SS, or HH:MM:SS with whole, non-negative numbers.",
    "player.seek_range": "That timestamp is outside the current track.",
    "queue.missing": "That queue entry no longer exists.",
    "queue.removed": "Removed **{title}**.",
    "queue.jumped": "Jumped to **{title}**.",
    "player.import_failed": "The import stopped after {count} track(s): {reason}",
    "player.import_interrupted": "Playify stopped before this import finished; it will continue next start.",
    "autoplay.enabled": "Autoplay is enabled.",
    "autoplay.disabled": "Autoplay is disabled; unplayed autoplay tracks were removed.",
    "autoplay.armed": "Autoplay is armed and will generate tracks when playback starts.",
    "autoplay.failed": "Autoplay could not generate recommendations, but it remains enabled.",
    "setup.allowlist.unrestricted": "The allowlist is empty, so all accessible channels are allowed.",
    "setup.allowlist.updated": "Allowlist updated: {added} added, {removed} removed, {unchanged} unchanged.",
    "setup.channelmove": "Channel move mode is **{mode}**.",
    "controller.stale": "That controller is stale.",
    "controller.add.title": "Add a track",
    "controller.add.query": "Link or search",
    "controller.button.previous": "Previous",
    "controller.button.play": "Play",
    "controller.button.pause": "Pause",
    "controller.button.skip": "Skip",
    "controller.button.stop": "Stop",
    "controller.button.add": "✚︎ Add Song",
    "controller.button.volume_down": "Vol-",
    "controller.button.volume_up": "Vol+",
    "controller.button.shuffle": "Shuffle",
    "controller.button.loop": "Loop",
    "controller.button.autoplay": "Autoplay",
    "controller.button.queue": "Show Queue",
    "controller.button.jump": "Jump To",
    "controller.current.linked": "[{title}]({link})",
    "controller.current.details": "{title}\n{uploader} • {position}",
    "controller.waiting": "Waiting for a track.",
    "controller.up_next.track": "{title} — {uploader}",
    "controller.up_next.empty": "Nothing queued.",
    "controller.up_next.counts": "{upcoming} upcoming • {pending} pending",
    "controller.title.playing": "Now Playing",
    "controller.title.waiting": "Waiting for a Track",
    "controller.field.up_next": "Up Next",
    "controller.footer": "{source} • Volume: {volume}%",
    "button.close": "✖︎ Close",
    "button.previous_page": "⬅️ Previous",
    "button.next_page": "Next ➡️",
    "queue.select.remove": "Choose a queued track to remove",
    "queue.select.jump": "Choose a queued track to jump to",
    "queue.select.description": "#{number} • {uploader}",
    "queue.empty": "The committed queue is empty.",
    "queue.title": "Queue",
    "queue.line": "`{number}` {title} — {uploader}",
    "queue.footer": "Page {page}/{pages} • {queued} queued • {pending} pending",
    "search.title": "## Search Results",
    "search.result": "**{number}. {title}**\n{uploader} • {duration}",
    "search.artwork": "Artwork for {title}",
    "search.select": "Choose a result",
    "search.option": "{number}. {title}",
    "search.option_description": "{uploader} • {duration}",
    "seek.modal.title": "Jump To",
    "seek.modal.label": "Timestamp",
    "seek.modal.placeholder": "For example: 1:23 or 45",
    "seek.button.thirty": "30s",
    "seek.button.ten": "10s",
    "seek.button.jump": "Jump To",
    "seek.nothing": "Nothing playing",
    "seek.title": "Seek",
    "seek.position": "{position} / {duration}",
    "seek.description": "**{title}**\n\n`[{bar}]`\n**{position}**",
    "channels.empty": "No channels.",
    "channels.title": "Playify channels",
    "channels.footer": "Page {page}/{pages}",
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
        if not key or not template:
            raise ValueError("Catalog keys and templates must not be empty")
        try:
            list(Formatter().parse(template))
        except ValueError as exc:
            raise ValueError(f"Invalid format string for {key}: {exc}") from exc


validate_catalog()
