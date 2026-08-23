from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .contracts import CreateRunRequest, Role, TaskContract


class TaskRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    read_only: bool
    required_exact_allowed_paths: list[str] = Field(default_factory=list)
    required_commands: list[str] = Field(default_factory=list)
    direct_dependency_tags: list[str] = Field(default_factory=list)


class PolicyProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    mutable_tasks_require_commands: bool = True
    required_run_acceptance_criteria: list[str] = Field(default_factory=list)
    task_rules: dict[str, TaskRule]


class RepositoryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    repository: str
    policy_source: str
    primary_key_policy: dict[str, str]
    profiles: dict[str, PolicyProfile]


def _repository_path(repository: str) -> Path:
    root = Path(os.environ.get("PROJECTS_ROOT", "/repositories")).resolve()
    candidate = (root / repository).resolve()
    if root not in candidate.parents:
        raise ValueError("repository policy path escapes PROJECTS_ROOT")
    return candidate


def load_repository_policy(repository: str) -> RepositoryPolicy | None:
    path = _repository_path(repository) / ".factory" / "policy.json"
    if not path.is_file():
        return None
    try:
        policy = RepositoryPolicy.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid repository factory policy: {exc}") from exc
    if policy.repository != repository:
        raise ValueError(
            f"repository policy names {policy.repository!r}, expected {repository!r}"
        )
    return policy


def public_repository_policy(repository: str) -> dict:
    policy = load_repository_policy(repository)
    if policy is None:
        return {"repository": repository, "policy_present": False}
    return {"policy_present": True, **policy.model_dump(mode="json")}


def _single_tag(task: TaskContract, known_tags: set[str]) -> str:
    selected = [tag for tag in task.policy_tags if tag in known_tags]
    unknown = set(task.policy_tags) - known_tags
    if unknown:
        raise ValueError(f"task {task.id} has unknown repository policy tags: {sorted(unknown)}")
    if len(selected) != 1:
        raise ValueError(f"task {task.id} must declare exactly one repository policy tag")
    return selected[0]


def validate_repository_policy(request: CreateRunRequest) -> None:
    policy = load_repository_policy(request.repository)
    if policy is None:
        return
    if request.policy_profile is None:
        raise ValueError("repository requires an explicit policy_profile")
    profile = policy.profiles.get(request.policy_profile)
    if profile is None:
        raise ValueError(f"unknown repository policy profile {request.policy_profile!r}")

    missing_criteria = set(profile.required_run_acceptance_criteria) - set(request.acceptance_criteria)
    if missing_criteria:
        raise ValueError(
            f"run is missing repository-required acceptance criteria: {sorted(missing_criteria)}"
        )

    known_tags = set(profile.task_rules)
    tasks_by_tag: dict[str, TaskContract] = {}
    for task in request.task_dag:
        tag = _single_tag(task, known_tags)
        if tag in tasks_by_tag:
            raise ValueError(f"repository policy tag {tag!r} is assigned more than once")
        tasks_by_tag[tag] = task

    missing_tags = known_tags - set(tasks_by_tag)
    if missing_tags:
        raise ValueError(f"run is missing repository-required task tags: {sorted(missing_tags)}")

    for tag, rule in profile.task_rules.items():
        task = tasks_by_tag[tag]
        if task.role != rule.role:
            raise ValueError(f"task {task.id} tagged {tag!r} must use role {rule.role}")
        if task.read_only != rule.read_only:
            raise ValueError(f"task {task.id} tagged {tag!r} has incorrect read_only setting")
        if profile.mutable_tasks_require_commands and not task.read_only and not task.commands:
            raise ValueError(f"mutable task {task.id} must have non-empty quality commands")
        missing_paths = set(rule.required_exact_allowed_paths) - set(task.allowed_paths)
        if missing_paths:
            raise ValueError(
                f"task {task.id} is missing repository-required exact allowed_paths: {sorted(missing_paths)}"
            )
        missing_commands = set(rule.required_commands) - set(task.commands)
        if missing_commands:
            raise ValueError(
                f"task {task.id} is missing repository-required commands: {sorted(missing_commands)}"
            )
        required_dependencies = {tasks_by_tag[value].id for value in rule.direct_dependency_tags}
        if set(task.dependencies) != required_dependencies:
            raise ValueError(
                f"task {task.id} must depend directly on {sorted(required_dependencies)}"
            )
