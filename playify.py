"""Thin managed-environment entrypoint for Playify's bot process."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
EXPECTED_ENV = (PROJECT_ROOT / ".venv").resolve()


def main() -> None:
    if Path(sys.prefix).resolve() != EXPECTED_ENV:
        raise SystemExit("Run Playify through start.bat or start.sh so it uses .venv.")
    from src.playify.app import run

    run()


if __name__ == "__main__":
    main()
