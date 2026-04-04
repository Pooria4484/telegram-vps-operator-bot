from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
import pwd
import pty
import re
import signal
import subprocess


@dataclass(slots=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(slots=True)
class LiveCommand:
    process: asyncio.subprocess.Process
    pty_master_fd: int


def _login_identity() -> tuple[str, str, str]:
    try:
        pw = pwd.getpwuid(os.getuid())
        username = pw.pw_name
        home = pw.pw_dir
        login_shell = pw.pw_shell or "/bin/bash"
        return username, home, login_shell
    except Exception:
        return (
            os.environ.get("USER", "unknown"),
            os.environ.get("HOME", str(Path.home())),
            os.environ.get("SHELL", "/bin/bash"),
        )


def _sanitize_path(raw_path: str) -> str:
    cleaned_parts: list[str] = []
    seen: set[str] = set()
    for part in raw_path.split(os.pathsep):
        item = part.strip()
        if not item:
            continue
        if "/.codex/tmp/arg0/" in item:
            continue
        if "/node_modules/@openai/codex-linux-" in item:
            continue
        if "/@openai/codex/" in item and "/vendor/" in item:
            continue
        if item in seen:
            continue
        seen.add(item)
        cleaned_parts.append(item)
    return os.pathsep.join(cleaned_parts)


@lru_cache(maxsize=1)
def _detect_login_path() -> str:
    username, home, login_shell = _login_identity()
    env = os.environ.copy()
    env["HOME"] = home
    env["USER"] = username
    env["LOGNAME"] = username
    env["SHELL"] = login_shell
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")

    marker_begin = "__TG_VPS_BOT_PATH_BEGIN__"
    marker_end = "__TG_VPS_BOT_PATH_END__"
    probe = f'printf "{marker_begin}%s{marker_end}" "$PATH"'
    try:
        completed = subprocess.run(
            [login_shell, "-ilc", probe],
            capture_output=True,
            text=True,
            timeout=8,
            env=env,
        )
        merged = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        match = re.search(
            rf"{re.escape(marker_begin)}(.*?){re.escape(marker_end)}",
            merged,
            flags=re.DOTALL,
        )
        if match:
            detected = match.group(1).strip()
            if detected:
                return _sanitize_path(detected)
    except Exception:
        pass

    return _sanitize_path(
        env.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    )


def build_command_env() -> dict[str, str]:
    username, home, login_shell = _login_identity()
    env = os.environ.copy()
    env["HOME"] = home
    env["USER"] = username
    env["LOGNAME"] = username
    env["SHELL"] = login_shell
    env.setdefault("TERM", "xterm-256color")
    env["PATH"] = _detect_login_path()
    return env


def _run_command_sync(
    command: str,
    shell: str,
    cwd: Path,
    timeout_seconds: float | None = None,
) -> CommandResult:
    env = build_command_env()
    completed = subprocess.run(
        [shell, "-lc", command],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout_seconds,
    )
    return CommandResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


async def run_command(
    command: str,
    shell: str,
    cwd: Path,
    timeout_seconds: float | None = None,
) -> CommandResult:
    return await asyncio.to_thread(
        _run_command_sync,
        command,
        shell,
        cwd,
        timeout_seconds,
    )


async def start_live_command(command: str, shell: str, cwd: Path) -> LiveCommand:
    master_fd, slave_fd = pty.openpty()
    env = build_command_env()
    try:
        process = await asyncio.create_subprocess_exec(
            shell,
            "-lc",
            command,
            cwd=str(cwd),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            env=env,
        )
    finally:
        os.close(slave_fd)

    return LiveCommand(process=process, pty_master_fd=master_fd)


async def stop_live_command(
    process: asyncio.subprocess.Process,
    timeout_seconds: float = 3.0,
) -> None:
    if process.returncode is not None:
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        return
    except asyncio.TimeoutError:
        pass

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return

    await process.wait()


def send_ctrl_c(process: asyncio.subprocess.Process) -> bool:
    if process.returncode is not None:
        return False

    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        return False

    return True


def send_pty_input(master_fd: int, data: bytes) -> bool:
    if not data:
        return True

    view = memoryview(data)
    try:
        while view:
            written = os.write(master_fd, view)
            if written <= 0:
                return False
            view = view[written:]
    except OSError:
        return False

    return True
