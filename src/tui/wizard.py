"""Atomic, live-verified configuration wizard."""

from __future__ import annotations

import base64
import json
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import discord
from dotenv import dotenv_values
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.text import Text

from src.playify.constants import HTTP_USER_AGENT
from src.playify.messages import message

from .key_input import ask_with_escape, wait_for_key
from .menu import menu_layout


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {str(key): str(value) for key, value in dotenv_values(path).items() if key is not None and value is not None}


def save_env(path: Path, updates: dict[str, str]) -> None:
    """Replace known assignments while retaining comments and unknown keys."""

    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)
    if remaining:
        if output and output[-1]:
            output.append("")
        output.extend(f"{key}={value}" for key, value in remaining.items())
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(output).rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_request(request: urllib.request.Request, timeout: int = 15) -> dict:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def verify_discord(token: str) -> tuple[str, str | None]:
    request = urllib.request.Request(
        "https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bot {token}", "User-Agent": HTTP_USER_AGENT},
    )
    try:
        data = _json_request(request)
        return "valid", str(data.get("id")) if data.get("id") else None
    except urllib.error.HTTPError as exc:
        return ("invalid", None) if exc.code in {401, 403} else ("network", None)
    except (OSError, ValueError):
        return "network", None


def verify_spotify(client_id: str, client_secret: str) -> str:
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    request = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=body,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": HTTP_USER_AGENT,
        },
    )
    try:
        data = _json_request(request)
        return "valid" if data.get("access_token") else "invalid"
    except urllib.error.HTTPError as exc:
        return "invalid" if exc.code in {400, 401, 403} else "network"
    except (OSError, ValueError):
        return "network"


def invite_url(application_id: str, *, stage_moderation: bool) -> str:
    permissions = discord.Permissions.none()
    permissions.view_channel = True
    permissions.send_messages = True
    permissions.embed_links = True
    permissions.read_message_history = True
    permissions.connect = True
    permissions.speak = True
    permissions.use_application_commands = True
    permissions.mute_members = stage_moderation
    return "https://discord.com/oauth2/authorize?" + urllib.parse.urlencode(
        {
            "client_id": application_id,
            "scope": "bot applications.commands",
            "permissions": permissions.value,
        }
    )


def _status(configured: bool) -> tuple[str, str]:
    key = "configured" if configured else "missing"
    return message(f"tui.wizard.status.{key}"), "success" if configured else "warning"


def _menu(console: Console, current: dict[str, str]) -> str:
    discord_status, discord_style = _status(bool(current.get("DISCORD_TOKEN")))
    spotify_values = (current.get("SPOTIFY_CLIENT_ID"), current.get("SPOTIFY_CLIENT_SECRET"))
    spotify_status, spotify_style = _status(all(spotify_values))
    if any(spotify_values) and not all(spotify_values):
        spotify_status = message("tui.wizard.status.incomplete")
        spotify_style = "error"
    invite_ready = bool(current.get("DISCORD_TOKEN"))
    invite_status = message(f"tui.wizard.status.{'available' if invite_ready else 'unavailable'}")
    footer = Text()
    footer.append("1–3", style="brand")
    footer.append(f" {message('tui.menu.select')}   ", style="dash.muted")
    footer.append("Esc", style="brand")
    footer.append(f" {message('tui.menu.back')}", style="dash.muted")
    rows = (
        (
            "1",
            message("tui.wizard.option.discord"),
            discord_status,
            message("tui.wizard.option.discord_help"),
            discord_style,
        ),
        (
            "2",
            message("tui.wizard.option.spotify"),
            spotify_status,
            message("tui.wizard.option.spotify_help"),
            spotify_style,
        ),
        (
            "3",
            message("tui.wizard.option.invite"),
            invite_status,
            message("tui.wizard.option.invite_help"),
            "info" if invite_ready else "muted",
        ),
    )
    console.clear()
    console.print(
        menu_layout(
            message("tui.wizard.menu_title"),
            message("tui.wizard.notice"),
            rows,
            footer,
        )
    )
    return ask_with_escape(
        console,
        message("tui.wizard.select"),
        choices=["1", "2", "3", "esc"],
        default="esc",
    )


def _configure_discord(console: Console, path: Path, current: dict[str, str]) -> tuple[bool, str | None]:
    token = Prompt.ask(
        message("tui.wizard.discord_token"),
        password=True,
        default="" if not current.get("DISCORD_TOKEN") else "__KEEP__",
        show_default=False,
    )
    if token == "__KEEP__":
        token = current.get("DISCORD_TOKEN", "")
    if not token:
        console.print(message("tui.wizard.discord_required"))
        wait_for_key(console)
        return False, None
    status, application_id = verify_discord(token)
    if status == "invalid":
        console.print(message("tui.wizard.discord_rejected"))
        wait_for_key(console)
        return False, None
    if status == "network" and not Confirm.ask(message("tui.wizard.discord_unverified"), default=False):
        return False, None
    changed = token != current.get("DISCORD_TOKEN", "")
    save_env(path, {"DISCORD_TOKEN": token})
    current["DISCORD_TOKEN"] = token
    console.print(message("tui.wizard.saved"))
    if not application_id:
        wait_for_key(console)
    return changed, application_id


def _configure_spotify(console: Console, path: Path, current: dict[str, str]) -> bool:

    spotify_id = Prompt.ask(
        message("tui.wizard.spotify_id"),
        password=True,
        default="__KEEP__" if current.get("SPOTIFY_CLIENT_ID") else "",
        show_default=False,
    )
    spotify_secret = Prompt.ask(
        message("tui.wizard.spotify_secret"),
        password=True,
        default="__KEEP__" if current.get("SPOTIFY_CLIENT_SECRET") else "",
        show_default=False,
    )
    if spotify_id == "__KEEP__":
        spotify_id = current.get("SPOTIFY_CLIENT_ID", "")
    if spotify_secret == "__KEEP__":
        spotify_secret = current.get("SPOTIFY_CLIENT_SECRET", "")
    if bool(spotify_id) != bool(spotify_secret):
        console.print(message("tui.wizard.spotify_pair"))
        wait_for_key(console)
        return False
    if spotify_id:
        spotify_status = verify_spotify(spotify_id, spotify_secret)
        if spotify_status == "invalid":
            console.print(message("tui.wizard.spotify_rejected"))
            wait_for_key(console)
            return False
        if spotify_status == "network" and not Confirm.ask(message("tui.wizard.spotify_unverified"), default=False):
            return False
    changed = (
        spotify_id != current.get("SPOTIFY_CLIENT_ID", "")
        or spotify_secret != current.get("SPOTIFY_CLIENT_SECRET", "")
    )
    save_env(
        path,
        {
            "SPOTIFY_CLIENT_ID": spotify_id,
            "SPOTIFY_CLIENT_SECRET": spotify_secret,
        },
    )
    current["SPOTIFY_CLIENT_ID"] = spotify_id
    current["SPOTIFY_CLIENT_SECRET"] = spotify_secret
    console.print(message("tui.wizard.saved"))
    wait_for_key(console)
    return changed


def _show_invite(console: Console, application_id: str) -> None:
    stage = Confirm.ask(message("tui.wizard.stage_permission"), default=False)
    url = invite_url(application_id, stage_moderation=stage)
    console.print(message("tui.wizard.invite"))
    console.print(f"[link={url}]{url}[/link]")
    wait_for_key(console, message("tui.wizard.invite_continue"))


def _invite_from_current_token(console: Console, current: dict[str, str]) -> None:
    token = current.get("DISCORD_TOKEN", "")
    if not token:
        console.print(message("tui.wizard.invite_unavailable"))
        wait_for_key(console)
        return
    status, application_id = verify_discord(token)
    if status == "invalid":
        console.print(message("tui.wizard.discord_rejected"))
    elif status == "network" or not application_id:
        console.print(message("tui.wizard.invite_verify_failed"))
    else:
        _show_invite(console, application_id)
        return
    wait_for_key(console)


def run_wizard(console: Console, project_root: Path) -> bool:
    """Run the credential menu and report whether any saved value changed."""

    path = project_root / ".env"
    current = load_env(path)
    changed = False
    while True:
        selection = _menu(console, current)
        if selection == "esc":
            return changed
        if selection == "1":
            discord_changed, application_id = _configure_discord(console, path, current)
            changed = changed or discord_changed
            if application_id:
                _show_invite(console, application_id)
        elif selection == "2":
            changed = _configure_spotify(console, path, current) or changed
        elif selection == "3":
            _invite_from_current_token(console, current)
