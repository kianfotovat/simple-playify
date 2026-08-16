"""Playify TUI — Visual theme, colors, and style constants."""

from rich.style import Style
from rich.theme import Theme

# ─── Brand Palette ──────────────────────────────────────────────────────────
BRAND_PURPLE = "#7571D5"
BRAND_PALE = "#DCDCF5"
BRAND_GRADIENT = (
    BRAND_PURPLE,
    "#8683DB",
    "#9894E0",
    "#AAA6E6",
    "#BBB8EB",
    "#C3BFEF",
    "#CEC8F3",
)

# Interface accents over neutral chrome. These remain visually
# distinct when Rich downsamples them to the standard 16-color ANSI palette.
DASH_PURPLE = "#C678DD"
DASH_CYAN = "#5CCFE6"
DASH_PINK = "#F071A7"
DASH_ORANGE = "#FFAE57"
DASH_GREEN = "#A6E22E"
DASH_YELLOW = "#FFD866"
DASH_RED = "#FF5C57"
DASH_TEXT = "#D8DEE9"
DASH_MUTED = "#98A2B3"
DASH_BORDER = "#3B4261"

# ─── Rich Theme ──────────────────────────────────────────────────────────────
PLAYIFY_THEME = Theme(
    {
        "title": Style(color=BRAND_PURPLE, bold=True),
        "subtitle": Style(color=BRAND_PALE),
        "success": Style(color=DASH_GREEN, bold=True),
        "error": Style(color=DASH_RED, bold=True),
        "warning": Style(color=DASH_YELLOW, bold=True),
        "info": Style(color=DASH_CYAN),
        "muted": Style(color=DASH_MUTED),
        "accent": Style(color=BRAND_PURPLE, bold=True),
        "key": Style(color=BRAND_PALE, bold=True),
        "value": Style(color=DASH_TEXT),
        "header": Style(color=BRAND_PURPLE, bold=True),
        "border": Style(color=DASH_BORDER),
        "log.info": Style(color=DASH_CYAN),
        "log.warning": Style(color=DASH_YELLOW),
        "log.error": Style(color=DASH_RED, bold=True),
        "log.debug": Style(color=DASH_MUTED),
        "prompt": Style(color=BRAND_PALE, bold=True),
        "input": Style(color=DASH_TEXT, bold=True),
        "status.online": Style(color=DASH_GREEN, bold=True),
        "status.starting": Style(color=DASH_YELLOW, bold=True),
        "status.offline": Style(color=DASH_RED),
        "music.title": Style(color=DASH_TEXT, bold=True),
        "music.artist": Style(color=BRAND_PALE),
        "music.time": Style(color=DASH_CYAN),
        "hotkey": Style(color=BRAND_PURPLE, bold=True),
        "hotkey.desc": Style(color=DASH_MUTED),
        "dash.purple": Style(color=DASH_PURPLE, bold=True),
        "dash.cyan": Style(color=DASH_CYAN, bold=True),
        "dash.pink": Style(color=DASH_PINK, bold=True),
        "dash.orange": Style(color=DASH_ORANGE, bold=True),
        "dash.green": Style(color=DASH_GREEN, bold=True),
        "dash.yellow": Style(color=DASH_YELLOW, bold=True),
        "dash.red": Style(color=DASH_RED, bold=True),
        "dash.text": Style(color=DASH_TEXT),
        "dash.value": Style(color=DASH_TEXT, bold=True),
        "dash.muted": Style(color=DASH_MUTED),
        "dash.border": Style(color=DASH_BORDER),
        "brand": Style(color=BRAND_PURPLE, bold=True),
        "dash.status.online": Style(color="#111318", bgcolor=DASH_GREEN, bold=True),
        "dash.status.starting": Style(color="#111318", bgcolor=DASH_YELLOW, bold=True),
        "dash.status.offline": Style(color="#FFFFFF", bgcolor=DASH_RED, bold=True),
        "dash.log.debug": Style(color=DASH_MUTED, bold=True),
        "dash.log.info": Style(color=DASH_CYAN, bold=True),
        "dash.log.warning": Style(color=DASH_YELLOW, bold=True),
        "dash.log.error": Style(color=DASH_RED, bold=True),
    }
)

# ─── Box Characters ──────────────────────────────────────────────────────────
BOX_H = "━"
BOX_V = "┃"
BOX_TL = "┏"
BOX_TR = "┓"
BOX_BL = "┗"
BOX_BR = "┛"
BOX_T = "┳"
BOX_B = "┻"
BOX_L = "┣"
BOX_R = "┫"
BOX_X = "╋"

# ─── Status Icons (ASCII/Unicode only, no emojis) ────────────────────────────
ICON_CHECK = "+"
ICON_CROSS = "x"
ICON_MUSIC = "♪"
ICON_PLAY = ">"
ICON_PAUSE = "||"
ICON_STOP = "[]"
ICON_LOOP = "~"
ICON_VOLUME = "))"
ICON_ONLINE = "*"
ICON_OFFLINE = "o"
ICON_ARROW = "->"
ICON_SPARK = "::"
ICON_WARN = "/!\\"
ICON_GEAR = "#"
ICON_ROCKET = ">>"

VERSION = "2.1.0"
