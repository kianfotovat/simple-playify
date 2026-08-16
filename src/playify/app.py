"""Bot-process entrypoint."""

import os
import sys
from pathlib import Path

from .constants import BOT_LAUNCH_STAGE, LAUNCH_STAGE_ENV, RUNTIME_PREFIX_ENV
from .messages import message


def _internal_launch_valid() -> bool:
    control_id = os.getenv("PLAYIFY_CONTROL_ID", "").strip().lower()
    expected_prefix = os.getenv(RUNTIME_PREFIX_ENV, "")
    return (
        os.getenv(LAUNCH_STAGE_ENV) == BOT_LAUNCH_STAGE
        and len(control_id) == 32
        and all(character in "0123456789abcdef" for character in control_id)
        and bool(expected_prefix)
        and Path(sys.prefix).resolve() == Path(expected_prefix).resolve()
    )


def run() -> None:
    if not _internal_launch_valid():
        raise SystemExit(message("bootstrap.run_managed"))
    from .discord_app import run_bot

    run_bot()


if __name__ == "__main__":
    run()
