from __future__ import annotations

import os
import shlex
import time
from pathlib import Path

import docker

from .contracts import RuntimeContract, TaskContract
from .executor_types import ExecutionError


PYTHON_GATE_PREFIXES = ("python ", "pytest ", "ruff ")


class QualityRuntime:
    """Run project code in an unprivileged container with no Docker socket."""

    def __init__(self, client: docker.DockerClient, workspaces_root: Path, host_workspaces_root: str):
        self.client = client
        self.workspaces_root = workspaces_root
        self.host_workspaces_root = host_workspaces_root
        self.image = os.environ.get("QUALITY_RUNNER_IMAGE", "hermes-quality-runner:local")

    def validate_artifacts(self, workspace: Path, contract: TaskContract) -> RuntimeContract:
        runtime = contract.runtime
        if runtime is None:
            raise ExecutionError("quality commands require a runtime contract")
        for artifact in (runtime.project_file, runtime.lock_file):
            if not (workspace / artifact).is_file():
                raise ExecutionError(f"runtime artifact is missing after worker completion: {artifact}")
        return runtime

    def _host_workspace(self, workspace: Path) -> str:
        relative = workspace.relative_to(self.workspaces_root).as_posix()
        return f"{self.host_workspaces_root.rstrip('/\\\\')}/{relative}"

    def _run_container(self, task_id: str, run_id: str, workspace: Path, argv: list[str], *, network: str | None = None) -> dict:
        started = time.monotonic()
        container = self.client.containers.run(
            self.image,
            command=argv,
            entrypoint=[],
            detach=True,
            working_dir="/task",
            environment={
                "UV_PROJECT_ENVIRONMENT": "/task/.factory/runtime-venv",
                "UV_CACHE_DIR": "/tmp/uv-cache",
                "UV_LINK_MODE": "copy",
            },
            volumes={self._host_workspace(workspace): {"bind": "/task", "mode": "rw"}},
            network=network,
            read_only=True,
            tmpfs={"/tmp": "rw,noexec,nosuid,uid=10003,gid=10003,mode=700,size=256m"},
            mem_limit="4096m",
            nano_cpus=2_000_000_000,
            pids_limit=256,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            user="10003:10003",
            labels={"factory.run": run_id, "factory.task": task_id, "factory.component": "quality"},
        )
        try:
            status = container.wait(timeout=900)
            output = container.logs(stdout=True, stderr=True).decode("utf-8", "replace")[-8000:]
            return {
                "returncode": int(status.get("StatusCode", 1)),
                "output": output,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
        finally:
            try:
                container.remove(force=True)
            except docker.errors.DockerException:
                pass

    def prepare(self, task_id: str, run_id: str, workspace: Path, runtime: RuntimeContract) -> dict:
        cache_state = "warm" if (workspace / ".factory" / "runtime-venv").exists() else "cold"
        result = self._run_container(
            task_id,
            run_id,
            workspace,
            ["uv", "sync", "--frozen", "--project", "/task", "--python", runtime.python_version],
        )
        result["cache_state"] = cache_state
        return result

    def run_gate(self, task_id: str, run_id: str, workspace: Path, command: str) -> dict:
        argv = shlex.split(command)
        if command.startswith(PYTHON_GATE_PREFIXES):
            argv = ["uv", "run", "--frozen", "--no-sync", "--project", "/task", "--", *argv]
        return self._run_container(task_id, run_id, workspace, argv)

    def run_health_gate(self, task_id: str, run_id: str, workspace: Path, command: str, network: str) -> dict:
        argv = shlex.split(command)
        argv[-1] = argv[-1].replace("http://localhost:8000", "http://web:8000")
        return self._run_container(task_id, run_id, workspace, argv, network=network)
