"""Shared dashboard-style layouts for interactive TUI menus."""

from __future__ import annotations

from collections.abc import Iterable

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def menu_layout(
    title: str,
    subtitle: str,
    rows: Iterable[tuple[str, str, str, str, str]],
    footer: RenderableType,
) -> Group:
    """Build a menu using the same visual hierarchy as the main dashboard."""

    heading = Table.grid(expand=True)
    heading.add_column()
    heading.add_column(justify="right")
    heading.add_row(Text("Playify", style="brand"), Text(title, style="dash.value"))

    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(width=3, justify="right", no_wrap=True)
    grid.add_column(ratio=2, no_wrap=True)
    grid.add_column(ratio=2)
    grid.add_column(ratio=5)
    for shortcut, label, value, description, value_style in rows:
        key = Text(shortcut, style="brand")
        name = Text()
        name.append("▌ ", style="dash.purple")
        name.append(label, style="dash.value")
        grid.add_row(key, name, Text(value, style=value_style), Text(description, style="dash.muted"))

    return Group(
        Panel(heading, border_style="dash.border", padding=(0, 1)),
        Panel(
            grid,
            title=Text(f" {title} ", style="brand"),
            subtitle=Text(subtitle, style="dash.muted"),
            border_style="dash.border",
            padding=(0, 1),
        ),
        Panel(Align.center(footer), border_style="dash.border", padding=(0, 1)),
    )
