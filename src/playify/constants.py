"""Project-wide constants and portable runtime paths."""

from __future__ import annotations

import subprocess
from pathlib import Path

NAME = "Playify"
VERSION = "2.1.0"
FORK_REPOSITORY = "kianfotovat/simple-playify"
FORK_URL = f"https://github.com/{FORK_REPOSITORY}"
ISSUES_URL = f"{FORK_URL}/issues"
UPSTREAM_REPOSITORY = "alan7383/playify"
HTTP_USER_AGENT = f"{NAME}/{'.'.join(VERSION.split('.')[:2])} (+{FORK_URL})"
CHROME_STABLE_VERSION_URL = "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_STABLE"
DEFAULT_CHROME_MAJOR = 152
LAUNCH_STAGE_ENV = "PLAYIFY_LAUNCH_STAGE"
RUNTIME_PREFIX_ENV = "PLAYIFY_RUNTIME_PREFIX"
TUI_LAUNCH_STAGE = "tui"
BOT_LAUNCH_STAGE = "bot"
TUI_RUNTIME_REFRESH_EXIT = 75

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
BIN_DIR = PROJECT_ROOT / "bin"
LOG_DIR = DATA_DIR / "logs"
COOKIE_DIR = DATA_DIR / "cookies"
BACKUP_DIR = DATA_DIR / "backups"
TEMP_DIR = DATA_DIR / "tmp"
DATABASE_PATH = DATA_DIR / "playify.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
INSTALLATION_PATH = DATA_DIR / "installation.json"


def ensure_runtime_dirs() -> None:
    """Create only the portable directories Playify owns."""

    for path in (DATA_DIR, BIN_DIR, LOG_DIR, COOKIE_DIR, BACKUP_DIR, TEMP_DIR):
        path.mkdir(parents=True, exist_ok=True)


def git_revision(*, short: bool = False) -> str:
    """Return the current Git revision without making Git a runtime requirement."""

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        revision = result.stdout.strip()
        if len(revision) != 40:
            return "unknown"
        return revision[:7] if short else revision
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return "unknown"


def display_version() -> str:
    return f"{VERSION} ({git_revision(short=True)})"
