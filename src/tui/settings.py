"""Persistent categorized runtime settings menu."""

from __future__ import annotations

from rich.console import Console
from rich.prompt import Prompt
from rich.text import Text

from src.playify.config import Config
from src.playify.messages import message

from .key_input import ask_with_escape
from .menu import menu_layout

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


def _label(key: str) -> str:
    return message(f"tui.settings.label.{key}")


def _settings_menu(console: Console, restart_required: str | None) -> str:
    footer = Text()
    footer.append(f"1–{len(ROWS)}", style="brand")
    footer.append(f" {message('tui.menu.select')}   ", style="dash.muted")
    footer.append("Esc", style="brand")
    footer.append(f" {message('tui.menu.back')}", style="dash.muted")
    rows = (
        (str(index), _label(key), _display(Config.get(key)), f"{category} · {description}", "dash.text")
        for index, (category, key, description) in enumerate(ROWS, 1)
    )
    console.clear()
    console.print(
        menu_layout(
            message("tui.settings.title"),
            message("tui.settings.subtitle"),
            rows,
            footer,
        )
    )
    if restart_required:
        console.print(message("tui.settings.restart_required", scope=restart_required))
    return ask_with_escape(console, message("tui.settings.select"), default="esc")


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
        selection = _settings_menu(console, restart_required)
        if selection in {"esc", "q", "back"}:
            return restart_required
        try:
            index = int(selection) - 1
            if not 0 <= index < len(ROWS):
                continue
            _, key, _ = ROWS[index]
        except (ValueError, IndexError):
            continue
        current = Config.get(key)
        choices = CHOICES.get(key)
        raw = Prompt.ask(
            message("tui.settings.new", setting=_label(key)),
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
