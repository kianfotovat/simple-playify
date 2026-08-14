"""Playify's TUI-only launcher and supervisor."""

from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from src.playify.config import Config
from src.playify.constants import PROJECT_ROOT, display_version
from src.playify.logging_utils import configure_logging

from .bot_process_v2 import BotProcess
from .dashboard_v2 import run_dashboard
from .maintenance import locate_ffmpeg, managed_ffmpeg_due, install_ffmpeg, run_maintenance
from .settings_v2 import run_settings
from .theme import PLAYIFY_THEME
from .wizard_v2 import load_env, run_wizard


def _console() -> Console:
    mode = Config.get("color_mode", "auto")
    return Console(
        theme=PLAYIFY_THEME,
        highlight=False,
        no_color=mode == "none",
        force_terminal=False if mode == "none" else None,
    )


def _has_token() -> bool:
    return bool(load_env(PROJECT_ROOT / ".env").get("DISCORD_TOKEN", "").strip())


def _stop_with_choice(console: Console, bot: BotProcess) -> bool:
    bot.request_stop()
    if bot.wait_for_stop(15):
        return True
    choice = Prompt.ask(
        "The bot did not stop in 15 seconds",
        choices=["force", "wait", "cancel"],
        default="wait",
    )
    if choice == "cancel":
        return False
    if choice == "force":
        bot.force_stop()
        return True
    return _stop_with_choice(console, bot)


def _start_bot(console: Console, bot: BotProcess) -> str:
    bot.start()
    while True:
        state = bot.wait_for_startup(30)
        if state != "timeout":
            return state
        if not Confirm.ask(
            "Discord startup is still pending after 30 seconds. Keep waiting?", default=True
        ):
            _stop_with_choice(console, bot)
            return "stopped"


def _preflight_update(console: Console) -> None:
    if not Config.get("updates_enabled", True):
        return
    try:
        from .updater_v2 import choose_update, inspect_update
    except ImportError:
        return
    status = inspect_update(PROJECT_ROOT)
    action = choose_update(console, status)
    if action == "install":
        console.print("[warning]Accept the pending updater safety confirmation, then use U from the dashboard.[/]")


def main() -> None:
    configure_logging(bot_process=False)
    console = _console()
    console.clear()
    console.print(
        Panel(
            f"[bold cyan]PLAYIFY[/]\n{display_version()}\nTUI-only self-hosted Discord music bot",
            border_style="blue",
        )
    )
    _preflight_update(console)

    ffmpeg, source = locate_ffmpeg()
    if ffmpeg is None:
        console.print("[warning]No functional FFmpeg was found in bin/ or PATH.[/]")
        if not Confirm.ask("Install the managed BtbN build now?", default=True) or not install_ffmpeg(console):
            raise SystemExit(1)
    elif source == "managed" and managed_ffmpeg_due():
        console.print("[info]Managed FFmpeg is due for its optional monthly maintenance check (M).[/]")

    if not _has_token() and not run_wizard(console, PROJECT_ROOT):
        raise SystemExit(2)
    if not _has_token():
        raise SystemExit(2)

    python = Path(sys.executable).resolve()
    expected = (PROJECT_ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")).resolve()
    if python != expected:
        console.print("[error]The TUI must run with Playify's exact .venv interpreter.[/]")
        raise SystemExit(1)

    bot = BotProcess(PROJECT_ROOT, python)
    startup = _start_bot(console, bot)
    if startup not in {"online", "timeout"}:
        console.print(Panel("\n".join(bot.recent_logs(20)) or "No bot output.", title="Bot failed to start"))
        console.input("Press Enter to continue to the offline dashboard…")

    try:
        while True:
            action = run_dashboard(console, bot)
            if action == "config":
                if run_wizard(console, PROJECT_ROOT):
                    bot.metrics["restart_required"] = "Bot"
            elif action == "settings":
                if run_settings(console):
                    bot.metrics["restart_required"] = "Bot"
            elif action == "maintenance":
                bot_restart, launcher_restart = run_maintenance(console)
                if launcher_restart:
                    bot.metrics["restart_required"] = "Launcher"
                elif bot_restart:
                    bot.metrics["restart_required"] = "Bot"
                console.input("Press Enter or Esc to return…")
            elif action == "update":
                try:
                    from .updater_v2 import choose_update, inspect_update, install_update
                except ImportError:
                    console.print("[warning]The updater install path is awaiting explicit safety approval.[/]")
                    console.input("Press Enter or Esc to return…")
                    continue
                status = inspect_update(PROJECT_ROOT, manual=True)
                if choose_update(console, status) == "install":
                    if not _stop_with_choice(console, bot):
                        continue
                    success, detail = install_update(PROJECT_ROOT, status)
                    console.print(
                        f"[{'success' if success else 'error'}]{'Updated to' if success else 'Update failed:'} {detail}[/]"
                    )
                    console.input("Press any key or Esc to restart the launcher…")
                    raise SystemExit(0 if success else 1)
            elif action == "restart":
                if not Confirm.ask("Restart the bot now?", default=False):
                    continue
                if not _stop_with_choice(console, bot):
                    continue
                bot.metrics.pop("restart_required", None)
                startup = _start_bot(console, bot)
                if startup != "online":
                    console.print("[error]The bot did not come online. It will remain stopped.[/]")
                    console.input("Press Enter or Esc to return…")
            elif action == "quit":
                if not Confirm.ask("Quit Playify?", default=False):
                    continue
                if _stop_with_choice(console, bot):
                    return
    except KeyboardInterrupt:
        _stop_with_choice(console, bot)
