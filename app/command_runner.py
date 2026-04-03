from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import pty
import signal


@dataclass(slots=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(slots=True)
class LiveCommand:
    process: asyncio.subprocess.Process
    pty_master_fd: int


async def run_command(command: str, shell: str, cwd: Path) -> CommandResult:
    process = await asyncio.create_subprocess_exec(
        shell,
        "-lc",
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    return CommandResult(
        exit_code=process.returncode,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
    )


async def start_live_command(command: str, shell: str, cwd: Path) -> LiveCommand:
    master_fd, slave_fd = pty.openpty()
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
