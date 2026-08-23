from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Role(StrEnum):
    ARCHITECT = "architect"
    BACKEND = "backend"
    FRONTEND = "frontend"
    QA = "qa"
    REVIEWER = "reviewer"


class TaskStatus(StrEnum):
    PLANNED = "planned"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class Limits(BaseModel):
    timeout_seconds: int = Field(default=1800, ge=60, le=7200)
    max_total_tokens: int = Field(default=150_000, ge=1_000, le=500_000)
    memory_mb: int = Field(default=4096, ge=512, le=16384)
    cpu_count: float = Field(default=2.0, gt=0, le=8)


class ModelRoute(BaseModel):
    model: Literal["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"]


class RuntimeContract(BaseModel):
    """User-approved, project-owned execution environment for quality gates."""

    version: Literal[1] = 1
    kind: Literal["python-uv"] = "python-uv"
    python_version: str = Field(pattern=r"^3\.(?:12|13)(?:\.\d+)?$")
    project_file: str = Field(default="pyproject.toml", pattern=r"^[A-Za-z0-9_.\-/]+$")
    lock_file: str = Field(default="uv.lock", pattern=r"^[A-Za-z0-9_.\-/]+$")
    bootstrap_allowed: bool = False

    @model_validator(mode="after")
    def clean_paths(self) -> "RuntimeContract":
        for value in (self.project_file, self.lock_file):
            if value.startswith(("/", "..")) or "\\" in value or "/../" in value:
                raise ValueError("runtime paths must be clean repository-relative POSIX paths")
        return self


class TaskContract(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    title: str = Field(min_length=3, max_length=160)
    role: Role
    objective: str = Field(min_length=10, max_length=6000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=12)
    allowed_paths: list[str] = Field(min_length=1, max_length=40)
    dependencies: list[str] = Field(default_factory=list)
    policy_tags: list[str] = Field(default_factory=list, max_length=8)
    commands: list[str] = Field(default_factory=list, max_length=24)
    limits: Limits = Field(default_factory=Limits)
    read_only: bool = False
    risk: Literal["normal", "high", "exceptional"] = "normal"
    risk_justification: str | None = Field(default=None, max_length=1200)
    resolved_route: ModelRoute | None = None
    runtime: RuntimeContract | None = None

    @model_validator(mode="after")
    def role_constraints(self) -> "TaskContract":
        if self.role in {Role.ARCHITECT, Role.REVIEWER} and not self.read_only:
            raise ValueError(f"{self.role} tasks must be read_only")
        if any(path.startswith(("/", "..")) or "\\" in path for path in self.allowed_paths):
            raise ValueError("allowed_paths must be clean repository-relative POSIX paths")
        if self.risk != "normal" and not self.risk_justification:
            raise ValueError("high and exceptional risks require a risk_justification")
        if self.commands and self.runtime is None:
            raise ValueError("tasks with quality commands require an approved runtime contract")
        if self.runtime and self.runtime.bootstrap_allowed and not self.read_only:
            owned = {path.rstrip("/") for path in self.allowed_paths}
            for artifact in (self.runtime.project_file, self.runtime.lock_file):
                if not any(artifact == path or artifact.startswith(path + "/") for path in owned):
                    raise ValueError(f"bootstrap task must own runtime artifact {artifact!r}")
        return self


class CreateRunRequest(BaseModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
    base_ref: str = Field(default="main", min_length=1, max_length=128)
    policy_profile: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{1,63}$")
    objective: str = Field(min_length=10, max_length=6000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=12)
    task_dag: list[TaskContract] = Field(min_length=1, max_length=32)


class WorkerResult(BaseModel):
    outcome: Literal["completed", "blocked", "failed"]
    summary: str = Field(min_length=1, max_length=6000)
    changes_made: bool
    acceptance_evidence: list[str]
    tests: list[dict]
    blocking_reason: str | None


QUALITY_GATE_COMMANDS = {
    "ruff format --check .",
    "ruff check .",
    "pytest -q",
    "python manage.py check",
    "python manage.py makemigrations --check --dry-run",
    "docker compose config",
    "docker compose up --build -d",
    "docker compose ps",
    "docker compose exec -T web python manage.py migrate --noinput",
    "docker compose exec -T web python manage.py check",
    "curl --fail --show-error http://localhost:8000/healthz/live",
    "curl --fail --show-error http://localhost:8000/healthz/ready",
    "docker compose down",
}


def validate_task_dag(tasks: list[TaskContract], max_workers: int) -> None:
    identifiers = {task.id for task in tasks}
    if len(identifiers) != len(tasks):
        raise ValueError("task IDs must be unique")
    if max_workers < 1 or max_workers > 3:
        raise ValueError("max_workers must be between 1 and 3")
    for task in tasks:
        unknown = set(task.dependencies) - identifiers
        if unknown:
            raise ValueError(f"task {task.id} has unknown dependencies: {sorted(unknown)}")
        if task.id in task.dependencies:
            raise ValueError(f"task {task.id} cannot depend on itself")
        if any(command not in QUALITY_GATE_COMMANDS for command in task.commands):
            raise ValueError(f"task {task.id} contains a command outside the quality-gate allowlist")
    # Independent code writers cannot claim a common path. Prefixes conflict
    # too, because a directory-level claim includes every child path.
    writers = [task for task in tasks if not task.read_only and task.role not in {Role.QA}]
    for index, left in enumerate(writers):
        for right in writers[index + 1 :]:
            if left.id in right.dependencies or right.id in left.dependencies:
                continue
            for a in left.allowed_paths:
                for b in right.allowed_paths:
                    if a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/"):
                        raise ValueError(f"parallel tasks {left.id} and {right.id} overlap at {a!r}/{b!r}")
    # Reject dependency cycles with a compact Kahn traversal.
    remaining = {task.id: set(task.dependencies) for task in tasks}
    while remaining:
        roots = {task_id for task_id, deps in remaining.items() if not deps}
        if not roots:
            raise ValueError("task dependencies contain a cycle")
        for task_id in roots:
            del remaining[task_id]
        for deps in remaining.values():
            deps.difference_update(roots)
