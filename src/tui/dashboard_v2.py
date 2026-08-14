"""Responsive Rich dashboard and full-log viewer."""

from __future__ import annotations

import os
import shutil
import time

from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.playify.config import Config
from src.playify.constants import display_version

from .bot_process_v2 import BotProcess
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
        ("Uptime", bot.uptime),
        ("Memory", f"{memory:.1f} MB"),
        ("Servers", str(bot.metrics.get("servers", 0))),
        ("Players", str(bot.metrics.get("players", 0))),
        ("Queued", str(bot.metrics.get("queued", 0))),
        ("FFmpeg", str(ffmpeg)),
        ("Cache", str(bot.metrics.get("cache", 0))),
        ("Crashes", str(bot.crash_count)),
    ]
    cards = [Panel(f"[bold]{value}[/]", title=label, width=15) for label, value in values]
    return Panel(Columns(cards, equal=True, expand=True), title="Runtime", border_style="blue")


def _now_playing(bot: BotProcess) -> Panel:
    player = bot.now_playing()
    if not player:
        content = "No active or dormant track."
    else:
        state = "active" if player.get("active") else "dormant"
        content = (
            f"[bold]{player.get('track') or 'Unknown track'}[/]  [{state}]\n"
            f"Guild {player.get('guild_id')} • {player.get('queued', 0)} queued • "
            f"{player.get('pending', 0)} pending"
        )
    return Panel(content, title="Now Playing", border_style="cyan")


def _logs(bot: BotProcess, height: int) -> Panel:
    lines = bot.recent_logs(max(3, height))
    text = Text()
    for line in lines:
        style = "red" if "ERROR" in line or "CRITICAL" in line else "yellow" if "WARNING" in line else "white"
        text.append(line, style=style)
        text.append("\n")
    return Panel(text, title="Recent logs", border_style="bright_black", height=height + 2)


def _dashboard(bot: BotProcess, width: int, height: int):
    if width < 100 or height < 30:
        return Panel(
            f"Resize the terminal to at least 100×30. Current size: {width}×{height}",
            title="Playify",
            border_style="yellow",
        )
    status = "ONLINE" if bot.is_online else "STARTING" if bot.is_running else "OFFLINE"
    restart = bot.metrics.get("restart_required")
    badge = f"  •  [yellow]{restart} restart required[/]" if restart else ""
    header = Panel(
        f"[bold cyan]Playify {display_version()}[/]  •  {status}{badge}",
        border_style="blue",
    )
    hotkeys = Panel(
        "[bold]L[/] Logs  [bold]C[/] Config  [bold]S[/] Settings  "
        "[bold]U[/] Update  [bold]M[/] Maintenance  [bold]R[/] Restart  [bold]Q[/] Quit",
        border_style="bright_black",
    )
    log_height = max(4, height - 20)
    items = [header, _metrics(bot), _now_playing(bot), _logs(bot, log_height)]
    if not bot.is_running:
        items.append(
            Panel(
                f"Bot exited with code {bot.last_exit_code}. Press R to restart; Playify will not loop automatically.",
                title="Bot offline",
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
            width, height = shutil.get_terminal_size((120, 40))
            lines = list(bot.logs)
            page = max(1, height - 5)
            if following:
                offset = max(0, len(lines) - page)
            visible = lines[offset : offset + page]
            text = Text("\n".join(visible), overflow="fold", no_wrap=False)
            footer = (
                f"↑/↓ scroll • PgUp/PgDn • Home/End • L/Esc back • "
                f"{'following' if following else 'paused'} • {offset + 1}-{min(len(lines), offset + page)}/{len(lines)}"
            )
            live.update(Panel(text, title="Full logs", subtitle=footer), refresh=True)
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
