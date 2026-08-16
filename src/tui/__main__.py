"""Playify TUI entrypoint."""

import os
import sys
from pathlib import Path

from src.playify.constants import (
    LAUNCH_STAGE_ENV,
    RUNTIME_PREFIX_ENV,
    TUI_LAUNCH_STAGE,
)
from src.playify.messages import message

expected_prefix = os.getenv(RUNTIME_PREFIX_ENV, "")
if (
    os.getenv(LAUNCH_STAGE_ENV) != TUI_LAUNCH_STAGE
    or not expected_prefix
    or Path(sys.prefix).resolve() != Path(expected_prefix).resolve()
):
    raise SystemExit(message("bootstrap.run_managed"))

from .main import main

if __name__ == "__main__":
    main()
