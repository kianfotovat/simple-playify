"""Responsive Rich dashboard and full-log viewer."""

from __future__ import annotations

import os
import re
import shutil
import time

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from src.playify.config import Config
from src.playify.constants import display_version
from src.playify.messages import message

from .bot_process import BotProcess
from .key_input import read_key, terminal_mode
from .theme import BRAND_GRADIENT

LOG_LINE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[,.]\d+)?)\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<source>[^:]+):\s?(?P<body>.*)$",
    re.IGNORECASE,
)


def refresh_rate() -> int:
    value = Config.get("tui_refresh", "auto")
    if value == "auto":
        cores = os.cpu_count() or 1
        return 5 if cores <= 2 else 10 if cores <= 6 else 20
    try:
        return max(1, min(30, int(value)))
    except (TypeError, ValueError):
        return 10


def _metric(label: str, value: str, style: str, *, ascii_symbols: bool) -> Text:
    text = Text()
    text.append("| " if ascii_symbols else "▌ ", style=style)
    text.append(label, style="dash.muted")
    text.append("\n  ")
    text.append(value, style=style)
    return text


def _metrics(bot: BotProcess, *, ascii_symbols: bool) -> Panel:
    memory, ffmpeg = bot.process_metrics()
    values = [
        (message("tui.dashboard.metric.uptime"), bot.uptime, "dash.cyan"),
        (message("tui.dashboard.metric.memory"), f"{memory:.1f} MB", "dash.purple"),
        (message("tui.dashboard.metric.servers"), str(bot.metrics.get("servers", 0)), "dash.pink"),
        (
            message("tui.dashboard.metric.players"),
            str(bot.metrics.get("players", 0)),
            "dash.green" if bot.metrics.get("players", 0) else "dash.muted",
        ),
        (
            message("tui.dashboard.metric.queued"),
            str(bot.metrics.get("queued", 0)),
            "dash.orange" if bot.metrics.get("queued", 0) else "dash.muted",
        ),
        (message("tui.dashboard.metric.ffmpeg"), str(ffmpeg), "dash.yellow" if ffmpeg else "dash.muted"),
        (message("tui.dashboard.metric.cache"), str(bot.metrics.get("cache", 0)), "dash.cyan"),
        (
            message("tui.dashboard.metric.crashes"),
            str(bot.crash_count),
            "dash.red" if bot.crash_count else "dash.muted",
        ),
    ]
    grid = Table.grid(expand=True, padding=(0, 2))
    for _ in range(4):
        grid.add_column(ratio=1)
    metrics = [_metric(label, value, style, ascii_symbols=ascii_symbols) for label, value, style in values]
    grid.add_row(*metrics[:4])
    grid.add_row(*metrics[4:])
    return Panel(
        grid,
        title=Text(f" {message('tui.dashboard.runtime')} ", style="brand"),
        border_style="dash.border",
        padding=(0, 1),
    )


def _now_playing(bot: BotProcess) -> Panel:
    player = bot.now_playing()
    if not player:
        content = Text(message("tui.dashboard.no_track"), style="dash.muted")
    else:
        active = bool(player.get("active"))
        state = message("tui.dashboard.state.active" if active else "tui.dashboard.state.dormant")
        separator = " | " if Config.get("symbol_mode", "auto") == "ascii" else " • "
        content = Text()
        content.append(str(player.get("track") or message("tui.dashboard.unknown_track")), style="dash.value")
        content.append("  ")
        content.append(state.capitalize(), style="dash.green" if active else "dash.yellow")
        content.append("\n")
        content.append(message("tui.dashboard.detail.guild") + " ", style="dash.muted")
        content.append(str(player.get("guild_id")), style="brand")
        content.append(separator, style="dash.muted")
        content.append(str(player.get("queued", 0)), style="brand")
        content.append(" " + message("tui.dashboard.detail.queued"), style="dash.muted")
        content.append(separator, style="dash.muted")
        content.append(str(player.get("pending", 0)), style="brand")
        content.append(" " + message("tui.dashboard.detail.pending"), style="dash.muted")
    return Panel(
        content,
        title=Text(f" {message('controller.title.playing')} ", style="brand"),
        border_style="dash.border",
        padding=(0, 1),
    )


def _log_text(lines: list[str]) -> Text:
    text = Text()
    for index, line in enumerate(lines):
        match = LOG_LINE.match(line)
        if match:
            level = match.group("level").upper()
            level_style = {
                "DEBUG": "dash.log.debug",
                "INFO": "dash.log.info",
                "WARNING": "dash.log.warning",
                "ERROR": "dash.log.error",
                "CRITICAL": "dash.log.error",
            }[level]
            text.append(match.group("timestamp"), style="dash.muted")
            text.append("  ")
            text.append(f"{level:<8}", style=level_style)
            text.append(match.group("source"), style="brand")
            text.append("  ", style="dash.muted")
            text.append(match.group("body"), style="dash.text")
        else:
            text.append(line, style="dash.text")
        if index < len(lines) - 1:
            text.append("\n")
    return text


def _logs(bot: BotProcess, height: int) -> Panel:
    lines = bot.recent_logs(max(3, height))
    return Panel(
        _log_text(lines),
        title=Text(f" {message('tui.dashboard.logs.recent')} ", style="brand"),
        border_style="dash.border",
        height=height + 2,
        padding=(0, 1),
    )


def _brand(color_system: str | None) -> Text:
    if color_system != "truecolor":
        return Text("Playify", style="brand")

    brand = Text()
    for character, color in zip("Playify", BRAND_GRADIENT, strict=True):
        brand.append(character, style=Style(color=color, bold=True))
    return brand


def _header(bot: BotProcess, separator: str, color_system: str | None) -> Panel:
    brand = _brand(color_system)
    brand.append(f"  {display_version()}", style="dash.muted")

    status = Text()
    if bot.is_online:
        status.append(message("tui.dashboard.status.online"), style="dash.status.online")
    elif bot.is_running:
        status.append(message("tui.dashboard.status.starting"), style="dash.status.starting")
    else:
        status.append(message("tui.dashboard.status.offline"), style="dash.status.offline")
    if restart := bot.metrics.get("restart_required"):
        status.append(separator, style="dash.muted")
        status.append(message("tui.dashboard.restart_badge", scope=restart), style="dash.yellow")

    row = Table.grid(expand=True)
    row.add_column()
    row.add_column(justify="right")
    row.add_row(brand, status)
    return Panel(row, border_style="dash.border", padding=(0, 1))


def _hotkeys() -> Panel:
    text = Text()
    bindings = [
        ("L", "Logs"),
        ("C", "Config"),
        ("S", "Settings"),
        ("U", "Update"),
        ("M", "Maintenance"),
        ("R", "Restart"),
        ("Q", "Quit"),
    ]
    for index, (key, label) in enumerate(bindings):
        if index:
            text.append("   ")
        text.append(key, style="brand")
        text.append(f" {label}", style="dash.muted")
    return Panel(Align.center(text), border_style="dash.border", padding=(0, 1))


def _dashboard(bot: BotProcess, width: int, height: int, *, color_system: str | None = None):
    ascii_symbols = Config.get("symbol_mode", "auto") == "ascii"
    separator = " | " if ascii_symbols else " • "
    size = f"{width}x{height}" if ascii_symbols else f"{width}×{height}"
    if width < 100 or height < 30:
        return Panel(
            Text(
                message(
                    "tui.dashboard.resize",
                    minimum="100x30" if ascii_symbols else "100×30",
                    size=size,
                ),
                style="warning",
            ),
            title=Text("Playify", style="title"),
            border_style="yellow",
        )
    log_height = max(4, height - 18 - (3 if not bot.is_running else 0))
    items = [
        _header(bot, separator, color_system),
        _metrics(bot, ascii_symbols=ascii_symbols),
        _now_playing(bot),
        _logs(bot, log_height),
    ]
    if not bot.is_running:
        items.append(
            Panel(
                Text(message("tui.dashboard.bot_exited", code=bot.last_exit_code), style="error"),
                title=Text(message("tui.dashboard.bot_offline"), style="error"),
                border_style="red",
            )
        )
    items.append(_hotkeys())
    return Group(*items)


def _full_logs(console: Console, bot: BotProcess) -> None:
    offset = 0
    following = True
    rate = refresh_rate()
    with Live(console=console, screen=True, auto_refresh=False) as live:
        while True:
            _width, height = shutil.get_terminal_size((120, 40))
            lines = list(bot.logs)
            page = max(1, height - 5)
            if following:
                offset = max(0, len(lines) - page)
            visible = lines[offset : offset + page]
            text = _log_text(visible)
            text.overflow = "fold"
            text.no_wrap = False
            footer = message(
                "tui.dashboard.logs.footer",
                state=message("tui.dashboard.logs.following" if following else "tui.dashboard.logs.paused"),
                start=offset + 1,
                end=min(len(lines), offset + page),
                total=len(lines),
            )
            live.update(
                Panel(
                    text,
                    title=Text(f" {message('tui.dashboard.logs.full')} ", style="brand"),
                    subtitle=footer,
                    border_style="dash.border",
                ),
                refresh=True,
            )
            key = read_key()
            if key in {"l", "esc"}:
                return
            if key == "up":
                following = False
                offset = max(0, offset - 1)
            elif key == "down":
                offset = min(max(0, len(lines) - page), offset + 1)
                following = offset >= max(0, len(lines) - page)
            elif key == "pageup":
                following = False
                offset = max(0, offset - page)
            elif key == "pagedown":
                offset = min(max(0, len(lines) - page), offset + page)
                following = offset >= max(0, len(lines) - page)
            elif key == "home":
                following = False
                offset = 0
            elif key == "end":
                following = True
            time.sleep(1 / rate)


def run_dashboard(console: Console, bot: BotProcess) -> str:
    rate = refresh_rate()
    with terminal_mode(), Live(console=console, screen=True, auto_refresh=False) as live:
        while True:
            width, height = shutil.get_terminal_size((120, 40))
            live.update(_dashboard(bot, width, height, color_system=console.color_system), refresh=True)
            key = read_key()
            if key == "l":
                live.stop()
                _full_logs(console, bot)
                live.start(refresh=True)
            elif key in {"c", "s", "u", "m", "r", "q"}:
                return {
                    "c": "config",
                    "s": "settings",
                    "u": "update",
                    "m": "maintenance",
                    "r": "restart",
                    "q": "quit",
                }[key]
            # Esc intentionally does nothing at the root dashboard.
            time.sleep(1 / rate)
