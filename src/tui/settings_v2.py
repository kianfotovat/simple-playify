"""Persistent categorized runtime settings menu."""

from __future__ import annotations

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from src.playify.config import Config

CHOICES = {
    "persistence_mode": ["full", "settings"],
    "updates_enabled": ["true", "false"],
    "soundcloud_fallback": ["true", "false"],
    "ip_mode": ["auto", "ipv4"],
    "color_mode": ["auto", "v2", "ansi", "none"],
    "symbol_mode": ["auto", "unicode", "ascii"],
    "bot_status_type": ["none", "playing", "listening", "watching", "competing"],
}

ROWS = (
    ("Playback", "persistence_mode", "Full player persistence or server settings only"),
    ("Playback", "tidal_country", "Two-letter Tidal country code"),
    ("Playback", "soundcloud_fallback", "Use SoundCloud recommendation fallback"),
    ("Network", "private_media_allowlist", "Private host/IP/CIDR allowlist"),
    ("Network", "ip_mode", "Auto networking or force IPv4"),
    ("Network", "youtube_clients", "yt-dlp YouTube client names"),
    ("Performance", "worker_count", "Auto or 1-8 metadata workers"),
    ("Performance", "http_concurrency", "Auto or 1-16 HTTP requests"),
    ("Interface", "tui_refresh", "Auto or refresh rate in Hz"),
    ("Interface", "color_mode", "Auto, V2, ANSI, or no colors"),
    ("Interface", "symbol_mode", "Auto, Unicode, or ASCII symbols"),
    ("Interface", "controller_idle_image", "Idle controller image URL or none"),
    ("Presence", "bot_status_type", "Optional Discord presence type"),
    ("Presence", "bot_status_text", "Optional Discord presence text"),
    ("Updates", "updates_enabled", "Check the canonical fork before launch"),
)


def _display(value) -> str:
    if isinstance(value, list):
        return ", ".join(value) or "(empty)"
    return str(value)


def _convert(key: str, raw: str):
    if key in {"updates_enabled", "soundcloud_fallback"}:
        return raw == "true"
    if key in {"private_media_allowlist", "youtube_clients"}:
        return [value.strip() for value in raw.split(",") if value.strip()]
    if key == "tidal_country":
        value = raw.strip().upper()
        if len(value) != 2 or not value.isalpha():
            raise ValueError("use a two-letter country code")
        return value
    if key == "worker_count":
        return "auto" if raw == "auto" else max(1, min(8, int(raw)))
    if key == "http_concurrency":
        return "auto" if raw == "auto" else max(1, min(16, int(raw)))
    if key == "tui_refresh":
        return "auto" if raw == "auto" else max(1, min(30, int(raw)))
    return raw.strip()


def run_settings(console: Console) -> bool:
    """Return True if any bot setting needs a restart."""

    Config.reload()
    restart_required = False
    while True:
        table = Table(title="Playify settings", show_lines=False)
        table.add_column("#", justify="right")
        table.add_column("Category")
        table.add_column("Setting")
        table.add_column("Current")
        table.add_column("Purpose")
        for index, (category, key, description) in enumerate(ROWS, 1):
            table.add_row(str(index), category, key, _display(Config.get(key)), description)
        console.clear()
        console.print(table)
        if restart_required:
            console.print("[warning]Restart required to apply one or more changes.[/]")
        selection = Prompt.ask("Setting number, or Esc to return", default="esc").lower()
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
            f"New {key}",
            choices=choices,
            default=_display(current) if not isinstance(current, list) else ",".join(current),
        )
        try:
            Config.set(key, _convert(key, raw))
        except (TypeError, ValueError) as exc:
            console.print(f"[error]{exc}[/]")
            console.input("Press Enter…")
            continue
        if key not in {"color_mode", "symbol_mode"}:
            restart_required = True
