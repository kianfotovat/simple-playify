"""Thin managed-environment entrypoint for Playify's bot process."""

from __future__ import annotations


def main() -> None:
    from src.playify.app import run

    run()


if __name__ == "__main__":
    main()
