"""Bot-process entrypoint."""

from .discord_app import run_bot


def run() -> None:
    run_bot()


if __name__ == "__main__":
    run()
