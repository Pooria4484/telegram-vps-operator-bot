from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


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
