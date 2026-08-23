from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory_executor.executor import ExecutionError, WorkerExecutor


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=path, text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _task(path: Path, allowed_paths: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        id="commit-test",
        worktree=str(path),
        contract={"read_only": False, "allowed_paths": allowed_paths, "title": "Commit verified task changes"},
    )


def _executor_without_docker() -> WorkerExecutor:
    # The commit operation is intentionally local Git only; its unit test must
    # not require the Docker-socket-owning broker constructor.
    return object.__new__(WorkerExecutor)


def test_executor_commits_only_declared_worker_paths(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("BASE = True\n", encoding="utf-8")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "src" / "app.py").write_text("BASE = False\n", encoding="utf-8")
    (tmp_path / ".factory").mkdir()
    (tmp_path / ".factory" / "result.json").write_text("{}", encoding="utf-8")

    commit = _executor_without_docker().commit_task_changes(_task(tmp_path, ["src/"]))

    assert commit != base
    assert _git(tmp_path, "show", "--format=%s", "--no-patch", "HEAD") == "factory(commit-test): Commit verified task changes"
    assert _git(tmp_path, "status", "--porcelain") == "?? .factory/"


def test_executor_rejects_worker_changes_outside_declared_paths(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("BASE = True\n", encoding="utf-8")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "base")
    (tmp_path / "outside.txt").write_text("not allowed\n", encoding="utf-8")

    with pytest.raises(ExecutionError, match="outside allowed_paths"):
        _executor_without_docker().commit_task_changes(_task(tmp_path, ["src/"]))
