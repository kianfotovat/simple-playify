"""Persistent categorized runtime settings menu."""

from __future__ import annotations

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from src.playify.config import Config
from src.playify.messages import message

from .key_input import ask_with_escape

CHOICES = {
    "persistence_mode": ["full", "settings"],
    "updates_enabled": ["true", "false"],
    "soundcloud_fallback": ["true", "false"],
    "ip_mode": ["auto", "ipv4"],
    "color_mode": ["auto", "v2", "ansi", "none"],
    "symbol_mode": ["auto", "unicode", "ascii"],
    "bot_status_type": ["none", "playing", "listening", "watching", "competing"],
}


def _row(category: str, key: str) -> tuple[str, str, str]:
    return (
        message(f"tui.settings.category.{category}"),
        key,
        message(f"tui.settings.{key}"),
    )


ROWS = (
    _row("playback", "persistence_mode"),
    _row("playback", "tidal_country"),
    _row("playback", "soundcloud_fallback"),
    _row("network", "private_media_allowlist"),
    _row("network", "ip_mode"),
    _row("network", "youtube_clients"),
    _row("performance", "worker_count"),
    _row("performance", "http_concurrency"),
    _row("interface", "tui_refresh"),
    _row("interface", "color_mode"),
    _row("interface", "symbol_mode"),
    _row("interface", "controller_idle_image"),
    _row("presence", "bot_status_type"),
    _row("presence", "bot_status_text"),
    _row("updates", "updates_enabled"),
)


def _display(value) -> str:
    if isinstance(value, list):
        return ", ".join(value) or message("tui.settings.empty")
    return str(value)


def _convert(key: str, raw: str):
    if key in {"updates_enabled", "soundcloud_fallback"}:
        return raw == "true"
    if key in {"private_media_allowlist", "youtube_clients"}:
        return [value.strip() for value in raw.split(",") if value.strip()]
    if key == "tidal_country":
        value = raw.strip().upper()
        if len(value) != 2 or not value.isalpha():
            raise ValueError(message("tui.settings.country_error"))
        return value
    if key == "worker_count":
        return "auto" if raw == "auto" else max(1, min(8, int(raw)))
    if key == "http_concurrency":
        return "auto" if raw == "auto" else max(1, min(16, int(raw)))
    if key == "tui_refresh":
        return "auto" if raw == "auto" else max(1, min(30, int(raw)))
    return raw.strip()


def run_settings(console: Console) -> str | None:
    """Return the highest restart scope required by changes."""

    Config.reload()
    restart_required: str | None = None
    while True:
        table = Table(title=message("tui.settings.title"), show_lines=False)
        table.add_column("#", justify="right")
        table.add_column(message("tui.settings.column.category"))
        table.add_column(message("tui.settings.column.setting"))
        table.add_column(message("tui.settings.column.current"))
        table.add_column(message("tui.settings.column.purpose"))
        for index, (category, key, description) in enumerate(ROWS, 1):
            table.add_row(str(index), category, key, _display(Config.get(key)), description)
        console.clear()
        console.print(table)
        if restart_required:
            console.print(message("tui.settings.restart_required", scope=restart_required))
        selection = ask_with_escape(console, message("tui.settings.select"), default="esc")
        if selection in {"esc", "q", "back"}:
            return restart_required
        try:
            index = int(selection) - 1
            _, key, _ = ROWS[index]
        except (ValueError, IndexError):
            continue
        current = Config.get(key)
        choices = CHOICES.get(key)
        raw = Prompt.ask(
            message("tui.settings.new", setting=key),
            choices=choices,
            default=_display(current) if not isinstance(current, list) else ",".join(current),
        )
        try:
            Config.set(key, _convert(key, raw))
        except (TypeError, ValueError) as exc:
            console.print(message("tui.settings.error", error=exc))
            console.input(message("tui.settings.press_enter"))
            continue
        if key in {"color_mode", "symbol_mode"}:
            restart_required = message("tui.scope.launcher")
        elif restart_required is None:
            restart_required = message("tui.scope.bot")
