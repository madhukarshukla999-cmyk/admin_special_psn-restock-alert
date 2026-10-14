"""Auto-update: fast-forward the git checkout to the latest code and reinstall packages if needed.

Only fast-forwards. If you have local commits or edited a file the update touches, nothing is
changed and the admins get a message explaining why.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW: no console pop-ups from pythonw


@dataclass
class Result:
    status: str          # updated | up-to-date | skipped | error
    message: str = ""
    changes: str = ""


def _git(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=_NO_WINDOW)


def current_version(repo: Path) -> str:
    try:
        r = _git(repo, "log", "-1", "--format=%h %cd", "--date=format:%d %b %Y")
        return r.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def check_and_update(repo: Path) -> Result:
    if not (repo / ".git").exists():
        return Result("skipped", "this folder isn't a git clone (downloaded as ZIP?)")
    if not os.access(repo / ".git", os.W_OK):
        return Result("skipped", "the code folder is read-only")
    try:
        f = _git(repo, "fetch", "--quiet", "origin")
        if f.returncode != 0:
            return Result("error", "git fetch failed: " + (f.stderr.strip() or f.stdout.strip())[:300])
        local = _git(repo, "rev-parse", "HEAD").stdout.strip()
        up = _git(repo, "rev-parse", "@{u}")
        if up.returncode != 0:
            return Result("skipped", "the current branch doesn't track a GitHub branch")
        remote = up.stdout.strip()
        if local == remote:
            return Result("up-to-date")
        if _git(repo, "merge-base", "--is-ancestor", "HEAD", "@{u}").returncode != 0:
            return Result("skipped", "this copy has its own commits; update it by hand with git pull")
        changes = _git(repo, "log", "--format=• %s", "HEAD..@{u}").stdout.strip()
        changed_files = _git(repo, "diff", "--name-only", "HEAD", "@{u}").stdout.split()
        m = _git(repo, "merge", "--ff-only", "--quiet", "@{u}")
        if m.returncode != 0:
            return Result("error", "couldn't apply the update (did you edit a file it changes, "
                                   "e.g. config.yaml?):\n" + (m.stderr.strip() or m.stdout.strip())[:300])
        if "requirements.txt" in changed_files:
            p = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
                               cwd=repo, capture_output=True, text=True, timeout=600, creationflags=_NO_WINDOW)
            if p.returncode != 0:
                return Result("error", "code updated but installing packages failed:\n" + p.stderr[-300:], changes)
        return Result("updated", changes=changes)
    except (OSError, subprocess.SubprocessError) as e:
        return Result("error", f"{e!r} (is Git installed?)")


def restart_mode(task_name: str | None) -> str:
    if os.environ.get("INVOCATION_ID"):          # systemd: Restart=always brings us back
        return "systemd"
    if os.name == "nt" and task_name:
        return "task"
    return "manual"                               # started by hand: the user restarts it


def restart(task_name: str | None) -> str:
    """Start a fresh copy after this process exits. Returns how it was done."""
    how = restart_mode(task_name)
    if how == "task":
        # Re-run the Task Scheduler task a few seconds after we exit, so it stays under the task.
        subprocess.Popen(["cmd", "/c", f'ping -n 6 127.0.0.1 >nul & schtasks /Run /TN "{task_name}"'],
                         creationflags=_NO_WINDOW | 0x00000008, close_fds=True)   # DETACHED_PROCESS
    return how
