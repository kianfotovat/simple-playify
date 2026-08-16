"""Bot subprocess supervision and structured event capture."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from collections import Counter, deque
from pathlib import Path
from typing import Any

import psutil

from src.playify.constants import BOT_LAUNCH_STAGE, LAUNCH_STAGE_ENV


class BotProcess:
    def __init__(self, project_root: Path, python_executable: Path) -> None:
        self.project_root = project_root
        self.python_executable = python_executable
        self.process: subprocess.Popen[str] | None = None
        self.logs: deque[str] = deque(maxlen=2_000)
        self.events: Counter[str] = Counter()
        self.metrics: dict[str, Any] = {}
        self.players: dict[int, dict[str, Any]] = {}
        self.reader: threading.Thread | None = None
        self.started_at: float | None = None
        self.last_exit_code: int | None = None
        self.control_file: Path | None = None
        self.crash_count = 0
        self._capture = False
        self._metric_cache_at = 0.0
        self._memory_mb = 0.0
        self._ffmpeg_processes = 0

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def is_online(self) -> bool:
        return self.is_running and bool(self.metrics)

    @property
    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self.started_at) if self.started_at and self.is_running else 0

    @property
    def uptime(self) -> str:
        seconds = self.uptime_seconds
        hours, remainder = divmod(seconds, 3_600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}h {minutes}m" if hours else f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

    def start(self) -> None:
        if self.is_running:
            return
        self._cleanup_control_file()
        control_id = uuid.uuid4().hex
        control_directory = self.project_root / "data" / "tmp"
        control_directory.mkdir(parents=True, exist_ok=True)
        self.control_file = control_directory / f"control-{control_id}.stop"
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment[LAUNCH_STAGE_ENV] = BOT_LAUNCH_STAGE
        environment["PLAYIFY_CONTROL_ID"] = control_id
        environment["PATH"] = str(self.project_root / "bin") + os.pathsep + environment.get("PATH", "")
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        self.process = subprocess.Popen(
            [str(self.python_executable), str(self.project_root / "playify.py")],
            cwd=self.project_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=flags,
        )
        self.started_at = time.monotonic()
        self.last_exit_code = None
        self.metrics.clear()
        self.players.clear()
        self._capture = True
        self.reader = threading.Thread(target=self._read, name="playify-output", daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            for raw in self.process.stdout:
                if not self._capture:
                    break
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                if line.startswith("PLAYIFY_EVENT "):
                    try:
                        self._event(json.loads(line.removeprefix("PLAYIFY_EVENT ")))
                        continue
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                self.logs.append(line)
        finally:
            if self.process:
                self.last_exit_code = self.process.poll()
                if self.last_exit_code not in {None, 0}:
                    self.crash_count += 1
            self._cleanup_control_file()

    def _event(self, payload: dict[str, Any]) -> None:
        kind = str(payload.get("type", "unknown"))
        self.events[kind] += 1
        if kind == "log":
            self.logs.append(
                f"{payload.get('level', 'INFO'):>7} {payload.get('logger', 'playify')}: {payload.get('message', '')}"
            )
        elif kind == "heartbeat":
            self.metrics.update(payload)
        elif kind == "player":
            guild_id = int(payload.get("guild_id", 0))
            self.players.pop(guild_id, None)
            self.players[guild_id] = payload
            self.events[str(payload.get("event", "player"))] += 1

    def wait_for_startup(self, timeout: float = 30) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_online:
                return "online"
            if not self.is_running:
                if self.process:
                    self.last_exit_code = self.process.poll()
                return "crashed"
            time.sleep(0.1)
        return "timeout"

    def request_stop(self) -> None:
        if not self.process or not self.is_running:
            return
        if self.control_file:
            try:
                self.control_file.write_text("stop\n", encoding="utf-8")
                return
            except OSError:
                pass
        if os.name == "nt":
            self.process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            self.process.terminate()

    def wait_for_stop(self, timeout: float = 15) -> bool:
        if not self.process:
            return True
        try:
            self.process.wait(timeout=timeout)
            self.last_exit_code = self.process.returncode
            self._capture = False
            self._cleanup_control_file()
            return True
        except subprocess.TimeoutExpired:
            return False

    def force_stop(self) -> None:
        if self.process and self.is_running:
            self.process.kill()
            self.process.wait(timeout=5)
        self._capture = False
        self.last_exit_code = self.process.returncode if self.process else self.last_exit_code
        self._cleanup_control_file()

    def _cleanup_control_file(self) -> None:
        if self.control_file:
            self.control_file.unlink(missing_ok=True)
            self.control_file = None

    def restart(self) -> str:
        self.request_stop()
        if not self.wait_for_stop(15):
            return "stuck"
        self.start()
        return self.wait_for_startup(30)

    def process_metrics(self) -> tuple[float, int]:
        if time.monotonic() - self._metric_cache_at < 2:
            return self._memory_mb, self._ffmpeg_processes
        self._metric_cache_at = time.monotonic()
        memory = 0
        ffmpeg = 0
        if self.process and self.is_running:
            try:
                parent = psutil.Process(self.process.pid)
                processes = [parent, *parent.children(recursive=True)]
                for child in processes:
                    try:
                        memory += child.memory_info().rss
                        if "ffmpeg" in child.name().lower():
                            ffmpeg += 1
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        self._memory_mb = memory / (1024 * 1024)
        self._ffmpeg_processes = ffmpeg
        return self._memory_mb, self._ffmpeg_processes

    def recent_logs(self, count: int = 100) -> list[str]:
        return list(self.logs)[-count:]

    def now_playing(self) -> dict[str, Any] | None:
        active = [value for value in self.players.values() if value.get("active") and value.get("track")]
        dormant = [value for value in self.players.values() if value.get("dormant") and value.get("track")]
        return (active or dormant or [None])[-1]
