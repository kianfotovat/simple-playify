"""Shared Windows/Linux launcher for Playify's managed environment."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.playify.messages import message

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
DATA = ROOT / "data"
INSTALLATION = DATA / "installation.json"
REQUIREMENTS = ROOT / "requirements.txt"
SUPPORTED = {(3, 12), (3, 13), (3, 14)}
IMPORT_CHECK = (
    "import aiohttp, aiosqlite, cachetools, discord, dotenv, psutil, rich, spotipy, spotify_scraper, yt_dlp; "
    "import nacl, davey"
)


def fail(detail: str) -> "NoReturn":
    print(message("bootstrap.output", detail=detail), file=sys.stderr)
    raise SystemExit(1)


def supported_host() -> bool:
    return sys.platform.startswith(("win32", "linux")) and platform.machine().lower() in {
        "amd64",
        "x86_64",
    }


def venv_python(path: Path = VENV) -> Path:
    return path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def load_metadata() -> dict:
    try:
        value = json.loads(INSTALLATION.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_metadata(value: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    temporary = INSTALLATION.with_name(f".{INSTALLATION.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, INSTALLATION)


def requirements_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def environment_valid(path: Path = VENV) -> bool:
    python = venv_python(path)
    if not python.is_file():
        return False
    version = subprocess.run(
        [
            str(python),
            "-c",
            "import sys; raise SystemExit(0 if sys.version_info[:2] in {(3,12),(3,13),(3,14)} else 1)",
        ],
        cwd=ROOT,
        capture_output=True,
        timeout=20,
    )
    if version.returncode != 0:
        return False
    return subprocess.run(
        [str(python), "-c", IMPORT_CHECK],
        cwd=ROOT,
        capture_output=True,
        timeout=30,
    ).returncode == 0


def dependency_state() -> tuple[bool, bool, dict]:
    metadata = load_metadata()
    mandatory = not environment_valid() or metadata.get("requirements_hash") != requirements_hash()
    last = metadata.get("last_dependency_check")
    stale = True
    if last:
        try:
            stale = datetime.now(UTC) - datetime.fromisoformat(last) >= timedelta(days=30)
        except ValueError:
            pass
    snooze = metadata.get("dependency_snooze_until")
    if snooze:
        try:
            stale = stale and datetime.now(UTC) >= datetime.fromisoformat(snooze)
        except ValueError:
            pass
    return mandatory, stale, metadata


def _safe_cleanup(path: Path) -> None:
    resolved = path.resolve()
    if resolved.parent != ROOT or not resolved.name.startswith(".venv-"):
        fail(message("bootstrap.cleanup_refused"))
    shutil.rmtree(resolved)


def stage_environment(metadata: dict) -> None:
    candidate = ROOT / f".venv-candidate-{os.getpid()}"
    backup = ROOT / f".venv-previous-{os.getpid()}"
    for path in (candidate, backup):
        if path.exists():
            _safe_cleanup(path)
    print(
        message(
            "bootstrap.output", detail=message("bootstrap.building")
        )
    )
    try:
        subprocess.run([sys.executable, "-m", "venv", str(candidate)], check=True, cwd=ROOT)
        python = venv_python(candidate)
        subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)],
            check=True,
            cwd=ROOT,
        )
        if not environment_valid(candidate):
            fail(message("bootstrap.stage_failed"))
        if VENV.exists():
            VENV.replace(backup)
        candidate.replace(VENV)
        if backup.exists():
            _safe_cleanup(backup)
    except BaseException:
        if not VENV.exists() and backup.exists():
            backup.replace(VENV)
        if candidate.exists():
            _safe_cleanup(candidate)
        raise
    metadata.update(
        {
            "requirements_hash": requirements_hash(),
            "last_dependency_check": datetime.now(UTC).isoformat(),
            "dependency_snooze_until": None,
            "python_version": ".".join(map(str, sys.version_info[:3])),
            "pending_environment": None,
        }
    )
    save_metadata(metadata)


def stage_pending_environment() -> Path:
    """Build a sibling environment while the TUI keeps the active bot running."""

    pending = ROOT / ".venv-pending"
    if pending.exists():
        _safe_cleanup(pending)
    subprocess.run([sys.executable, "-m", "venv", str(pending)], check=True, cwd=ROOT)
    python = venv_python(pending)
    try:
        subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)],
            check=True,
            cwd=ROOT,
        )
        if not environment_valid(pending):
            fail(message("bootstrap.stage_failed"))
    except BaseException:
        if pending.exists():
            _safe_cleanup(pending)
        raise
    metadata = load_metadata()
    metadata["pending_environment"] = pending.name
    metadata["pending_requirements_hash"] = requirements_hash()
    save_metadata(metadata)
    return pending


def promote_pending(metadata: dict) -> dict:
    name = metadata.get("pending_environment")
    if not isinstance(name, str) or not name.startswith(".venv-pending"):
        return metadata
    pending = (ROOT / name).resolve()
    if pending.parent != ROOT or not environment_valid(pending):
        metadata["pending_environment"] = None
        save_metadata(metadata)
        return metadata
    backup = ROOT / f".venv-previous-{os.getpid()}"
    if backup.exists():
        _safe_cleanup(backup)
    if VENV.exists():
        VENV.replace(backup)
    try:
        pending.replace(VENV)
    except BaseException:
        if not VENV.exists() and backup.exists():
            backup.replace(VENV)
        raise
    if backup.exists():
        _safe_cleanup(backup)
    metadata.update(
        {
            "requirements_hash": metadata.pop("pending_requirements_hash", requirements_hash()),
            "last_dependency_check": datetime.now(UTC).isoformat(),
            "pending_environment": None,
        }
    )
    save_metadata(metadata)
    return metadata


def main() -> None:
    if not supported_host():
        fail(message("bootstrap.host_unsupported"))
    if sys.version_info[:2] not in SUPPORTED:
        fail(message("bootstrap.python_unsupported"))
    if not REQUIREMENTS.is_file():
        fail(message("bootstrap.requirements_missing"))
    metadata = promote_pending(load_metadata())
    mandatory, stale, metadata = dependency_state()
    if mandatory:
        stage_environment(metadata)
    elif stale:
        answer = input(
            message(
                "bootstrap.output", detail=message("bootstrap.dependencies_due")
            )
        ).strip().lower()
        if answer in {"", "y", "yes"}:
            stage_environment(metadata)
        else:
            metadata["dependency_snooze_until"] = (datetime.now(UTC) + timedelta(days=3)).isoformat()
            save_metadata(metadata)
    python = venv_python()
    if not environment_valid():
        fail(message("bootstrap.venv_unavailable"))
    raise SystemExit(subprocess.call([str(python), "-m", "src.tui"], cwd=ROOT))


if __name__ == "__main__":
    main()
