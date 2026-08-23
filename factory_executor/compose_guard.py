from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import Path

import docker
import yaml

from .executor_types import ExecutionError


COMPOSE_COMMAND_PREFIX = "docker compose "
HEALTH_COMMAND_PREFIX = "curl --fail --show-error http://localhost:8000/healthz/"
_COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")


class ComposeGuard:
    """Validate repository Compose semantics before using the brokered socket."""

    def __init__(self, client: docker.DockerClient, workspace: Path, run_id: str, task_id: str):
        self.client = client
        self.workspace = workspace
        self.run_id = run_id
        self.task_id = task_id
        safe_task = re.sub(r"[^a-z0-9]", "", task_id.lower())[:24]
        self.project = f"hf{run_id.replace('-', '')[:8]}{safe_task}"[:48]
        self.compose_file = self._discover()
        self.override_file = workspace / ".factory" / "compose-guard.override.yaml"
        self._validated_config: str | None = None

    def _discover(self) -> Path:
        present = [self.workspace / name for name in _COMPOSE_FILES if (self.workspace / name).is_file()]
        if len(present) != 1:
            raise ExecutionError("exactly one supported Compose file is required")
        return present[0]

    def _base_args(self, include_override: bool = True) -> list[str]:
        args = ["docker-compose", "--project-name", self.project, "-f", str(self.compose_file)]
        if include_override and self.override_file.exists():
            args.extend(["-f", str(self.override_file)])
        return args

    def _subprocess(self, args: list[str], timeout: int = 900) -> dict:
        started = time.monotonic()
        completed = subprocess.run(args, cwd=self.workspace, text=True, capture_output=True, timeout=timeout, check=False)
        return {
            "returncode": completed.returncode,
            "output": (completed.stdout + completed.stderr)[-8000:],
            "duration_ms": round((time.monotonic() - started) * 1000),
        }

    def validate(self) -> dict:
        rendered = self._subprocess([*self._base_args(include_override=False), "config"])
        if rendered["returncode"]:
            raise ExecutionError(f"Compose configuration is invalid: {rendered['output'][-1000:]}")
        model = yaml.safe_load(rendered["output"])
        if not isinstance(model, dict) or not isinstance(model.get("services"), dict):
            raise ExecutionError("Compose configuration must define services")
        for name, service in model["services"].items():
            if not isinstance(service, dict):
                raise ExecutionError(f"Compose service {name!r} is malformed")
            for denied in ("privileged", "devices", "cap_add", "network_mode", "pid", "ipc"):
                value = service.get(denied)
                if value not in (None, False, [], ""):
                    raise ExecutionError(f"Compose service {name!r} uses forbidden option {denied!r}")
            for volume in service.get("volumes") or []:
                source = volume.get("source") if isinstance(volume, dict) else str(volume).split(":", 1)[0]
                volume_type = volume.get("type") if isinstance(volume, dict) else None
                if volume_type == "bind" or source.startswith((".", "/", "~")) or re.match(r"^[A-Za-z]:", source):
                    raise ExecutionError(f"Compose service {name!r} contains a host bind mount")
                target = volume.get("target", "") if isinstance(volume, dict) else str(volume).split(":", 2)[1]
                if target == "/var/run/docker.sock":
                    raise ExecutionError("Compose services may not mount the Docker socket")
            for port in service.get("ports") or []:
                host_ip = port.get("host_ip") if isinstance(port, dict) else None
                text = str(port)
                if host_ip not in ("127.0.0.1", "::1") and not text.startswith(("127.0.0.1:", "[::1]:")):
                    raise ExecutionError(f"Compose service {name!r} must bind published ports to loopback")
            build = service.get("build")
            if build:
                build = {"context": build} if isinstance(build, str) else build
                context = Path(str(build.get("context", "."))).resolve()
                if context != self.workspace.resolve():
                    raise ExecutionError(f"Compose service {name!r} build context must be the task worktree")
                dockerfile = str(build.get("dockerfile", "Dockerfile"))
                if Path(dockerfile).is_absolute() or ".." in Path(dockerfile).parts:
                    raise ExecutionError(f"Compose service {name!r} Dockerfile must be repository-relative")

        override = {"services": {name: {
            "mem_limit": "2048m",
            "cpus": 1.5,
            "pids_limit": 256,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "labels": {"factory.run": self.run_id, "factory.task": self.task_id, "factory.managed": "true"},
        } for name in model["services"]}}
        self.override_file.write_text(yaml.safe_dump(override, sort_keys=True), encoding="utf-8")
        self._validated_config = rendered["output"]
        return rendered

    def run(self, command: str) -> dict:
        if self._validated_config is None:
            self.validate()
        suffix = shlex.split(command)[2:]
        if suffix == ["config"]:
            return {"returncode": 0, "output": self._validated_config or "", "duration_ms": 0}
        if suffix[:2] == ["up", "--build"]:
            return self._subprocess([*self._base_args(), "up", "--build", "-d", "--remove-orphans"])
        if suffix == ["ps"]:
            return self._subprocess([*self._base_args(), "ps"])
        if suffix == ["down"]:
            return self.down()
        if suffix[:2] == ["exec", "-T"]:
            return self._subprocess([*self._base_args(), *suffix])
        raise ExecutionError(f"Compose command has no guarded implementation: {command}")

    def web_network(self) -> str:
        containers = self.client.containers.list(all=True, filters={
            "label": [f"com.docker.compose.project={self.project}", "com.docker.compose.service=web"]
        })
        if len(containers) != 1:
            raise ExecutionError("guarded Compose project does not have exactly one web container")
        containers[0].reload()
        networks = list(containers[0].attrs["NetworkSettings"]["Networks"])
        if not networks:
            raise ExecutionError("web container has no Compose network")
        return networks[0]

    def down(self) -> dict:
        return self._subprocess([*self._base_args(), "down", "--remove-orphans", "--volumes"])
