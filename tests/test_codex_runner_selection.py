from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.codex_runner import CodexRunArtifacts, CodexRunExecutionResult, CodexRunner
from app.config import Settings
from app.session_manager import SessionManager


class CapturingRunner(CodexRunner):
    def __init__(self, settings: Settings, session_manager: SessionManager) -> None:
        super().__init__(settings, session_manager)
        self.captured_model: str | None = None
        self.captured_effort: str | None = None

    async def _execute_run(  # type: ignore[override]
        self,
        run,
        model_name: str,
        reasoning_effort: str,
        status_callback=None,
    ) -> CodexRunExecutionResult:
        self.captured_model = model_name
        self.captured_effort = reasoning_effort
        run.status = "success"
        run.model_name = model_name
        run.reasoning_effort = reasoning_effort
        self.session_manager.save_codex_run(run)

        artifacts = CodexRunArtifacts(
            run_dir=self.artifacts_root,
            prompt_path=self.artifacts_root / "prompt.txt",
            schema_path=self.artifacts_root / "schema.json",
            stdout_path=self.artifacts_root / "stdout.txt",
            stderr_path=self.artifacts_root / "stderr.txt",
            output_last_message_path=self.artifacts_root / "last_message.json",
            events_path=self.artifacts_root / "events.jsonl",
        )
        return CodexRunExecutionResult(
            run=run,
            exit_code=0,
            stdout="",
            stderr="",
            parsed_result=None,
            artifacts=artifacts,
        )


def make_settings(root: Path) -> Settings:
    return Settings(
        bot_token="x",
        allowed_user_ids={1},
        default_shell="/bin/bash",
        workdir=root,
        max_tail_lines=30,
        max_upload_bytes=20 * 1024 * 1024,
        max_running_sessions_per_user=3,
        max_session_history_per_user=20,
        sessions_page_size=5,
        detached_session_ttl_seconds=3600,
        detached_sweep_interval_seconds=30,
        time_offset_minutes=0,
        log_level="INFO",
        session_db_path=root / "session_store.sqlite3",
        codex_command="codex",
        codex_timeout_seconds=120,
        codex_artifacts_dir=root / "codex_artifacts",
        codex_model="gpt-5.4",
        codex_available_models=["gpt-5.4", "gpt-5.4-mini"],
        codex_reasoning_effort="medium",
        codex_available_reasoning_efforts=["low", "medium", "high", "xhigh"],
        codex_sandbox_mode="workspace-write",
    )


class CodexRunnerSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_prompt_applies_model_and_effort_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            session_manager = SessionManager(root, settings.session_db_path, settings.time_offset_minutes)
            runner = CapturingRunner(settings, session_manager)

            result = await runner.run_prompt(
                telegram_chat_id=1001,
                telegram_user_id=2002,
                prompt_text="hello",
                run_mode="continue",
                model_override="gpt-5.4-mini",
                reasoning_effort_override="high",
                explicit_workspace=root,
            )

            self.assertEqual(runner.captured_model, "gpt-5.4-mini")
            self.assertEqual(runner.captured_effort, "high")
            self.assertEqual(result.run.model_name, "gpt-5.4-mini")
            self.assertEqual(result.run.reasoning_effort, "high")

    async def test_run_prompt_falls_back_to_active_session_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            session_manager = SessionManager(root, settings.session_db_path, settings.time_offset_minutes)
            runner = CapturingRunner(settings, session_manager)

            session = session_manager.create_codex_session(
                telegram_chat_id=1001,
                telegram_user_id=2002,
                workspace_path=root,
                default_mode="continue",
                default_model="gpt-5.4-mini",
                default_reasoning_effort="xhigh",
            )

            result = await runner.run_prompt(
                telegram_chat_id=1001,
                telegram_user_id=2002,
                prompt_text="hello again",
                run_mode="continue",
                explicit_workspace=root,
            )

            self.assertEqual(runner.captured_model, "gpt-5.4-mini")
            self.assertEqual(runner.captured_effort, "xhigh")
            self.assertEqual(result.run.model_name, "gpt-5.4-mini")
            self.assertEqual(result.run.reasoning_effort, "xhigh")

            refreshed = session_manager.get_codex_session(session.codex_session_id)
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertEqual(refreshed.default_model, "gpt-5.4-mini")
            self.assertEqual(refreshed.default_reasoning_effort, "xhigh")


if __name__ == "__main__":
    unittest.main()
