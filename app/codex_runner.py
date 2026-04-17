from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from app.command_runner import build_command_env
from app.config import Settings
from app.models import CodexRun, CodexSessionMode
from app.session_manager import SessionManager

logger = logging.getLogger(__name__)
StatusCallback = Callable[[str], Awaitable[None]]


STRUCTURED_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "summary_short",
        "summary_full",
        "what_changed",
        "changed_files",
        "checks",
        "result_for_user",
        "next_steps",
        "needs_user_input",
        "user_input_question",
        "debug",
    ],
    "properties": {
        "status": {"type": "string", "enum": ["success", "partial", "failed"]},
        "summary_short": {"type": "string"},
        "summary_full": {"type": "string"},
        "what_changed": {"type": "array", "items": {"type": "string"}},
        "changed_files": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "change_type", "summary"],
                "properties": {
                    "path": {"type": "string"},
                    "change_type": {"type": "string", "enum": ["created", "modified", "deleted"]},
                    "summary": {"type": "string"},
                },
            },
        },
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "status", "details"],
                "properties": {
                    "name": {"type": "string"},
                    "status": {"type": "string", "enum": ["passed", "failed", "skipped"]},
                    "details": {"type": "string"},
                },
            },
        },
        "result_for_user": {"type": "string"},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "needs_user_input": {"type": "boolean"},
        "user_input_question": {"type": "string"},
        "debug": {
            "type": "object",
            "additionalProperties": False,
            "required": ["commands_run", "notes"],
            "properties": {
                "commands_run": {"type": "array", "items": {"type": "string"}},
                "notes": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}


@dataclass(slots=True)
class CodexWorkspaceContext:
    workspace_path: Path
    source: str


@dataclass(slots=True)
class CodexRunArtifacts:
    run_dir: Path
    prompt_path: Path
    schema_path: Path
    stdout_path: Path
    stderr_path: Path
    output_last_message_path: Path
    events_path: Path


@dataclass(slots=True)
class CodexRunExecutionResult:
    run: CodexRun
    exit_code: int
    stdout: str
    stderr: str
    parsed_result: dict[str, object] | None
    artifacts: CodexRunArtifacts


class CodexRunConflictError(RuntimeError):
    pass


class CodexRunner:
    def __init__(self, settings: Settings, session_manager: SessionManager) -> None:
        self.settings = settings
        self.session_manager = session_manager
        self.artifacts_root = settings.codex_artifacts_dir.expanduser().resolve()
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        self._workspace_locks: dict[Path, asyncio.Lock] = {}
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._active_process_by_run_id: dict[str, asyncio.subprocess.Process] = {}

    def resolve_workspace(
        self,
        telegram_user_id: int,
        explicit_path: Path | None = None,
    ) -> CodexWorkspaceContext:
        if explicit_path is not None:
            workspace = explicit_path.expanduser().resolve()
            source = "explicit"
        else:
            workspace = self.session_manager.get_current_workdir(telegram_user_id).resolve()
            source = "current_workdir"

        if not workspace.exists():
            raise FileNotFoundError(f"workspace does not exist: {workspace}")
        if not workspace.is_dir():
            raise NotADirectoryError(f"workspace is not a directory: {workspace}")

        return CodexWorkspaceContext(workspace_path=workspace, source=source)

    async def run_prompt(
        self,
        *,
        telegram_chat_id: int,
        telegram_user_id: int,
        prompt_text: str,
        run_mode: CodexSessionMode = "continue",
        model_override: str | None = None,
        reasoning_effort_override: str | None = None,
        explicit_workspace: Path | None = None,
        status_callback: StatusCallback | None = None,
    ) -> CodexRunExecutionResult:
        if status_callback is not None:
            await status_callback("Queued")
            await status_callback("Understanding request")
        workspace_ctx = self.resolve_workspace(telegram_user_id, explicit_workspace)
        if status_callback is not None:
            await status_callback("Inspecting project")
        active_chat_run = self.session_manager.get_active_codex_run_for_chat(telegram_chat_id)
        if active_chat_run is not None:
            raise CodexRunConflictError(
                f"chat is already busy with run {active_chat_run.codex_run_id}"
            )
        active_run = self.session_manager.get_active_codex_run_for_workspace(workspace_ctx.workspace_path)
        if active_run is not None:
            raise CodexRunConflictError(
                f"workspace is already busy with run {active_run.codex_run_id}"
            )

        active_session = self.session_manager.get_active_codex_session_for_chat(telegram_chat_id)
        resolved_model = (model_override or "").strip()
        if not resolved_model and active_session is not None:
            resolved_model = (active_session.default_model or "").strip()
        if not resolved_model:
            resolved_model = (self.settings.codex_model or "").strip()
        resolved_effort = (reasoning_effort_override or "").strip().lower()
        if not resolved_effort and active_session is not None:
            resolved_effort = (active_session.default_reasoning_effort or "").strip().lower()
        if not resolved_effort:
            resolved_effort = (self.settings.codex_reasoning_effort or "").strip().lower()
        if run_mode == "new" or active_session is None:
            codex_session = self.session_manager.create_codex_session(
                telegram_chat_id=telegram_chat_id,
                telegram_user_id=telegram_user_id,
                workspace_path=workspace_ctx.workspace_path,
                default_mode=run_mode,
                default_model=resolved_model,
                default_reasoning_effort=resolved_effort,
            )
        else:
            codex_session = active_session
            codex_session.workspace_path = workspace_ctx.workspace_path
            codex_session.default_mode = run_mode
            codex_session.default_model = resolved_model
            codex_session.default_reasoning_effort = resolved_effort
            self.session_manager.save_codex_session(codex_session)

        run = self.session_manager.create_codex_run(
            codex_session_id=codex_session.codex_session_id,
            telegram_chat_id=telegram_chat_id,
            telegram_user_id=telegram_user_id,
            workspace_path=workspace_ctx.workspace_path,
            prompt_text=prompt_text,
            run_mode=run_mode,
            model_name=resolved_model,
            reasoning_effort=resolved_effort,
        )

        workspace_lock = self._workspace_locks.setdefault(workspace_ctx.workspace_path, asyncio.Lock())
        chat_lock = self._chat_locks.setdefault(telegram_chat_id, asyncio.Lock())

        async with chat_lock:
            async with workspace_lock:
                latest_active = self.session_manager.get_active_codex_run_for_workspace(
                    workspace_ctx.workspace_path
                )
                if latest_active is not None and latest_active.codex_run_id != run.codex_run_id:
                    raise CodexRunConflictError(
                        f"workspace became busy with run {latest_active.codex_run_id}"
                    )
                return await self._execute_run(
                    run,
                    model_name=resolved_model,
                    reasoning_effort=resolved_effort,
                    status_callback=status_callback,
                )

    async def cancel_run(self, codex_run_id: str) -> bool:
        process = self._active_process_by_run_id.get(codex_run_id)
        if process is None or process.returncode is not None:
            return False
        process.terminate()
        return True

    def _create_artifacts(self, run: CodexRun) -> CodexRunArtifacts:
        run_dir = self.artifacts_root / run.codex_run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return CodexRunArtifacts(
            run_dir=run_dir,
            prompt_path=run_dir / "prompt.txt",
            schema_path=run_dir / "output_schema.json",
            stdout_path=run_dir / "stdout.txt",
            stderr_path=run_dir / "stderr.txt",
            output_last_message_path=run_dir / "last_message.json",
            events_path=run_dir / "events.jsonl",
        )

    def _build_prompt(self, user_prompt: str, workspace_path: Path) -> str:
        return (
            "You are running non-interactively for a Telegram VPS bot.\n"
            "Inspect the repository before editing. Make minimal, high-confidence changes.\n"
            "Run relevant verification when reasonable.\n"
            "Return the final answer only in the required structured schema.\n"
            "Do not roleplay a terminal. Do not emit noisy progress chatter in the final answer.\n"
            "Do not include raw logs or stderr in the final structured response.\n"
            f"Workspace: {workspace_path}\n\n"
            "User request:\n"
            f"{user_prompt.strip()}\n"
        )

    def _build_exec_command(
        self,
        *,
        schema_path: Path,
        output_last_message_path: Path,
        workspace_path: Path,
        model_name: str,
        reasoning_effort: str,
    ) -> list[str]:
        command = [
            self.settings.codex_command,
            "exec",
            "-C",
            str(workspace_path),
            "--skip-git-repo-check",
            "--sandbox",
            self.settings.codex_sandbox_mode,
            "--json",
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_last_message_path),
            "-",
        ]
        if model_name:
            command.extend(["-m", model_name])
        if reasoning_effort:
            command.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        return command

    async def _execute_run(
        self,
        run: CodexRun,
        model_name: str,
        reasoning_effort: str,
        status_callback: StatusCallback | None = None,
    ) -> CodexRunExecutionResult:
        artifacts = self._create_artifacts(run)
        prompt_text = self._build_prompt(run.prompt_text, run.workspace_path)
        artifacts.prompt_path.write_text(prompt_text, encoding="utf-8")
        artifacts.schema_path.write_text(
            json.dumps(STRUCTURED_OUTPUT_SCHEMA, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

        env = build_command_env()
        run.status = "running"
        self.session_manager.save_codex_run(run)
        if status_callback is not None:
            await status_callback("Applying changes")

        command = self._build_exec_command(
            schema_path=artifacts.schema_path,
            output_last_message_path=artifacts.output_last_message_path,
            workspace_path=run.workspace_path,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
        )
        logger.info(
            "Starting codex run: run_id=%s chat_id=%s user_id=%s workspace=%s mode=%s",
            run.codex_run_id,
            run.telegram_chat_id,
            run.telegram_user_id,
            run.workspace_path,
            run.run_mode,
        )
        if status_callback is not None:
            await status_callback("Connecting")
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(run.workspace_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._active_process_by_run_id[run.codex_run_id] = process

        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(
                process.communicate(prompt_text.encode("utf-8")),
                timeout=self.settings.codex_timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            stdout_raw = b""
            stderr_raw = b"Codex run timed out."
            run.status = "failed"
            run.failure_summary = "timed out"
            run.exit_code = None
            run.ended_at = self.session_manager.now()
            artifacts.stdout_path.write_text("", encoding="utf-8")
            artifacts.stderr_path.write_text("Codex run timed out.", encoding="utf-8")
            self.session_manager.save_codex_run(run)
            raise
        finally:
            self._active_process_by_run_id.pop(run.codex_run_id, None)

        stdout_text = stdout_raw.decode("utf-8", errors="replace")
        stderr_text = stderr_raw.decode("utf-8", errors="replace")
        if status_callback is not None:
            await status_callback("Running checks")
        artifacts.stdout_path.write_text(stdout_text, encoding="utf-8")
        artifacts.stderr_path.write_text(stderr_text, encoding="utf-8")
        artifacts.events_path.write_text(stdout_text, encoding="utf-8")

        run.stdout_artifact_path = artifacts.stdout_path
        run.stderr_artifact_path = artifacts.stderr_path
        run.exit_code = process.returncode
        run.ended_at = self.session_manager.now()

        parsed_result = self._load_structured_result(artifacts.output_last_message_path)
        if status_callback is not None:
            await status_callback("Finalizing response")
        if parsed_result is not None:
            run.structured_result_json = json.dumps(parsed_result, ensure_ascii=True)
            run.summary_short = str(parsed_result.get("summary_short") or "")
            status_value = str(parsed_result.get("status") or "")
            if status_value in {"success", "partial", "failed"}:
                run.status = status_value  # type: ignore[assignment]
            elif process.returncode == 0:
                run.status = "success"
            else:
                run.status = "failed"
        else:
            run.status = "success" if process.returncode == 0 else "failed"
            run.failure_summary = stderr_text.strip()[:500]

        self.session_manager.save_codex_run(run)
        return CodexRunExecutionResult(
            run=run,
            exit_code=process.returncode or 0,
            stdout=stdout_text,
            stderr=stderr_text,
            parsed_result=parsed_result,
            artifacts=artifacts,
        )

    def _load_structured_result(self, result_path: Path) -> dict[str, object] | None:
        if not result_path.exists():
            return None
        try:
            raw = result_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed
