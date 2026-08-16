#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Playify requires Python 3.11 or newer on x86-64. Install it with your package manager." >&2
    exit 1
fi
python3 -c 'import platform,sys; raise SystemExit(0 if sys.version_info.major == 3 and sys.version_info[:2] >= (3,11) and platform.machine().lower() in {"x86_64","amd64"} else 1)' || {
    echo "Playify requires Python 3.11 or newer on x86-64." >&2
    exit 1
}
exec python3 bootstrap.py
