from __future__ import annotations

import contextlib
from io import StringIO
from unittest import TestCase
from unittest.mock import patch

from rich.console import Console

from src.tui.key_input import ask_with_escape
from src.tui.theme import PLAYIFY_THEME


class TtyInput:
    def isatty(self) -> bool:
        return True


class KeyInputTests(TestCase):
    def setUp(self) -> None:
        self.output = StringIO()
        self.console = Console(file=self.output, theme=PLAYIFY_THEME, force_terminal=False)

    def test_physical_escape_returns_immediately(self) -> None:
        with (
            patch("src.tui.key_input.sys.stdin", TtyInput()),
            patch("src.tui.key_input.terminal_mode", return_value=contextlib.nullcontext()),
            patch("src.tui.key_input.read_key", return_value="esc"),
        ):
            result = ask_with_escape(self.console, "Choice", choices=["1", "esc"], default="esc")

        self.assertEqual(result, "esc")

    def test_multi_digit_selection_is_preserved(self) -> None:
        with (
            patch("src.tui.key_input.sys.stdin", TtyInput()),
            patch("src.tui.key_input.terminal_mode", return_value=contextlib.nullcontext()),
            patch("src.tui.key_input.read_key", side_effect=["1", "2", "\r"]),
        ):
            result = ask_with_escape(self.console, "Setting", default="esc")

        self.assertEqual(result, "12")
