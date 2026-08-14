"""Managed FFmpeg and staged environment maintenance."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt

from src.playify.config import Installation
from src.playify.constants import BIN_DIR

API = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest"
USER_AGENT = "Playify/2.1 (+https://github.com/kianfotovat/simple-playify)"


def ffmpeg_name() -> str:
    return "ffmpeg.exe" if os.name == "nt" else "ffmpeg"


def functional_ffmpeg(path: str | Path) -> bool:
    try:
        result = subprocess.run(
            [str(path), "-version"], capture_output=True, timeout=10, text=True
        )
        return result.returncode == 0 and "ffmpeg version" in result.stdout.lower()
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False


def locate_ffmpeg() -> tuple[str | None, str]:
    local = BIN_DIR / ffmpeg_name()
    if local.is_file() and functional_ffmpeg(local):
        return str(local), "managed"
    system = shutil.which("ffmpeg")
    if system and functional_ffmpeg(system):
        return system, "path"
    return None, "missing"


def managed_ffmpeg_due() -> bool:
    _, source = locate_ffmpeg()
    if source != "managed":
        return False
    raw = Installation.get("last_ffmpeg_check")
    if not raw:
        return True
    try:
        return datetime.now(UTC) - datetime.fromisoformat(raw) >= timedelta(days=30)
    except ValueError:
        return True


def _request(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def _release_asset() -> tuple[str, str, str]:
    release = json.loads(_request(API))
    suffix = "win64-gpl.zip" if os.name == "nt" else "linux64-gpl.tar.xz"
    assets = release.get("assets", [])
    archive = next(
        (asset for asset in assets if str(asset.get("name", "")).endswith(suffix)), None
    )
    if archive is None:
        raise RuntimeError("BtbN did not publish the expected x64 GPL archive")
    digest = str(archive.get("digest") or "")
    if digest.startswith("sha256:"):
        expected = digest.split(":", 1)[1]
    else:
        checksum = next(
            (
                asset
                for asset in assets
                if asset.get("name") in {archive["name"] + ".sha256", "checksums.sha256"}
            ),
            None,
        )
        if checksum is None:
            raise RuntimeError("BtbN release has no SHA-256 digest")
        text = _request(checksum["browser_download_url"]).decode("utf-8", errors="replace")
        line = next((line for line in text.splitlines() if archive["name"] in line), "")
        expected = line.split()[0] if line else text.strip().split()[0]
    return archive["browser_download_url"], archive["name"], expected.lower()


def _extract_binary(archive: Path, destination: Path) -> None:
    wanted = "/bin/" + ffmpeg_name()
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as package:
            member = next((name for name in package.namelist() if name.endswith(wanted)), None)
            if not member or Path(member).is_absolute() or ".." in Path(member).parts:
                raise RuntimeError("FFmpeg archive has no safe executable member")
            with package.open(member) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
    else:
        with tarfile.open(archive, "r:xz") as package:
            member = next((item for item in package.getmembers() if item.name.endswith(wanted) and item.isfile()), None)
            if not member or Path(member.name).is_absolute() or ".." in Path(member.name).parts:
                raise RuntimeError("FFmpeg archive has no safe executable member")
            source = package.extractfile(member)
            if source is None:
                raise RuntimeError("FFmpeg executable could not be extracted")
            with source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)


def install_ffmpeg(console: Console) -> bool:
    console.print("[info]Downloading the latest BtbN x64 GPL FFmpeg build…[/]")
    try:
        url, name, expected = _release_asset()
        with tempfile.TemporaryDirectory(prefix="playify-ffmpeg-") as folder:
            archive = Path(folder) / name
            archive.write_bytes(_request(url))
            actual = hashlib.sha256(archive.read_bytes()).hexdigest()
            if actual != expected:
                raise RuntimeError("FFmpeg archive SHA-256 did not match")
            candidate = Path(folder) / ffmpeg_name()
            _extract_binary(archive, candidate)
            if os.name != "nt":
                candidate.chmod(0o755)
            if not functional_ffmpeg(candidate):
                raise RuntimeError("the extracted FFmpeg binary failed its version check")
            BIN_DIR.mkdir(parents=True, exist_ok=True)
            target = BIN_DIR / ffmpeg_name()
            os.replace(candidate, target)
            if os.name != "nt":
                target.chmod(0o755)
        Installation.set("last_ffmpeg_check", datetime.now(UTC).isoformat())
        console.print("[success]Managed FFmpeg installed and validated.[/]")
        return True
    except Exception as exc:
        console.print(f"[error]FFmpeg installation failed: {exc}[/]")
        return False


def run_maintenance(console: Console) -> tuple[bool, bool]:
    """Return (bot_restart, full_launcher_restart)."""

    path, source = locate_ffmpeg()
    console.print(f"\n[title]Maintenance[/]\nFFmpeg: {source} ({path or 'not found'})")
    console.print("[1] Stage dependencies/interpreter  [2] Install/update managed FFmpeg  [3] Run all  [Esc] Back")
    choice = Prompt.ask("Choice", choices=["1", "2", "3", "esc"], default="esc").lower()
    bot_restart = False
    launcher_restart = False
    if choice in {"1", "3"}:
        try:
            from bootstrap import stage_pending_environment

            stage_pending_environment()
            console.print("[success]A validated environment is staged for the next launcher start.[/]")
            launcher_restart = True
        except Exception as exc:
            console.print(f"[error]Environment staging failed; the active environment is unchanged: {exc}[/]")
    if choice in {"2", "3"}:
        bot_restart = install_ffmpeg(console)
    return bot_restart, launcher_restart
