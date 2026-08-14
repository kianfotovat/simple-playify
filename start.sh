#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Playify requires Python 3.12-3.14 x64. Install it with your package manager." >&2
    exit 1
fi
python3 -c 'import platform,sys; raise SystemExit(0 if sys.version_info[:2] in {(3,12),(3,13),(3,14)} and platform.machine().lower() in {"x86_64","amd64"} else 1)' || {
    echo "Playify requires Python 3.12-3.14 on x86-64." >&2
    exit 1
}
exec python3 bootstrap.py
