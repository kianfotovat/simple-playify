"""Responsive Rich dashboard and full-log viewer."""

from __future__ import annotations

import os
import shutil
import time

from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from src.playify.config import Config
from src.playify.constants import display_version
from src.playify.messages import message

from .bot_process import BotProcess
from .key_input import read_key, terminal_mode


def refresh_rate() -> int:
    value = Config.get("tui_refresh", "auto")
    if value == "auto":
        cores = os.cpu_count() or 1
        return 5 if cores <= 2 else 10 if cores <= 6 else 20
    try:
        return max(1, min(30, int(value)))
    except (TypeError, ValueError):
        return 10


def _metrics(bot: BotProcess) -> Panel:
    memory, ffmpeg = bot.process_metrics()
    values = [
        (message("tui.dashboard.metric.uptime"), bot.uptime),
        (message("tui.dashboard.metric.memory"), f"{memory:.1f} MB"),
        (message("tui.dashboard.metric.servers"), str(bot.metrics.get("servers", 0))),
        (message("tui.dashboard.metric.players"), str(bot.metrics.get("players", 0))),
        (message("tui.dashboard.metric.queued"), str(bot.metrics.get("queued", 0))),
        (message("tui.dashboard.metric.ffmpeg"), str(ffmpeg)),
        (message("tui.dashboard.metric.cache"), str(bot.metrics.get("cache", 0))),
        (message("tui.dashboard.metric.crashes"), str(bot.crash_count)),
    ]
    cards = [Panel(f"[bold]{value}[/]", title=label, width=15) for label, value in values]
    return Panel(
        Columns(cards, equal=True, expand=True),
        title=message("tui.dashboard.runtime"),
        border_style="blue",
    )


def _now_playing(bot: BotProcess) -> Panel:
    player = bot.now_playing()
    if not player:
        content = message("tui.dashboard.no_track")
    else:
        state = message("tui.dashboard.state.active" if player.get("active") else "tui.dashboard.state.dormant")
        content = message(
            "tui.dashboard.track",
            track=player.get("track") or message("tui.dashboard.unknown_track"),
            state=state,
            guild=player.get("guild_id"),
            queued=player.get("queued", 0),
            pending=player.get("pending", 0),
        )
    return Panel(content, title=message("controller.title.playing"), border_style="cyan")


def _logs(bot: BotProcess, height: int) -> Panel:
    lines = bot.recent_logs(max(3, height))
    text = Text()
    for line in lines:
        style = "red" if "ERROR" in line or "CRITICAL" in line else "yellow" if "WARNING" in line else "white"
        text.append(line, style=style)
        text.append("\n")
    return Panel(
        text,
        title=message("tui.dashboard.logs.recent"),
        border_style="bright_black",
        height=height + 2,
    )


def _dashboard(bot: BotProcess, width: int, height: int):
    ascii_symbols = Config.get("symbol_mode", "auto") == "ascii"
    separator = " | " if ascii_symbols else " • "
    size = f"{width}x{height}" if ascii_symbols else f"{width}×{height}"
    if width < 100 or height < 30:
        return Panel(
            message(
                "tui.dashboard.resize",
                minimum="100x30" if ascii_symbols else "100×30",
                size=size,
            ),
            title="Playify",
            border_style="yellow",
        )
    status = message(
        "tui.dashboard.status.online"
        if bot.is_online
        else "tui.dashboard.status.starting"
        if bot.is_running
        else "tui.dashboard.status.offline"
    )
    restart = bot.metrics.get("restart_required")
    badge = separator + message("tui.dashboard.restart_badge", scope=restart) if restart else ""
    header = Panel(
        message(
            "tui.dashboard.header",
            version=display_version(),
            separator=separator,
            status=status,
            badge=badge,
        ),
        border_style="blue",
    )
    hotkeys = Panel(
        message("tui.dashboard.hotkeys"),
        border_style="bright_black",
    )
    log_height = max(4, height - 20)
    items = [header, _metrics(bot), _now_playing(bot), _logs(bot, log_height)]
    if not bot.is_running:
        items.append(
            Panel(
                message("tui.dashboard.bot_exited", code=bot.last_exit_code),
                title=message("tui.dashboard.bot_offline"),
                border_style="red",
            )
        )
    items.append(hotkeys)
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
            text = Text("\n".join(visible), overflow="fold", no_wrap=False)
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
                    title=message("tui.dashboard.logs.full"),
                    subtitle=footer,
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
            live.update(_dashboard(bot, width, height), refresh=True)
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
