from __future__ import annotations

from io import StringIO
from typing import ClassVar
from unittest import TestCase
from unittest.mock import patch

from rich.console import Console

from src.playify.config import Config
from src.tui.dashboard import _dashboard, _log_text
from src.tui.main import _console
from src.tui.theme import PLAYIFY_THEME


class FakeBot:
    is_online = True
    is_running = True
    last_exit_code = None
    crash_count = 0
    uptime = "1h 23m"
    logs: ClassVar[list[str]] = ["INFO ready", "WARNING queue delayed", "ERROR stream failed"]
    metrics: ClassVar[dict[str, int]] = {
        "servers": 3,
        "players": 1,
        "queued": 4,
        "cache": 12,
    }

    def process_metrics(self) -> tuple[float, int]:
        return 42.5, 1

    def now_playing(self) -> dict[str, object]:
        return {
            "active": True,
            "track": "Song [live]",
            "guild_id": 123,
            "queued": 4,
            "pending": 1,
        }

    def recent_logs(self, count: int) -> list[str]:
        return self.logs[-count:]


class DashboardColorTests(TestCase):
    def test_auto_color_mode_uses_rich_capability_detection(self) -> None:
        def setting(key: str, default: object = None) -> object:
            return "auto" if key == "color_mode" else default

        with patch.object(Config, "get", side_effect=setting), patch("src.tui.main.Console") as constructor:
            _console()

        self.assertEqual(constructor.call_args.kwargs["color_system"], "auto")
        self.assertIsNone(constructor.call_args.kwargs["no_color"])

    def test_no_color_mode_disables_terminal_styling(self) -> None:
        def setting(key: str, default: object = None) -> object:
            return "none" if key == "color_mode" else default

        with patch.object(Config, "get", side_effect=setting), patch("src.tui.main.Console") as constructor:
            _console()

        self.assertIsNone(constructor.call_args.kwargs["color_system"])
        self.assertTrue(constructor.call_args.kwargs["no_color"])
        self.assertFalse(constructor.call_args.kwargs["force_terminal"])

    def test_dashboard_renders_color_and_plain_fallback(self) -> None:
        color_output = StringIO()
        color_console = Console(
            file=color_output,
            theme=PLAYIFY_THEME,
            force_terminal=True,
            color_system="truecolor",
            width=120,
        )
        color_console.print(_dashboard(FakeBot(), 120, 40))

        plain_output = StringIO()
        plain_console = Console(
            file=plain_output,
            theme=PLAYIFY_THEME,
            force_terminal=False,
            no_color=True,
            width=120,
        )
        plain_console.print(_dashboard(FakeBot(), 120, 40))

        self.assertIn("\x1b[", color_output.getvalue())
        self.assertNotIn("\x1b[", plain_output.getvalue())
        self.assertIn("Song [live]", plain_output.getvalue())
        self.assertIn("ONLINE", plain_output.getvalue())

    def test_log_severities_receive_semantic_styles(self) -> None:
        text = _log_text(["INFO ready", "WARNING delayed", "ERROR failed", "DEBUG detail"])

        self.assertEqual(
            [span.style for span in text.spans],
            ["log.info", "log.warning", "log.error", "log.debug"],
        )
