from __future__ import annotations

import re
from io import StringIO
from typing import ClassVar
from unittest import TestCase
from unittest.mock import patch

from rich.console import Console
from rich.style import Style
from rich.theme import Theme

from src.playify.config import Config
from src.tui.dashboard import _dashboard, _log_text
from src.tui.main import _console
from src.tui.theme import PLAYIFY_THEME


def fresh_theme() -> Theme:
    """Avoid Rich's per-Style ANSI cache leaking between test consoles."""

    return Theme(
        {
            name: Style(
                color=style.color,
                bgcolor=style.bgcolor,
                bold=style.bold,
                dim=style.dim,
                italic=style.italic,
                underline=style.underline,
                blink=style.blink,
                blink2=style.blink2,
                reverse=style.reverse,
                conceal=style.conceal,
                strike=style.strike,
                underline2=style.underline2,
                frame=style.frame,
                encircle=style.encircle,
                overline=style.overline,
            )
            for name, style in PLAYIFY_THEME.styles.items()
        }
    )


class FakeBot:
    is_online = True
    is_running = True
    last_exit_code = None
    crash_count = 0
    uptime = "1h 23m"
    logs: ClassVar[list[str]] = [
        "2026-08-16 14:00:00 INFO playify: ready",
        "2026-08-16 14:00:01 WARNING playify: queue delayed",
        "2026-08-16 14:00:02 ERROR playify: stream failed",
    ]
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
            theme=fresh_theme(),
            force_terminal=True,
            color_system="truecolor",
            width=120,
        )
        color_console.print(_dashboard(FakeBot(), 120, 40))

        plain_output = StringIO()
        plain_console = Console(
            file=plain_output,
            theme=fresh_theme(),
            force_terminal=False,
            no_color=True,
            width=120,
        )
        plain_console.print(_dashboard(FakeBot(), 120, 40))

        self.assertIn("\x1b[", color_output.getvalue())
        color_sequences = set(re.findall(r"\x1b\[[0-9;]+m", color_output.getvalue()))
        self.assertGreaterEqual(len(color_sequences), 8)
        self.assertNotIn("\x1b[", plain_output.getvalue())
        self.assertIn("Song [live]", plain_output.getvalue())
        self.assertIn("ONLINE", plain_output.getvalue())
        self.assertLessEqual(len(plain_output.getvalue().splitlines()), 40)

    def test_standard_ansi_palette_keeps_distinct_accents(self) -> None:
        # Rich caches combined style escape codes globally, including the color
        # system used by earlier test consoles.
        Style._add.cache_clear()
        output = StringIO()
        console = Console(
            file=output,
            theme=fresh_theme(),
            force_terminal=True,
            color_system="standard",
            width=120,
        )
        console.print(_dashboard(FakeBot(), 120, 40))

        for color in ("\x1b[1;91m", "\x1b[1;92m", "\x1b[1;93m", "\x1b[1;95m", "\x1b[1;96m"):
            self.assertIn(color, output.getvalue())

    def test_log_severities_receive_semantic_styles(self) -> None:
        text = _log_text(
            [
                "2026-08-16 14:00:00 INFO playify: ready",
                "2026-08-16 14:00:01 WARNING playify: delayed",
                "2026-08-16 14:00:02 ERROR playify: failed",
                "2026-08-16 14:00:03 DEBUG playify: detail",
            ]
        )

        styles = [span.style for span in text.spans]
        self.assertIn("dash.log.info", styles)
        self.assertIn("dash.log.warning", styles)
        self.assertIn("dash.log.error", styles)
        self.assertIn("dash.log.debug", styles)
        self.assertIn("dash.log.source", styles)
