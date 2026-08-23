from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory_executor.contracts import TaskStatus
from factory_executor.contracts import WorkerResult
from factory_executor.executor import ExecutionError, WorkerExecutor


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args], cwd=path, text=True, capture_output=True, check=False
    )
    if check:
        assert completed.returncode == 0, completed.stderr
    return completed


def _repository(root: Path) -> Path:
    repository = root / "repositories" / "demo"
    repository.mkdir(parents=True)
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.invalid")
    (repository / "shared.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base")
    return repository


def _dependency_commit(repository: Path, branch: str, files: dict[str, str]) -> str:
    _git(repository, "checkout", "-b", branch, "main")
    for name, content in files.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", branch)
    commit = _git(repository, "rev-parse", "HEAD").stdout.strip()
    _git(repository, "checkout", "main")
    return commit


class _Store:
    def __init__(self, dependencies: dict[str, SimpleNamespace]) -> None:
        self.run = SimpleNamespace(id="run-12345678", repository="demo", base_ref="main")
        self.dependencies = dependencies
        self.events: list[dict] = []
        self.transitions: list[tuple[str, TaskStatus, dict]] = []

    def get_run(self, run_id: str):
        return self.run if run_id == self.run.id else None

    def get_task(self, task_id: str):
        return self.dependencies.get(task_id)

    def record_event(self, run_id: str, event: str, detail: dict, task_id: str | None = None):
        self.events.append(
            {"run_id": run_id, "event": event, "detail": detail, "task_id": task_id}
        )

    def events_for_run(self, run_id: str):
        return [
            SimpleNamespace(event=item["event"], detail=item["detail"])
            for item in self.events
            if item["run_id"] == run_id
        ]

    def transition_task(self, task_id: str, status: TaskStatus, **values):
        self.transitions.append((task_id, status, values))


def _dependency(task_id: str, commit: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        run_id="run-12345678",
        status=TaskStatus.SUCCEEDED,
        result={"commit": commit},
    )


def _task(dependencies: list[str], task_id: str = "combined-task") -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        run_id="run-12345678",
        contract={"dependencies": dependencies},
        worktree=None,
        branch=None,
        container_id=None,
    )


def _executor(tmp_path: Path, store: _Store) -> WorkerExecutor:
    executor = object.__new__(WorkerExecutor)
    executor.store = store
    executor.projects_root = (tmp_path / "repositories").resolve()
    executor.workspaces_root = (tmp_path / "workspaces").resolve()
    executor.workspaces_root.mkdir()
    return executor


def test_prepare_inherits_one_dependency_commit(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    commit = _dependency_commit(repository, "foundation", {"foundation.txt": "ready\n"})
    store = _Store({"foundation": _dependency("foundation", commit)})

    workspace, _, evidence = _executor(tmp_path, store).prepare(_task(["foundation"]))

    assert (workspace / "foundation.txt").read_text(encoding="utf-8") == "ready\n"
    assert evidence["dependencies"] == [{"task_id": "foundation", "commit": commit}]
    assert _git(workspace, "merge-base", "--is-ancestor", commit, "HEAD", check=False).returncode == 0


def test_prepare_pins_one_base_commit_for_the_entire_run(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    store = _Store({})
    executor = _executor(tmp_path, store)

    first_workspace, _, first_evidence = executor.prepare(_task([], "first-child"))
    _git(repository, "checkout", "main")
    (repository / "later.txt").write_text("not in approved run base\n", encoding="utf-8")
    _git(repository, "add", "later.txt")
    _git(repository, "commit", "-m", "move main")
    second_workspace, _, second_evidence = executor.prepare(_task([], "second-child"))

    assert first_evidence["base_commit"] == second_evidence["base_commit"]
    assert not (first_workspace / "later.txt").exists()
    assert not (second_workspace / "later.txt").exists()
    assert [item["event"] for item in store.events].count("run_base_pinned") == 1


def test_prepare_merges_multiple_dependencies_in_contract_order(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = _dependency_commit(repository, "first", {"first.txt": "first\n"})
    second = _dependency_commit(repository, "second", {"second.txt": "second\n"})
    store = _Store({"first": _dependency("first", first), "second": _dependency("second", second)})

    workspace, _, evidence = _executor(tmp_path, store).prepare(_task(["second", "first"]))

    assert (workspace / "first.txt").exists()
    assert (workspace / "second.txt").exists()
    assert [item["task_id"] for item in evidence["dependencies"]] == ["second", "first"]
    assert all(
        _git(workspace, "merge-base", "--is-ancestor", commit, "HEAD", check=False).returncode == 0
        for commit in (first, second)
    )


def test_prepare_blocks_and_cleans_up_dependency_conflict(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = _dependency_commit(repository, "first", {"shared.txt": "first\n"})
    second = _dependency_commit(repository, "second", {"shared.txt": "second\n"})
    store = _Store({"first": _dependency("first", first), "second": _dependency("second", second)})
    executor = _executor(tmp_path, store)

    with pytest.raises(ExecutionError, match="dependency merge conflict before dispatch"):
        executor.prepare(_task(["first", "second"]))

    workspace = executor.workspaces_root / "run-12345678" / "combined-task"
    assert not workspace.exists()
    assert _git(
        repository,
        "show-ref",
        "--verify",
        "refs/heads/factory/run-1234/combined-task",
        check=False,
    ).returncode != 0
    assert store.events[-1]["event"] == "dependency_composition_failed"
    assert store.events[-1]["detail"]["conflicting_paths"] == ["shared.txt"]


def test_launch_persists_composition_evidence(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspaces" / "run-12345678" / "combined-task"
    workspace.mkdir(parents=True)
    composition = {
        "kind": "dependency_composition",
        "base_ref": "main",
        "base_commit": "a" * 40,
        "dependencies": [{"task_id": "foundation", "commit": "b" * 40}],
        "composed_commit": "c" * 40,
    }
    store = _Store({})
    executor = object.__new__(WorkerExecutor)
    executor.store = store
    executor.host_workspaces_root = "F:/factory-workspaces"
    executor.workspaces_root = tmp_path / "workspaces"
    executor.worker_image = "worker:test"
    executor.prepare = lambda task: (workspace, "factory/run/combined", composition)
    container = SimpleNamespace(id="container-1")
    executor.client = SimpleNamespace(
        containers=SimpleNamespace(run=lambda *args, **kwargs: container)
    )
    monkeypatch.setenv("CODEX_AUTH_VOLUME", "codex-auth")
    task = SimpleNamespace(
        id="combined-task",
        run_id="run-12345678",
        contract={
            "id": "combined-task",
            "title": "Combined task",
            "role": "backend",
            "objective": "Implement a bounded composed dependency task.",
            "acceptance_criteria": ["The implementation passes its gates."],
            "allowed_paths": ["src/"],
            "dependencies": ["foundation"],
            "commands": [],
        },
    )

    assert executor.launch(task) == "container-1"
    assert store.transitions[-1][1] == TaskStatus.RUNNING
    assert store.transitions[-1][2]["evidence"] == [composition]


def test_reconcile_keeps_composition_and_gate_evidence(tmp_path: Path) -> None:
    store = _Store({})
    executor = object.__new__(WorkerExecutor)
    executor.store = store
    container = SimpleNamespace(
        status="exited",
        attrs={"State": {"ExitCode": 0}},
        reload=lambda: None,
        logs=lambda stdout, stderr: b"worker complete",
    )
    executor.client = SimpleNamespace(
        containers=SimpleNamespace(get=lambda container_id: container)
    )
    worker_result = WorkerResult(
        outcome="completed",
        summary="Implemented the bounded task.",
        changes_made=True,
        acceptance_evidence=["Gate passed"],
        tests=[],
        blocking_reason=None,
    )
    executor.collect = lambda task: worker_result
    executor.quality_gates = lambda task: [{"command": "pytest -q", "returncode": 0}]
    executor.commit_task_changes = lambda task: "d" * 40
    executor.publish_pull_request = lambda task, result: None
    composition = {"kind": "dependency_composition", "composed_commit": "c" * 40}
    task = SimpleNamespace(
        id="combined-task",
        run_id="run-12345678",
        container_id="container-1",
        evidence=[composition],
        contract={"read_only": False},
    )

    result = executor.reconcile(task)

    assert result["state"] == "succeeded"
    assert store.transitions[-1][1] == TaskStatus.SUCCEEDED
    assert store.transitions[-1][2]["evidence"] == [
        composition,
        {"command": "pytest -q", "returncode": 0},
    ]
