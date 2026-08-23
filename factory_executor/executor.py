from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import docker

from .compose_guard import COMPOSE_COMMAND_PREFIX, HEALTH_COMMAND_PREFIX, ComposeGuard
from .contracts import TaskContract, TaskStatus, WorkerResult
from .executor_types import ExecutionError, QualityGateError
from .runtime import QualityRuntime
from .store import Store, Task

class WorkerExecutor:
    """The sole component allowed to create workers or publish branches."""

    def __init__(self, store: Store):
        self.store = store
        self.projects_root = Path(os.environ.get("PROJECTS_ROOT", "/repositories")).resolve()
        self.host_projects_root = os.environ.get("HOST_PROJECTS_ROOT", "").strip()
        self.workspaces_root = Path(os.environ.get("WORKSPACES_ROOT", "/factory-workspaces")).resolve()
        # This is a Docker Desktop *host* path (for example ``F:/hermes/...``),
        # not a path that exists inside the Linux executor container.
        self.host_workspaces_root = os.environ.get("HOST_WORKSPACES_ROOT", "").strip()
        self.worker_image = os.environ.get("CODEX_WORKER_IMAGE", "hermes-codex-worker:local")
        self.max_workers = min(3, max(1, int(os.environ.get("MAX_WORKERS", "3"))))
        self.client = docker.from_env()

    def repository_path(self, repository: str) -> Path:
        path = (self.projects_root / repository).resolve()
        if self.projects_root not in path.parents or not (path / ".git").exists():
            raise ExecutionError("repository must be an existing Git repository below PROJECTS_ROOT")
        return path

    def _run(self, args: list[str], cwd: Path) -> str:
        completed = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
        if completed.returncode:
            raise ExecutionError(f"git command failed: {' '.join(args)}: {completed.stderr[-1000:]}")
        return completed.stdout.strip()

    def _pinned_base_commit(self, run, repository: Path) -> str:
        """Resolve a run base once and reuse that immutable commit for every task."""
        for event in reversed(self.store.events_for_run(run.id)):
            if event.event != "run_base_pinned":
                continue
            commit = event.detail.get("base_commit")
            if not isinstance(commit, str) or not commit:
                raise ExecutionError("run has malformed pinned base evidence")
            return self._run(["git", "rev-parse", f"{commit}^{{commit}}"], repository)
        commit = self._run(["git", "rev-parse", f"{run.base_ref}^{{commit}}"], repository)
        self.store.record_event(
            run.id,
            "run_base_pinned",
            {"base_ref": run.base_ref, "base_commit": commit},
        )
        return commit

    def _dependency_commits(self, task: Task, repository: Path, base: str) -> list[dict[str, str]]:
        """Resolve successful direct dependencies to verified local commits."""
        inherited: list[dict[str, str]] = []
        for dependency_id in task.contract.get("dependencies", []):
            dependency = self.store.get_task(dependency_id)
            if dependency is None or dependency.run_id != task.run_id:
                raise ExecutionError(f"dependency {dependency_id} is not part of run {task.run_id}")
            if dependency.status != TaskStatus.SUCCEEDED:
                raise ExecutionError(f"dependency {dependency_id} has not succeeded")
            raw_commit = (dependency.result or {}).get("commit")
            if not isinstance(raw_commit, str) or not raw_commit:
                raise ExecutionError(f"dependency {dependency_id} has no authoritative commit evidence")
            commit = self._run(["git", "rev-parse", f"{raw_commit}^{{commit}}"], repository)
            ancestor = subprocess.run(
                ["git", "merge-base", "--is-ancestor", base, commit],
                cwd=repository,
                text=True,
                capture_output=True,
                check=False,
            )
            if ancestor.returncode != 0:
                raise ExecutionError(
                    f"dependency {dependency_id} commit {commit} is not descended from the approved base"
                )
            inherited.append({"task_id": dependency_id, "commit": commit})
        return inherited

    def _discard_prepared_worktree(self, repository: Path, workspace: Path, branch: str) -> None:
        """Remove only the validated disposable worktree/branch after composition failure."""
        if self.workspaces_root not in workspace.resolve().parents:
            raise ExecutionError("refusing to clean a worktree outside WORKSPACES_ROOT")
        subprocess.run(["git", "merge", "--abort"], cwd=workspace, capture_output=True, check=False)
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(workspace)],
            cwd=repository,
            capture_output=True,
            check=False,
        )
        subprocess.run(["git", "branch", "-D", branch], cwd=repository, capture_output=True, check=False)

    def _compose_dependencies(
        self,
        task: Task,
        run,
        repository: Path,
        workspace: Path,
        branch: str,
        base: str,
        inherited: list[dict[str, str]],
    ) -> dict:
        """Merge dependency commits in approved contract order before worker launch."""
        for dependency in inherited:
            completed = subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Hermes Factory",
                    "-c",
                    "user.email=factory@local",
                    "merge",
                    "--no-ff",
                    "--no-edit",
                    "-m",
                    f"factory({task.id}): inherit {dependency['task_id']}",
                    dependency["commit"],
                ],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode:
                conflicts = subprocess.run(
                    ["git", "diff", "--name-only", "--diff-filter=U"],
                    cwd=workspace,
                    text=True,
                    capture_output=True,
                    check=False,
                ).stdout.splitlines()
                detail = {
                    "base_ref": run.base_ref,
                    "base_commit": base,
                    "dependencies": inherited,
                    "failed_dependency": dependency,
                    "conflicting_paths": conflicts,
                }
                self.store.record_event(
                    task.run_id,
                    "dependency_composition_failed",
                    detail,
                    task_id=task.id,
                )
                self._discard_prepared_worktree(repository, workspace, branch)
                paths = ", ".join(conflicts) if conflicts else "unknown paths"
                raise ExecutionError(
                    f"dependency merge conflict before dispatch for {dependency['task_id']}: {paths}"
                )
        composed_commit = self._run(["git", "rev-parse", "HEAD"], workspace)
        return {
            "kind": "dependency_composition",
            "base_ref": run.base_ref,
            "base_commit": base,
            "dependencies": inherited,
            "composed_commit": composed_commit,
        }

    def prepare(self, task: Task) -> tuple[Path, str, dict]:
        run = self.store.get_run(task.run_id)
        if not run:
            raise ExecutionError("run no longer exists")
        repository = self.repository_path(run.repository)
        if self._run(["git", "status", "--porcelain"], repository):
            raise ExecutionError("repository is dirty; factory requires a clean base")
        base = self._pinned_base_commit(run, repository)
        inherited = self._dependency_commits(task, repository, base)
        branch = f"factory/{task.run_id[:8]}/{task.id}"
        workspace = self.workspaces_root / task.run_id / task.id
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if workspace.exists():
            # A launch can fail after the worktree was prepared but before a
            # worker container exists. Reuse only that exact, container-less
            # preparation state; never reuse a worktree from a prior worker.
            if task.worktree == str(workspace) and task.branch and not task.container_id:
                return workspace, task.branch, {
                    "kind": "dependency_composition",
                    "base_ref": run.base_ref,
                    "base_commit": base,
                    "dependencies": inherited,
                    "composed_commit": self._run(["git", "rev-parse", "HEAD"], workspace),
                }
            raise ExecutionError("worktree already exists; use recovery instead of dispatching again")
        self._run(["git", "worktree", "add", "-b", branch, str(workspace), base], repository)
        composition = self._compose_dependencies(
            task, run, repository, workspace, branch, base, inherited
        )
        # The worker deliberately runs as an unprivileged UID. Make only this
        # disposable worktree writable across the executor/worker boundary;
        # no repository root or shared credential directory is mounted there.
        for candidate in [workspace, *workspace.rglob("*")]:
            try:
                mode = candidate.stat().st_mode
                candidate.chmod(mode | stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH | (stat.S_IXGRP | stat.S_IXOTH if candidate.is_dir() else 0))
            except OSError as exc:
                raise ExecutionError(f"could not prepare writable task worktree: {exc}") from exc
        (workspace / ".factory").mkdir(exist_ok=True)
        control_dir = workspace / ".factory"
        # This directory is deliberately shared with the unprivileged worker
        # for its schema and structured result. It is created after the
        # worktree chmod loop above, so grant access explicitly here.
        control_dir.chmod(0o777)
        (control_dir / "contract.json").write_text(json.dumps(task.contract, indent=2), encoding="utf-8")
        return workspace, branch, composition

    def launch(self, task: Task) -> str:
        contract = TaskContract.model_validate(task.contract)
        if not self.host_workspaces_root:
            raise ExecutionError("HOST_WORKSPACES_ROOT must be configured for Docker worker mounts")
        workspace, branch, composition = self.prepare(task)
        if contract.runtime and not contract.runtime.bootstrap_allowed:
            for artifact in (contract.runtime.project_file, contract.runtime.lock_file):
                if not (workspace / artifact).is_file():
                    raise ExecutionError(f"approved runtime artifact is missing: {artifact}")
        relative = workspace.relative_to(self.workspaces_root).as_posix()
        # Docker Desktop requires host paths for socket-created bind mounts.
        # Do not use Path.resolve() or Path.exists() here: this value belongs
        # to the Docker host (Windows in this deployment), while this process
        # runs in a Linux container. Docker validates the host bind itself.
        host_workspace = f"{self.host_workspaces_root.rstrip('/\\\\')}/{relative}"
        auth_volume = os.environ.get("CODEX_AUTH_VOLUME", "codex-auth")
        container = self.client.containers.run(
            self.worker_image,
            detach=True,
            name=f"factory-{task.run_id[:8]}-{task.id}",
            working_dir="/task",
            environment={"TASK_CONTRACT_PATH": "/task/.factory/contract.json", "TASK_RESULT_PATH": "/task/.factory/result.json"},
            volumes={str(host_workspace): {"bind": "/task", "mode": "rw"}, auth_volume: {"bind": "/codex-auth", "mode": "ro"}},
            network_disabled=False,
            read_only=True,
            # Docker creates tmpfs mounts as root by default. Assign ownership
            # to the unprivileged worker so it can create its isolated Codex
            # home under /state without any privilege escalation.
            tmpfs={
                "/tmp": "rw,noexec,nosuid,uid=10002,gid=10002,mode=700,size=256m",
                "/state": "rw,nosuid,uid=10002,gid=10002,mode=700,size=256m",
            },
            mem_limit=f"{contract.limits.memory_mb}m",
            nano_cpus=int(contract.limits.cpu_count * 1_000_000_000),
            pids_limit=256,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            user="10002:10002",
            labels={"factory.run": task.run_id, "factory.task": task.id},
        )
        self.store.transition_task(
            task.id,
            TaskStatus.RUNNING,
            branch=branch,
            worktree=str(workspace),
            container_id=container.id,
            evidence=[composition],
        )
        return container.id

    def collect(self, task: Task) -> WorkerResult:
        if not task.worktree:
            raise ExecutionError("task has no worktree")
        result_path = Path(task.worktree) / ".factory" / "result.json"
        if not result_path.exists():
            raise ExecutionError("worker did not produce a structured result")
        return WorkerResult.model_validate_json(result_path.read_text(encoding="utf-8"))

    def quality_gates(self, task: Task) -> list[dict]:
        """Run gates outside the model and outside the socket-holding process."""
        if not task.worktree:
            raise ExecutionError("task has no worktree")
        contract = TaskContract.model_validate(task.contract)
        workspace = Path(task.worktree)
        runtime = QualityRuntime(self.client, self.workspaces_root, self.host_workspaces_root)
        runtime_contract = runtime.validate_artifacts(workspace, contract)
        evidence: list[dict] = []
        prepared = runtime.prepare(task.id, task.run_id, workspace, runtime_contract)
        evidence.append({"command": "runtime: uv sync --frozen", **prepared})
        if prepared["returncode"]:
            raise QualityGateError("locked project runtime preparation failed", evidence)

        commands = task.contract.get("commands", [])
        compose_guard: ComposeGuard | None = None
        try:
            for command in commands:
                if command.startswith(COMPOSE_COMMAND_PREFIX):
                    compose_guard = compose_guard or ComposeGuard(self.client, workspace, task.run_id, task.id)
                    result = compose_guard.run(command)
                elif command.startswith(HEALTH_COMMAND_PREFIX):
                    if compose_guard is None:
                        raise ExecutionError("health gate requires a guarded Compose project")
                    result = runtime.run_health_gate(task.id, task.run_id, workspace, command, compose_guard.web_network())
                else:
                    result = runtime.run_gate(task.id, task.run_id, workspace, command)
                evidence.append({"command": command, **result})
                if result["returncode"]:
                    raise QualityGateError(f"quality gate failed: {command}", evidence)
        finally:
            if compose_guard is not None and "docker compose down" not in commands:
                cleanup = compose_guard.down()
                evidence.append({"command": "factory cleanup", **cleanup})
        return evidence

    def commit_task_changes(self, task: Task) -> str:
        """Validate and commit worker changes from the executor-owned worktree.

        Git worktrees store shared metadata outside the mounted task directory.
        Keeping commits here preserves worker isolation and avoids mounting the
        repository's shared ``.git`` directory into disposable containers.
        """
        if not task.worktree:
            raise ExecutionError("task has no worktree")
        workspace = Path(task.worktree)
        if task.contract.get("read_only"):
            return self._run(["git", "rev-parse", "HEAD"], workspace)

        allowed_paths = [path.rstrip("/") for path in task.contract["allowed_paths"]]
        changed = self._run(["git", "status", "--porcelain", "--untracked-files=all"], workspace).splitlines()
        changed_paths: list[str] = []
        for line in changed:
            # Porcelain status is a two-character status field followed by a
            # space.  Use lstrip rather than a fixed third-column offset: some
            # Git states retain an extra separator before the pathname.
            path = line[2:].lstrip()
            # Renames use "old -> new" in porcelain output. Both locations
            # must be checked before staging so a task cannot move a file
            # outside its declared ownership boundary.
            candidates = [segment.strip() for segment in path.split(" -> ")]
            for candidate in candidates:
                if candidate == ".factory" or candidate.startswith(".factory/"):
                    continue
                if not any(candidate == allowed or candidate.startswith(allowed + "/") for allowed in allowed_paths):
                    raise ExecutionError(f"worker changed path outside allowed_paths: {candidate}")
                changed_paths.append(candidate)

        if not changed_paths:
            return self._run(["git", "rev-parse", "HEAD"], workspace)
        self._run(["git", "add", "-A", "--", *allowed_paths], workspace)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=workspace, check=False)
        if staged.returncode == 0:
            return self._run(["git", "rev-parse", "HEAD"], workspace)
        if staged.returncode != 1:
            raise ExecutionError("could not inspect staged task changes")
        self._run([
            "git", "-c", "user.name=Hermes Factory", "-c", "user.email=factory@local",
            "commit", "-m", f"factory({task.id}): {task.contract['title']}",
        ], workspace)
        return self._run(["git", "rev-parse", "HEAD"], workspace)

    def publish_pull_request(self, task: Task, result: WorkerResult) -> str | None:
        """Push with the executor credential only; workers never receive it."""
        token = os.environ.get("GITHUB_TOKEN")
        if not token or not task.branch or not task.worktree:
            return None
        run = self.store.get_run(task.run_id)
        if not run:
            raise ExecutionError("run missing during publication")
        workspace = Path(task.worktree)
        remote = self._run(["git", "remote", "get-url", "origin"], workspace)
        if "github.com" not in remote:
            raise ExecutionError("PR publication supports GitHub origins only")
        self._run(["git", "push", "origin", f"HEAD:refs/heads/{task.branch}"], workspace)
        # GitHub CLI is intentionally not installed in the worker. The broker
        # uses its token over HTTPS and returns the created PR URL as evidence.
        import urllib.request
        slug = remote.removesuffix(".git").split("github.com")[-1].lstrip(":/")
        data = json.dumps({"title": task.contract["title"], "head": task.branch, "base": run.base_ref,
                           "body": f"Hermes factory run `{task.run_id}`.\n\n{result.summary}"}).encode()
        request = urllib.request.Request(f"https://api.github.com/repos/{slug}/pulls", data=data,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())["html_url"]
        except Exception as exc:
            raise ExecutionError(f"branch pushed but PR creation failed: {exc}") from exc

    def reconcile(self, task: Task) -> dict:
        if not task.container_id:
            raise ExecutionError("task has no worker container")
        container = self.client.containers.get(task.container_id)
        container.reload()
        if container.status == "running":
            contract = TaskContract.model_validate(task.contract)
            started_text = container.attrs.get("State", {}).get("StartedAt", "")
            try:
                started = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                raise ExecutionError("worker container has malformed start-time evidence")
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            if elapsed > contract.limits.timeout_seconds:
                try:
                    container.kill()
                except docker.errors.DockerException:
                    pass
                logs = container.logs(stdout=True, stderr=True).decode("utf-8", "replace")[-8000:]
                self.store.transition_task(
                    task.id,
                    TaskStatus.FAILED,
                    result={
                        "error": f"worker exceeded timeout of {contract.limits.timeout_seconds} seconds",
                        "logs": logs,
                    },
                )
                return {"task_id": task.id, "state": "failed"}
            return {"task_id": task.id, "state": "running"}
        logs = container.logs(stdout=True, stderr=True).decode("utf-8", "replace")[-8000:]
        if container.attrs["State"].get("ExitCode") != 0:
            self.store.transition_task(task.id, TaskStatus.FAILED, result={"error": "worker process failed", "logs": logs})
            return {"task_id": task.id, "state": "failed"}
        result = self.collect(task)
        if result.outcome != "completed":
            self.store.transition_task(task.id, TaskStatus.BLOCKED, result=result.model_dump(mode="json"))
            return {"task_id": task.id, "state": "blocked"}
        try:
            evidence = [*(task.evidence or []), *self.quality_gates(task)]
            commit = self.commit_task_changes(task)
            pr_url = None if task.contract["read_only"] else self.publish_pull_request(task, result)
            payload = result.model_dump(mode="json") | {"commit": commit, "pull_request": pr_url}
            self.store.transition_task(task.id, TaskStatus.SUCCEEDED, result=payload, evidence=evidence)
            return {"task_id": task.id, "state": "succeeded", "pull_request": pr_url}
        except QualityGateError as exc:
            evidence = [*(task.evidence or []), *exc.evidence]
            self.store.transition_task(
                task.id,
                TaskStatus.FAILED,
                result={"error": str(exc), "worker": result.model_dump(mode="json")},
                evidence=evidence,
            )
            return {"task_id": task.id, "state": "failed"}
        except ExecutionError as exc:
            self.store.transition_task(task.id, TaskStatus.FAILED, result={"error": str(exc), "worker": result.model_dump(mode="json")})
            return {"task_id": task.id, "state": "failed"}
