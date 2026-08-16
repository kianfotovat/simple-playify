"""Cross-platform non-blocking key input for the dashboard."""

from __future__ import annotations

import contextlib
import os
import sys
import time

from rich.console import Console
from rich.prompt import Prompt
from rich.text import Text

from src.playify.messages import message


@contextlib.contextmanager
def terminal_mode():
    if os.name == "nt" or not sys.stdin.isatty():
        yield
        return
    import termios
    import tty

    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setcbreak(descriptor)
        yield
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def read_key() -> str | None:
    if os.name == "nt":
        import msvcrt

        if not msvcrt.kbhit():
            return None
        key = msvcrt.getwch()
        if key in {"\x00", "\xe0"}:
            return {
                "H": "up",
                "P": "down",
                "I": "pageup",
                "Q": "pagedown",
                "G": "home",
                "O": "end",
            }.get(msvcrt.getwch())
        return "esc" if key == "\x1b" else key.lower()

    import select

    if not select.select([sys.stdin], [], [], 0)[0]:
        return None
    first = sys.stdin.read(1)
    if first != "\x1b":
        return first.lower()
    sequence = ""
    while select.select([sys.stdin], [], [], 0.01)[0] and len(sequence) < 5:
        sequence += sys.stdin.read(1)
    return {
        "[A": "up",
        "[B": "down",
        "[5~": "pageup",
        "[6~": "pagedown",
        "[H": "home",
        "[F": "end",
        "[1~": "home",
        "[4~": "end",
    }.get(sequence, "esc")


def wait_for_key(console: Console, prompt: str = message("tui.key.return")) -> str:
    """Wait for one key in a TTY, with a line-input fallback for redirected stdin."""

    if not sys.stdin.isatty():
        console.input(prompt)
        return "enter"
    console.print(prompt, end="")
    try:
        with terminal_mode():
            while (key := read_key()) is None:
                time.sleep(0.03)
        return key
    finally:
        console.print()


def ask_with_escape(
    console: Console,
    prompt: str,
    *,
    choices: list[str] | tuple[str, ...] | None = None,
    default: str | None = None,
) -> str:
    """Read a line while allowing a physical Escape key to return immediately."""

    if not sys.stdin.isatty():
        return Prompt.ask(prompt, choices=choices, default=default).lower()

    allowed = {choice.lower() for choice in choices} if choices else None
    while True:
        label = Text.from_markup(prompt)
        if choices:
            label.append(" [", style="muted")
            for index, choice in enumerate(choices):
                if index:
                    label.append("/", style="muted")
                label.append(choice, style="key")
            label.append("]", style="muted")
        if default is not None:
            label.append(f" ({default})", style="muted")
        label.append(": ")
        console.print(label, end="")

        entered: list[str] = []
        with terminal_mode():
            while True:
                key = read_key()
                if key is None:
                    time.sleep(0.01)
                    continue
                if key == "esc":
                    console.print()
                    return "esc"
                if key in {"\r", "\n"}:
                    console.print()
                    value = "".join(entered).strip().lower()
                    if not value and default is not None:
                        value = default.lower()
                    break
                if key == "\x03":
                    console.print()
                    raise KeyboardInterrupt
                if key in {"\x08", "\x7f"}:
                    if entered:
                        entered.pop()
                        console.file.write("\b \b")
                        console.file.flush()
                    continue
                if len(key) == 1 and key.isprintable():
                    entered.append(key)
                    console.file.write(key)
                    console.file.flush()

        if allowed is None or value in allowed:
            return value
        console.print(message("tui.key.invalid_choice", choices=", ".join(choices or ())))
