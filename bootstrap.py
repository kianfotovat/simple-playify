"""Shared Windows/Linux launcher and runtime bootstrap."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, NoReturn
from uuid import uuid4

from src.playify.constants import (
    LAUNCH_STAGE_ENV,
    RUNTIME_PREFIX_ENV,
    TUI_LAUNCH_STAGE,
    TUI_RUNTIME_REFRESH_EXIT,
)
from src.playify.messages import message

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
DATA = ROOT / "data"
SETTINGS = DATA / "settings.json"
INSTALLATION = DATA / "installation.json"
REQUIREMENTS = ROOT / "requirements.txt"
OWNERSHIP_MARKER = ".playify-runtime.json"
MINIMUM_PYTHON = (3, 11)
IMPORT_CHECK = (
    "import aiohttp, aiosqlite, cachetools, discord, dotenv, psutil, rich, spotipy, spotify_scraper, yt_dlp; "
    "import nacl, davey"
)

SETTINGS_DEFAULTS: dict[str, Any] = {
    "persistence_mode": "full",
    "updates_enabled": True,
    "tidal_country": "US",
    "soundcloud_fallback": True,
    "private_media_allowlist": [],
    "ip_mode": "auto",
    "youtube_clients": ["web", "android", "ios"],
    "worker_count": "auto",
    "http_concurrency": "auto",
    "tui_refresh": "auto",
    "color_mode": "auto",
    "symbol_mode": "auto",
    "controller_idle_image": "https://i.imgur.com/vDusBWD.png",
    "bot_status_type": "none",
    "bot_status_text": "",
}

INSTALLATION_DEFAULTS: dict[str, Any] = {
    "last_dependency_check": None,
    "dependency_snooze_until": None,
    "last_ffmpeg_check": None,
    "update_remind_after": None,
    "ignored_update_sha": None,
    "previous_update_sha": None,
    "last_update_sha": None,
    "last_chrome_major": 151,
    "requirements_hash": None,
    "python_version": None,
    "runtime_mode": None,
    "runtime_path": None,
    "runtime_base_python": None,
    "runtime_ownership_id": None,
}


def fail(detail: str) -> NoReturn:
    print(message("bootstrap.output", detail=detail), file=sys.stderr)
    raise SystemExit(1)


def supported_host() -> bool:
    return sys.platform.startswith(("win32", "linux")) and platform.machine().lower() in {
        "amd64",
        "x86_64",
    }


def venv_python(path: Path) -> Path:
    return path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def active_virtual_environment() -> Path | None:
    """Return the active virtual environment, including virtualenv and Conda-style prefixes."""

    prefix = Path(sys.prefix).resolve()
    base_prefix = Path(getattr(sys, "base_prefix", sys.prefix)).resolve()
    if prefix != base_prefix or hasattr(sys, "real_prefix"):
        return prefix
    conda_value = os.getenv("CONDA_PREFIX")
    if conda_value:
        conda_prefix = Path(conda_value).resolve()
        if Path(sys.executable).resolve().is_relative_to(conda_prefix):
            return conda_prefix
    return None


def _python_supported(executable: Path) -> bool:
    if not executable.is_file():
        return False
    try:
        result = subprocess.run(
            [
                str(executable),
                "-c",
                "import platform,sys; raise SystemExit(0 if "
                "sys.version_info.major == 3 and sys.version_info[:2] >= (3,11) and "
                "platform.machine().lower() in {'amd64','x86_64'} else 1)",
            ],
            cwd=ROOT,
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _current_base_python() -> Path | None:
    for raw in (getattr(sys, "_base_executable", None), sys.executable):
        if raw:
            candidate = Path(raw).resolve()
            if _python_supported(candidate):
                return candidate
    return None


def _path_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _runtime_path_safe(path: Path) -> bool:
    resolved = path.resolve()
    root = ROOT.resolve()
    home = Path.home().resolve()
    return (
        resolved.parent != resolved
        and len(resolved.parts) >= 3
        and resolved not in {root, home}
        and not root.is_relative_to(resolved)
    )


def _adoption_supported(path: Path) -> bool:
    resolved = path.resolve()
    if resolved == VENV.resolve() or not _runtime_path_safe(resolved):
        return False
    if not (resolved / "pyvenv.cfg").is_file() or (resolved / "conda-meta").exists():
        return False
    base = _current_base_python()
    return base is not None and not _path_within(base, resolved)


def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _initialize_json(path: Path, defaults: Mapping[str, Any]) -> dict[str, Any]:
    loaded: dict[str, Any] = {}
    valid = False
    if path.exists():
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(candidate, dict):
                raise ValueError("the JSON root must be an object")
            loaded = candidate
            valid = True
        except (OSError, ValueError, json.JSONDecodeError):
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            backup = path.with_name(
                f"{path.stem}.invalid-{stamp}-{os.getpid()}.json"
            )
            shutil.copy2(path, backup)
            print(
                message(
                    "bootstrap.output",
                    detail=message(
                        "bootstrap.data_invalid", path=path, backup=backup
                    ),
                )
            )
    complete = deepcopy(dict(defaults))
    complete.update(loaded)
    if not valid or complete != loaded:
        _atomic_json_write(path, complete)
    return complete


def initialize_data_files() -> dict[str, Any]:
    """Create or complete both JSON files before application modules import them."""

    _initialize_json(SETTINGS, SETTINGS_DEFAULTS)
    return _initialize_json(INSTALLATION, INSTALLATION_DEFAULTS)


def load_metadata() -> dict[str, Any]:
    value = json.loads(INSTALLATION.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        fail(message("bootstrap.data_initialize_failed", error="invalid installation.json"))
    return value


def save_metadata(value: Mapping[str, Any]) -> None:
    _atomic_json_write(INSTALLATION, value)


def _new_ownership_id(metadata: Mapping[str, Any]) -> str:
    current = metadata.get("runtime_ownership_id")
    if isinstance(current, str) and len(current) == 32:
        try:
            int(current, 16)
        except ValueError:
            pass
        else:
            return current.lower()
    return uuid4().hex


def _marker_value(metadata: Mapping[str, Any]) -> dict[str, str]:
    return {
        "installation_id": str(metadata["runtime_ownership_id"]),
        "project_root": str(ROOT.resolve()),
    }


def _write_ownership_marker(path: Path, metadata: Mapping[str, Any]) -> None:
    _atomic_json_write(path / OWNERSHIP_MARKER, _marker_value(metadata))


def _marker_matches(path: Path, metadata: Mapping[str, Any]) -> bool:
    try:
        value = json.loads((path / OWNERSHIP_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return value == _marker_value(metadata)


def _runtime_record_matches(path: Path, metadata: Mapping[str, Any]) -> bool:
    recorded = metadata.get("runtime_path")
    return isinstance(recorded, str) and Path(recorded).resolve() == path.resolve()


def _verify_owned_runtime(path: Path, metadata: Mapping[str, Any]) -> None:
    resolved = path.resolve()
    if (
        not _runtime_path_safe(resolved)
        or not _runtime_record_matches(resolved, metadata)
        or not resolved.is_dir()
        or not (resolved / "pyvenv.cfg").is_file()
        or not _marker_matches(resolved, metadata)
    ):
        fail(message("bootstrap.runtime_ownership_invalid", path=resolved))


def _record_runtime(
    metadata: dict[str, Any], mode: str, path: Path, base_python: Path
) -> dict[str, Any]:
    metadata.update(
        {
            "runtime_mode": mode,
            "runtime_path": str(path.resolve()),
            "runtime_base_python": str(base_python.resolve()),
            "runtime_ownership_id": _new_ownership_id(metadata),
        }
    )
    metadata.pop("pending_environment", None)
    metadata.pop("pending_requirements_hash", None)
    save_metadata(metadata)
    return metadata


def _choose_custom_runtime(active: Path) -> tuple[str, Path, bool]:
    print(
        message(
            "bootstrap.output",
            detail=message("bootstrap.runtime_detected", path=active),
        )
    )
    if not _adoption_supported(active):
        print(
            message(
                "bootstrap.output",
                detail=message(
                    "bootstrap.runtime_not_adoptable", path=VENV.resolve()
                ),
            )
        )
        return "project", VENV.resolve(), False
    print(
        message(
            "bootstrap.output",
            detail=message(
                "bootstrap.runtime_options",
                project=VENV.resolve(),
                active=active,
            ),
        )
    )
    print(
        message(
            "bootstrap.output", detail=message("bootstrap.runtime_adoption_effect")
        )
    )
    while True:
        choice = input(message("bootstrap.runtime_choice")).strip()
        if choice == "1":
            return "project", VENV.resolve(), False
        if choice == "2":
            confirmation = input(
                message("bootstrap.runtime_adoption_confirm", path=active)
            ).strip()
            if confirmation == "MANAGE":
                return "adopted", active.resolve(), True
            print(
                message(
                    "bootstrap.output",
                    detail=message("bootstrap.runtime_adoption_cancelled"),
                )
            )
        else:
            print(
                message(
                    "bootstrap.output",
                    detail=message("bootstrap.runtime_choice_invalid"),
                )
            )


def _base_python_for_runtime(
    metadata: Mapping[str, Any], runtime: Path
) -> Path:
    candidates: list[Path] = []
    recorded = metadata.get("runtime_base_python")
    if isinstance(recorded, str) and recorded:
        candidates.append(Path(recorded).resolve())
    current = _current_base_python()
    if current is not None:
        candidates.append(current)
    for candidate in candidates:
        if not _path_within(candidate, runtime) and _python_supported(candidate):
            return candidate
    fail(message("bootstrap.runtime_base_unavailable", path=runtime))


def select_runtime(
    metadata: dict[str, Any]
) -> tuple[Path, Path, dict[str, Any]]:
    """Resolve the persisted ownership decision, prompting once when appropriate."""

    active = active_virtual_environment()
    mode = metadata.get("runtime_mode")
    first_adoption = False
    if mode == "project":
        runtime = VENV.resolve()
    elif mode == "adopted" and isinstance(metadata.get("runtime_path"), str):
        runtime = Path(str(metadata["runtime_path"])).resolve()
    else:
        if active is not None and active.resolve() != VENV.resolve():
            mode, runtime, first_adoption = _choose_custom_runtime(active)
        else:
            mode, runtime = "project", VENV.resolve()

    if not _runtime_path_safe(runtime):
        fail(message("bootstrap.runtime_path_unsafe", path=runtime))
    base_python = _base_python_for_runtime(metadata, runtime)
    metadata = _record_runtime(metadata, str(mode), runtime, base_python)

    if runtime.exists():
        if not runtime.is_dir() or not (runtime / "pyvenv.cfg").is_file():
            fail(message("bootstrap.runtime_ownership_invalid", path=runtime))
        if mode == "project" or first_adoption:
            _write_ownership_marker(runtime, metadata)
        else:
            _verify_owned_runtime(runtime, metadata)
    return runtime, base_python, metadata


def _handoff_if_inside_runtime(runtime: Path, base_python: Path) -> None:
    active = active_virtual_environment()
    if active is None or active.resolve() != runtime.resolve():
        return
    if _path_within(base_python, runtime):
        fail(message("bootstrap.runtime_base_unavailable", path=runtime))
    print(message("bootstrap.output", detail=message("bootstrap.runtime_handoff")))
    try:
        os.execv(
            str(base_python),
            [str(base_python), str(ROOT / "bootstrap.py"), *sys.argv[1:]],
        )
    except OSError as exc:
        fail(message("bootstrap.runtime_handoff_failed", error=exc))


def requirements_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def environment_valid(path: Path) -> bool:
    python = venv_python(path)
    if not python.is_file():
        return False
    try:
        version = subprocess.run(
            [
                str(python),
                "-c",
                "import sys; raise SystemExit(0 if "
                "sys.version_info.major == 3 and sys.version_info[:2] >= (3,11) else 1)",
            ],
            cwd=ROOT,
            capture_output=True,
            timeout=20,
        )
        if version.returncode != 0:
            return False
        imports = subprocess.run(
            [str(python), "-c", IMPORT_CHECK],
            cwd=ROOT,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return imports.returncode == 0


def dependency_state(
    environment: Path, metadata: dict[str, Any]
) -> tuple[bool, bool]:
    mandatory = (
        not environment_valid(environment)
        or metadata.get("requirements_hash") != requirements_hash()
    )
    last = metadata.get("last_dependency_check")
    stale = True
    if last:
        try:
            stale = datetime.now(UTC) - datetime.fromisoformat(last) >= timedelta(days=30)
        except (TypeError, ValueError):
            pass
    snooze = metadata.get("dependency_snooze_until")
    if snooze:
        try:
            stale = stale and datetime.now(UTC) >= datetime.fromisoformat(snooze)
        except (TypeError, ValueError):
            pass
    return mandatory, stale


def _runtime_python_version(runtime: Path) -> str | None:
    try:
        result = subprocess.run(
            [
                str(venv_python(runtime)),
                "-c",
                "import platform; print(platform.python_version())",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _record_dependency_check(
    metadata: dict[str, Any], runtime: Path
) -> None:
    metadata.update(
        {
            "requirements_hash": requirements_hash(),
            "last_dependency_check": datetime.now(UTC).isoformat(),
            "dependency_snooze_until": None,
            "python_version": _runtime_python_version(runtime),
        }
    )
    save_metadata(metadata)


def _temporary_runtime_path(runtime: Path, role: str, token: str) -> Path:
    return runtime.parent / f"{runtime.name}.playify-{role}-{token}"


def _cleanup_temporary_runtime(
    path: Path,
    runtime: Path,
    role: str,
    token: str,
    metadata: Mapping[str, Any],
    *,
    require_marker: bool,
) -> None:
    expected = _temporary_runtime_path(runtime, role, token)
    if (
        path.resolve() != expected.resolve()
        or path.parent.resolve() != runtime.parent.resolve()
        or path.is_symlink()
        or (require_marker and not _marker_matches(path, metadata))
    ):
        fail(message("bootstrap.cleanup_refused"))
    shutil.rmtree(path)


def stage_environment(
    runtime: Path, base_python: Path, metadata: dict[str, Any]
) -> None:
    """Build, validate, and atomically replace an owned virtual environment."""

    runtime = runtime.resolve()
    if not _runtime_record_matches(runtime, metadata) or not _runtime_path_safe(runtime):
        fail(message("bootstrap.runtime_path_unsafe", path=runtime))
    if _path_within(base_python, runtime):
        fail(message("bootstrap.runtime_base_unavailable", path=runtime))
    token = f"{os.getpid()}-{uuid4().hex[:8]}"
    candidate = _temporary_runtime_path(runtime, "candidate", token)
    backup = _temporary_runtime_path(runtime, "previous", token)
    if candidate.exists() or backup.exists():
        fail(message("bootstrap.cleanup_refused"))

    print(message("bootstrap.output", detail=message("bootstrap.building")))
    try:
        subprocess.run(
            [str(base_python), "-m", "venv", str(candidate)],
            check=True,
            cwd=ROOT,
        )
        _write_ownership_marker(candidate, metadata)
        subprocess.run(
            [
                str(venv_python(candidate)),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(REQUIREMENTS),
            ],
            check=True,
            cwd=ROOT,
        )
        if not environment_valid(candidate):
            raise RuntimeError(message("bootstrap.stage_failed"))
    except BaseException:
        if candidate.exists():
            _cleanup_temporary_runtime(
                candidate,
                runtime,
                "candidate",
                token,
                metadata,
                require_marker=False,
            )
        raise

    if runtime.exists():
        _verify_owned_runtime(runtime, metadata)
        runtime.replace(backup)
    candidate_installed = False
    try:
        candidate.replace(runtime)
        candidate_installed = True
        _verify_owned_runtime(runtime, metadata)
        if not environment_valid(runtime):
            raise RuntimeError(message("bootstrap.stage_failed"))
    except BaseException:
        if candidate_installed and runtime.exists():
            runtime.replace(candidate)
        if backup.exists():
            backup.replace(runtime)
        if candidate.exists():
            _cleanup_temporary_runtime(
                candidate,
                runtime,
                "candidate",
                token,
                metadata,
                require_marker=True,
            )
        raise

    if backup.exists():
        _cleanup_temporary_runtime(
            backup,
            runtime,
            "previous",
            token,
            metadata,
            require_marker=True,
        )
    _record_dependency_check(metadata, runtime)


def _launch_tui(runtime: Path, base_python: Path) -> NoReturn:
    while True:
        child_environment = os.environ.copy()
        child_environment[LAUNCH_STAGE_ENV] = TUI_LAUNCH_STAGE
        child_environment[RUNTIME_PREFIX_ENV] = str(runtime.resolve())
        code = subprocess.call(
            [str(venv_python(runtime)), "-m", "src.tui"],
            cwd=ROOT,
            env=child_environment,
        )
        if code != TUI_RUNTIME_REFRESH_EXIT:
            raise SystemExit(code)
        metadata = load_metadata()
        _verify_owned_runtime(runtime, metadata)
        stage_environment(runtime, base_python, metadata)


def main() -> None:
    if not supported_host():
        fail(message("bootstrap.host_unsupported"))
    if sys.version_info.major != 3 or sys.version_info[:2] < MINIMUM_PYTHON:
        fail(message("bootstrap.python_unsupported"))
    if not REQUIREMENTS.is_file():
        fail(message("bootstrap.requirements_missing"))
    try:
        metadata = initialize_data_files()
    except OSError as exc:
        fail(message("bootstrap.data_initialize_failed", error=exc))

    runtime, base_python, metadata = select_runtime(metadata)
    _handoff_if_inside_runtime(runtime, base_python)
    mandatory, stale = dependency_state(runtime, metadata)
    if mandatory:
        stage_environment(runtime, base_python, metadata)
    elif stale:
        answer = input(
            message(
                "bootstrap.output", detail=message("bootstrap.dependencies_due")
            )
        ).strip().lower()
        if answer in {"", "y", "yes"}:
            stage_environment(runtime, base_python, metadata)
        else:
            metadata["dependency_snooze_until"] = (
                datetime.now(UTC) + timedelta(days=3)
            ).isoformat()
            save_metadata(metadata)
    if not environment_valid(runtime):
        fail(message("bootstrap.environment_invalid"))
    _launch_tui(runtime, base_python)


if __name__ == "__main__":
    main()
