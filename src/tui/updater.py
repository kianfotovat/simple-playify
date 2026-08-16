"""Confirmation-based Git updater for the canonical Playify fork."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from src.playify.config import Installation
from src.playify.messages import message

CANONICAL_ORIGIN = "https://github.com/kianfotovat/simple-playify.git"
CHECK_TIMEOUT = 10
SHA = re.compile(r"^[0-9a-f]{40}$")
Relation = Literal[
    "up_to_date", "behind", "ahead", "diverged", "missing_main", "non_git", "error"
]
UpdateAction = Literal["install", "rollback", "skip"]


class UpdateError(RuntimeError):
    """An updater condition that is safe to show to the local operator."""


@dataclass(slots=True)
class UpdateStatus:
    root: Path
    relation: Relation
    head_sha: str | None = None
    main_sha: str | None = None
    target_sha: str | None = None
    branch: str | None = None
    commit_message: str = ""
    rollback_sha: str | None = None
    suppressed_reason: str | None = None
    error: str | None = None
    manual: bool = False
    dirty_tracked: tuple[str, ...] = ()
    selected_target: str | None = None
    confirmed_dirty: tuple[str, ...] = field(default_factory=tuple)
    discard_confirmed: bool = False
    switch_confirmed: bool = False


def _git(
    root: Path,
    *arguments: str,
    timeout: int = CHECK_TIMEOUT,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise UpdateError(message("tui.update.git_missing")) from exc
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(
            message(
                "tui.update.git_timeout",
                command=" ".join(arguments[:2]),
                seconds=timeout,
            )
        ) from exc
    if check and result.returncode != 0:
        detail = (
            result.stderr
            or result.stdout
            or message("tui.update.git_failed")
        ).strip()
        raise UpdateError(detail[-500:])
    return result


def _revision(root: Path, reference: str) -> str:
    revision = _git(root, "rev-parse", "--verify", reference).stdout.strip().lower()
    if not SHA.fullmatch(revision):
        raise UpdateError(
            message("tui.update.invalid_revision", reference=reference)
        )
    return revision


def _branch(root: Path) -> str | None:
    result = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return result.stdout.strip() or None


def _dirty_tracked(root: Path) -> tuple[str, ...]:
    output = _git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
    ).stdout
    return tuple(line for line in output.splitlines() if line)


def _nul_paths(result: subprocess.CompletedProcess[str]) -> set[str]:
    return {value for value in result.stdout.split("\0") if value}


def _local_untracked_and_ignored(root: Path) -> set[str]:
    untracked = _nul_paths(
        _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    )
    ignored = _nul_paths(
        _git(root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    )
    return untracked | ignored


def _target_paths(root: Path, revision: str) -> set[str]:
    return _nul_paths(
        _git(root, "ls-tree", "-r", "--name-only", "-z", revision)
    )


def _path_conflicts(root: Path, revisions: set[str]) -> tuple[str, ...]:
    local = _local_untracked_and_ignored(root)
    targets: set[str] = set()
    for revision in revisions:
        targets |= _target_paths(root, revision)
    conflicts = {
        item
        for item in local
        if any(
            item == target
            or item.startswith(target.rstrip("/") + "/")
            or target.startswith(item.rstrip("/") + "/")
            for target in targets
        )
    }
    return tuple(sorted(conflicts))


def _relation(root: Path, local: str, remote: str) -> Relation:
    if local == remote:
        return "up_to_date"
    local_is_ancestor = _git(
        root, "merge-base", "--is-ancestor", local, remote, check=False
    ).returncode
    remote_is_ancestor = _git(
        root, "merge-base", "--is-ancestor", remote, local, check=False
    ).returncode
    if local_is_ancestor == 0:
        return "behind"
    if remote_is_ancestor == 0:
        return "ahead"
    return "diverged"


def _future_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return datetime.now(UTC) < datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False


def inspect_update(project_root: Path, *, manual: bool = False) -> UpdateStatus:
    """Fetch and classify the canonical fork without modifying checked-out files."""

    root = project_root.resolve()
    if not (root / ".git").exists():
        return UpdateStatus(
            root,
            "non_git",
            error=message("tui.update.git_clone_required"),
            manual=manual,
        )
    try:
        origin = _git(root, "remote", "get-url", "origin").stdout.strip()
        if origin != CANONICAL_ORIGIN:
            raise UpdateError(message("tui.update.origin_mismatch"))
        head = _revision(root, "HEAD")
        branch = _branch(root)
        main_result = _git(
            root,
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/main",
            check=False,
        )
        if main_result.returncode != 0:
            return UpdateStatus(
                root,
                "missing_main",
                head_sha=head,
                branch=branch,
                error=message("tui.update.main_missing"),
                manual=manual,
            )
        _git(
            root,
            "fetch",
            "--quiet",
            "--prune",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        )
        main = _revision(root, "refs/heads/main")
        target = _revision(root, "refs/remotes/origin/main")
        relation = _relation(root, main, target)
        commit_message = _git(root, "log", "-1", "--format=%s", target).stdout.strip()
        rollback = Installation.get("previous_update_sha")
        if not isinstance(rollback, str) or not SHA.fullmatch(rollback):
            rollback = None
        elif (
            _git(root, "cat-file", "-e", f"{rollback}^{{commit}}", check=False).returncode
            != 0
        ):
            rollback = None

        suppressed: str | None = None
        if not manual and relation == "behind":
            if Installation.get("ignored_update_sha") == target:
                suppressed = "ignored"
            elif _future_timestamp(Installation.get("update_remind_after")):
                suppressed = "reminded"
        return UpdateStatus(
            root,
            relation,
            head_sha=head,
            main_sha=main,
            target_sha=target,
            branch=branch,
            commit_message=commit_message,
            rollback_sha=rollback,
            suppressed_reason=suppressed,
            manual=manual,
            dirty_tracked=_dirty_tracked(root),
        )
    except UpdateError as exc:
        return UpdateStatus(root, "error", error=str(exc), manual=manual)


def _show_conflicts(console: Console, conflicts: tuple[str, ...]) -> None:
    console.print(
        Panel(
            "\n".join(escape(path) for path in conflicts),
            title=message("tui.update.conflicts_title"),
            border_style="red",
        )
    )
    console.print(message("tui.update.conflicts_notice"))


def _confirm_target(console: Console, status: UpdateStatus, target: str) -> bool:
    dirty = _dirty_tracked(status.root)
    revisions = {target}
    if status.head_sha and dirty:
        revisions.add(status.head_sha)
    conflicts = _path_conflicts(status.root, revisions)
    if conflicts:
        _show_conflicts(console, conflicts)
        return False
    if status.branch != "main":
        label = escape(status.branch or message("tui.update.detached"))
        if not Confirm.ask(
            message("tui.update.switch_main", branch=label),
            default=False,
        ):
            return False
        status.switch_confirmed = True
    else:
        status.switch_confirmed = True
    if dirty:
        console.print(
            Panel(
                "\n".join(escape(line) for line in dirty),
                title=message("tui.update.dirty_title"),
                border_style="yellow",
            )
        )
        if not Confirm.ask(
            message("tui.update.discard"),
            default=False,
        ):
            return False
    status.selected_target = target
    status.confirmed_dirty = dirty
    status.discard_confirmed = True
    return True


def choose_update(console: Console, status: UpdateStatus) -> UpdateAction:
    """Explain update state and collect all destructive-operation confirmations."""

    if status.error:
        if status.manual:
            console.print(
                message("tui.update.check_skipped", error=escape(status.error))
            )
        return "skip"
    if status.suppressed_reason:
        return "skip"
    if status.relation in {"ahead", "diverged", "missing_main"}:
        if status.manual:
            console.print(message("tui.update.diverged"))
        return "skip"
    if status.relation == "up_to_date":
        if status.manual:
            console.print(
                message("tui.update.current", revision=status.main_sha[:7])
            )
        if status.manual and status.rollback_sha and status.rollback_sha != status.main_sha:
            choice = Prompt.ask(
                message("tui.update.choose_action"),
                choices=["rollback", "back"],
                default="back",
            )
            if choice == "rollback" and _confirm_target(console, status, status.rollback_sha):
                return "rollback"
        return "skip"
    if status.relation != "behind" or not status.target_sha:
        return "skip"

    console.print(
        Panel(
            message(
                "tui.update.available",
                local=status.main_sha[:7],
                available=status.target_sha[:7],
                summary=escape(
                    status.commit_message or message("tui.update.no_summary")
                ),
            ),
            border_style="blue",
        )
    )
    choices = ["install", "3d", "ignore", "skip"]
    if (
        status.rollback_sha
        and status.rollback_sha not in {status.main_sha, status.target_sha}
    ):
        choices.insert(1, "rollback")
    choice = Prompt.ask(
        message("tui.update.action"), choices=choices, default="install"
    )
    if choice == "3d":
        Installation.set(
            "update_remind_after", (datetime.now(UTC) + timedelta(days=3)).isoformat()
        )
        return "skip"
    if choice == "ignore":
        Installation.set("ignored_update_sha", status.target_sha)
        return "skip"
    if choice == "rollback" and status.rollback_sha:
        return (
            "rollback"
            if _confirm_target(console, status, status.rollback_sha)
            else "skip"
        )
    if choice == "install":
        return "install" if _confirm_target(console, status, status.target_sha) else "skip"
    return "skip"


def _stage_revision(root: Path, revision: str) -> None:
    temporary = Path(tempfile.mkdtemp(prefix="playify-update-"))
    checkout = temporary / "checkout"
    added = False
    try:
        _git(root, "worktree", "add", "--detach", str(checkout), revision, timeout=60)
        added = True
        required = (
            checkout / "bootstrap.py",
            checkout / "playify.py",
            checkout / "requirements.txt",
            checkout / "src" / "playify",
            checkout / "src" / "tui",
        )
        if not all(path.exists() for path in required):
            raise UpdateError(message("tui.update.stage_missing"))
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "compileall",
                "-q",
                "bootstrap.py",
                "playify.py",
                "src/playify",
                "src/tui",
            ],
            cwd=checkout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if result.returncode != 0:
            detail = (
                result.stderr
                or result.stdout
                or message("tui.update.compile_failed")
            ).strip()
            raise UpdateError(
                message("tui.update.stage_compile_failed", detail=detail[-500:])
            )
    except subprocess.TimeoutExpired as exc:
        raise UpdateError(message("tui.update.stage_timeout")) from exc
    finally:
        if added:
            try:
                _git(
                    root,
                    "worktree",
                    "remove",
                    "--force",
                    str(checkout),
                    timeout=60,
                    check=False,
                )
            except UpdateError:
                pass
        shutil.rmtree(temporary, ignore_errors=True)


def _revalidate(status: UpdateStatus, target: str, *, origin_target: bool) -> None:
    if status.selected_target != target or not status.discard_confirmed:
        raise UpdateError(message("tui.update.not_confirmed"))
    if _branch(status.root) != status.branch:
        raise UpdateError(message("tui.update.branch_changed"))
    if _revision(status.root, "HEAD") != status.head_sha:
        raise UpdateError(message("tui.update.head_changed"))
    if _dirty_tracked(status.root) != status.confirmed_dirty:
        raise UpdateError(message("tui.update.files_changed"))
    if origin_target and _revision(status.root, "refs/remotes/origin/main") != target:
        raise UpdateError(message("tui.update.origin_changed"))
    revisions = {target}
    if status.head_sha and status.confirmed_dirty:
        revisions.add(status.head_sha)
    conflicts = _path_conflicts(status.root, revisions)
    if conflicts:
        raise UpdateError(
            message("tui.update.new_conflict", paths=", ".join(conflicts))
        )


def _prepare_main(status: UpdateStatus) -> None:
    if status.branch != "main":
        if not status.switch_confirmed:
            raise UpdateError(message("tui.update.switch_unconfirmed"))
        if status.confirmed_dirty:
            _git(status.root, "reset", "--hard", "HEAD", timeout=60)
        _git(status.root, "switch", "main", timeout=60)


def install_update(project_root: Path, status: UpdateStatus) -> tuple[bool, str]:
    """Validate a fetched release and make local main exactly match origin/main."""

    del project_root  # The inspected, resolved root is authoritative.
    target = status.target_sha
    if not target:
        return False, message("tui.update.no_target")
    try:
        _revalidate(status, target, origin_target=True)
        _stage_revision(status.root, target)
        Installation.update(
            {
                "previous_update_sha": status.main_sha,
                "update_remind_after": None,
            }
        )
        _prepare_main(status)
        if conflicts := _path_conflicts(status.root, {target}):
            raise UpdateError(
                message("tui.update.preserve_path", paths=", ".join(conflicts))
            )
        _git(status.root, "reset", "--hard", "origin/main", timeout=60)
        if _revision(status.root, "HEAD") != target:
            raise UpdateError(message("tui.update.target_failed"))
        Installation.update(
            {
                "last_update_sha": target,
                "ignored_update_sha": None,
                "update_remind_after": None,
            }
        )
        return True, target[:7]
    except (OSError, UpdateError) as exc:
        return False, str(exc)


def rollback_update(project_root: Path, status: UpdateStatus) -> tuple[bool, str]:
    """Restore the previously installed code revision without touching runtime data."""

    del project_root
    target = status.rollback_sha
    if not target:
        return False, message("tui.update.no_rollback")
    try:
        _revalidate(status, target, origin_target=False)
        _stage_revision(status.root, target)
        current_main = status.main_sha
        _prepare_main(status)
        if conflicts := _path_conflicts(status.root, {target}):
            raise UpdateError(
                message(
                    "tui.update.rollback_preserve_path",
                    paths=", ".join(conflicts),
                )
            )
        _git(status.root, "reset", "--hard", target, timeout=60)
        if _revision(status.root, "HEAD") != target:
            raise UpdateError(message("tui.update.rollback_failed"))
        Installation.update(
            {
                "previous_update_sha": current_main,
                "last_update_sha": target,
                "ignored_update_sha": status.target_sha,
                "update_remind_after": None,
            }
        )
        return True, target[:7]
    except (OSError, UpdateError) as exc:
        return False, str(exc)
