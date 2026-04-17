from __future__ import annotations

import tempfile
import subprocess
import unittest
from datetime import datetime
from pathlib import Path

from app.bot import export_patch_for_run
from app.models import CodexRun


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def _make_run(workspace: Path, changed_files_json: str) -> CodexRun:
    run = CodexRun(
        codex_run_id="cxr_patch_test",
        codex_session_id="cxs_patch_test",
        telegram_chat_id=1,
        telegram_user_id=1,
        workspace_path=workspace,
        prompt_text="patch test",
        run_mode="continue",
        model_name="gpt-5.4",
        reasoning_effort="medium",
        started_at=datetime.utcnow(),
    )
    run.structured_result_json = changed_files_json
    run.stdout_artifact_path = workspace / "codex_artifacts" / "stdout.txt"
    run.stdout_artifact_path.parent.mkdir(parents=True, exist_ok=True)
    run.stdout_artifact_path.write_text("", encoding="utf-8")
    return run


class PatchExportTests(unittest.TestCase):
    def test_untracked_absolute_path_exports_relative_patch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "repo"
            ws.mkdir(parents=True)
            _git(ws, "init")
            _git(ws, "config", "user.email", "bot@example.com")
            _git(ws, "config", "user.name", "bot")

            (ws / "base.txt").write_text("ok\n", encoding="utf-8")
            _git(ws, "add", "base.txt")
            _git(ws, "commit", "-m", "init")

            (ws / "sub").mkdir()
            (ws / "sub" / "new.py").write_text("print(2)\n", encoding="utf-8")

            changed_json = (
                '{"changed_files":[{"path":"'
                + (ws / "sub" / "new.py").as_posix()
                + '","change_type":"created","summary":""}]}'
            )
            run = _make_run(ws, changed_json)
            patch_path, err = export_patch_for_run(run)
            self.assertTrue(patch_path is not None, msg=err)
            assert patch_path is not None
            text = patch_path.read_text(encoding="utf-8")

            self.assertIn("diff --git a/sub/new.py b/sub/new.py", text)
            self.assertNotIn(ws.as_posix(), text)

    def test_deleted_tracked_file_is_in_patch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "repo"
            ws.mkdir(parents=True)
            _git(ws, "init")
            _git(ws, "config", "user.email", "bot@example.com")
            _git(ws, "config", "user.name", "bot")

            (ws / "old.py").write_text("print('old')\n", encoding="utf-8")
            _git(ws, "add", "old.py")
            _git(ws, "commit", "-m", "init")

            (ws / "old.py").unlink()

            changed_json = (
                '{"changed_files":[{"path":"'
                + (ws / "old.py").as_posix()
                + '","change_type":"deleted","summary":""}]}'
            )
            run = _make_run(ws, changed_json)
            patch_path, err = export_patch_for_run(run)
            self.assertTrue(patch_path is not None, msg=err)
            assert patch_path is not None
            text = patch_path.read_text(encoding="utf-8")

            self.assertIn("diff --git a/old.py b/old.py", text)
            self.assertIn("--- a/old.py", text)
            self.assertIn("+++ /dev/null", text)

    def test_empty_changed_files_falls_back_to_git_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "repo"
            ws.mkdir(parents=True)
            _git(ws, "init")
            _git(ws, "config", "user.email", "bot@example.com")
            _git(ws, "config", "user.name", "bot")

            (ws / "main.py").write_text("print(1)\n", encoding="utf-8")
            _git(ws, "add", "main.py")
            _git(ws, "commit", "-m", "init")

            # Tracked modification + untracked file, with no structured changed_files.
            (ws / "main.py").write_text("print(2)\n", encoding="utf-8")
            (ws / "extra.txt").write_text("x\n", encoding="utf-8")

            run = _make_run(ws, '{"changed_files":[]}')
            patch_path, err = export_patch_for_run(run)
            self.assertTrue(patch_path is not None, msg=err)
            assert patch_path is not None
            text = patch_path.read_text(encoding="utf-8")

            self.assertIn("diff --git a/main.py b/main.py", text)
            self.assertIn("diff --git a/extra.txt b/extra.txt", text)

    def test_run_scope_requires_changed_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "repo"
            ws.mkdir(parents=True)
            _git(ws, "init")
            _git(ws, "config", "user.email", "bot@example.com")
            _git(ws, "config", "user.name", "bot")
            (ws / "main.py").write_text("print(1)\n", encoding="utf-8")
            _git(ws, "add", "main.py")
            _git(ws, "commit", "-m", "init")
            (ws / "main.py").write_text("print(2)\n", encoding="utf-8")

            run = _make_run(ws, '{"changed_files":[]}')
            patch_path, err = export_patch_for_run(run, scope="run_files")
            self.assertIsNone(patch_path)
            self.assertIn("No run-scoped changed files", err)

    def test_all_changes_scope_ignores_empty_changed_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "repo"
            ws.mkdir(parents=True)
            _git(ws, "init")
            _git(ws, "config", "user.email", "bot@example.com")
            _git(ws, "config", "user.name", "bot")
            (ws / "main.py").write_text("print(1)\n", encoding="utf-8")
            _git(ws, "add", "main.py")
            _git(ws, "commit", "-m", "init")
            (ws / "main.py").write_text("print(2)\n", encoding="utf-8")

            run = _make_run(ws, '{"changed_files":[]}')
            patch_path, err = export_patch_for_run(run, scope="all_changes")
            self.assertTrue(patch_path is not None, msg=err)


if __name__ == "__main__":
    unittest.main()
