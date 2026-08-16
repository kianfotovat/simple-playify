"""Playify's TUI-only launcher and supervisor."""

from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from src.playify.config import Config
from src.playify.constants import (
    PROJECT_ROOT,
    TUI_RUNTIME_REFRESH_EXIT,
    display_version,
)
from src.playify.logging_utils import configure_logging
from src.playify.messages import message

from .bot_process import BotProcess
from .dashboard import run_dashboard
from .key_input import wait_for_key
from .maintenance import install_ffmpeg, locate_ffmpeg, managed_ffmpeg_due, run_maintenance
from .settings import run_settings
from .theme import PLAYIFY_THEME
from .wizard import load_env, run_wizard


def _console() -> Console:
    mode = Config.get("color_mode", "auto")
    color_system = {"ansi": "standard", "v2": "truecolor", "none": None}.get(mode, "auto")
    return Console(
        theme=PLAYIFY_THEME,
        highlight=False,
        no_color=True if mode == "none" else None,
        color_system=color_system,
        safe_box=Config.get("symbol_mode", "auto") == "ascii",
        force_terminal=False if mode == "none" else None,
    )


def _has_token() -> bool:
    return bool(load_env(PROJECT_ROOT / ".env").get("DISCORD_TOKEN", "").strip())


def _stop_with_choice(console: Console, bot: BotProcess) -> bool:
    bot.request_stop()
    if bot.wait_for_stop(15):
        return True
    choice = Prompt.ask(
        message("tui.main.stop_timeout"),
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
        if not Confirm.ask(message("tui.main.start_pending"), default=True):
            _stop_with_choice(console, bot)
            return "stopped"


def _perform_update(console: Console, status, action: str) -> None:
    from .updater import install_update, rollback_update

    operation = rollback_update if action == "rollback" else install_update
    success, detail = operation(PROJECT_ROOT, status)
    result = (
        message(
            "tui.main.update.rolled_back" if action == "rollback" else "tui.main.update.updated",
            revision=detail,
        )
        if success
        else message("tui.main.update.failed", detail=detail)
    )
    console.print(f"[{'success' if success else 'error'}]{result}[/]")
    wait_for_key(console, message("tui.key.restart_launcher"))
    raise SystemExit(0 if success else 1)


def _preflight_update(console: Console) -> None:
    if not Config.get("updates_enabled", True):
        return
    from .updater import choose_update, inspect_update

    status = inspect_update(PROJECT_ROOT)
    action = choose_update(console, status)
    if action != "skip":
        _perform_update(console, status, action)


def main() -> None:
    configure_logging(bot_process=False)
    console = _console()
    console.clear()
    console.print(
        Panel(
            message("tui.main.banner", version=display_version()),
            border_style="blue",
        )
    )
    _preflight_update(console)

    ffmpeg, source = locate_ffmpeg()
    if ffmpeg is None:
        console.print(message("tui.main.ffmpeg_missing"))
        if not Confirm.ask(message("tui.main.ffmpeg_install"), default=True) or not install_ffmpeg(console):
            raise SystemExit(1)
    elif source == "managed" and managed_ffmpeg_due():
        console.print(message("tui.main.ffmpeg_due"))

    if not _has_token() and not run_wizard(console, PROJECT_ROOT):
        raise SystemExit(2)
    if not _has_token():
        raise SystemExit(2)

    python = Path(sys.executable).resolve()
    bot = BotProcess(PROJECT_ROOT, python)
    startup = _start_bot(console, bot)
    if startup not in {"online", "timeout"}:
        console.print(
            Panel(
                "\n".join(bot.recent_logs(20)) or message("tui.main.no_output"),
                title=message("tui.main.start_failed"),
            )
        )
        wait_for_key(console, message("tui.key.continue_offline"))

    try:
        while True:
            action = run_dashboard(console, bot)
            console.clear()
            if action == "config":
                if run_wizard(console, PROJECT_ROOT):
                    bot.metrics["restart_required"] = message("tui.scope.bot")
            elif action == "settings":
                if restart := run_settings(console):
                    bot.metrics["restart_required"] = restart
            elif action == "maintenance":
                bot_restart, launcher_restart = run_maintenance(console)
                if launcher_restart:
                    if not _stop_with_choice(console, bot):
                        continue
                    raise SystemExit(TUI_RUNTIME_REFRESH_EXIT)
                elif bot_restart:
                    bot.metrics["restart_required"] = message("tui.scope.bot")
                wait_for_key(console)
            elif action == "update":
                from .updater import choose_update, inspect_update

                status = inspect_update(PROJECT_ROOT, manual=True)
                update_action = choose_update(console, status)
                if update_action != "skip":
                    if not _stop_with_choice(console, bot):
                        continue
                    _perform_update(console, status, update_action)
            elif action == "restart":
                if not Confirm.ask(message("tui.main.restart"), default=False):
                    continue
                if not _stop_with_choice(console, bot):
                    continue
                bot.metrics.pop("restart_required", None)
                startup = _start_bot(console, bot)
                if startup != "online":
                    console.print(message("tui.main.restart_failed"))
                    wait_for_key(console)
            elif action == "quit":
                if not Confirm.ask(message("tui.main.quit"), default=False):
                    continue
                if _stop_with_choice(console, bot):
                    return
    except KeyboardInterrupt:
        _stop_with_choice(console, bot)
