"""Cross-platform non-blocking key input for the dashboard."""

from __future__ import annotations

import contextlib
import os
import sys
import time

from rich.console import Console


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


def wait_for_key(console: Console, prompt: str = "Press any key or Esc to return…") -> str:
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
