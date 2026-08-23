import json

import pytest

from factory_executor.contracts import CreateRunRequest
from factory_executor.repository_policy import validate_repository_policy


CRITERIA = [
    "DEFAULT_AUTO_FIELD is django.db.models.BigAutoField.",
    "Application and master-data tables use database-generated bigint/integer primary keys.",
    "UUID primary keys are prohibited for application and master-data tables.",
    "Phase 0 excludes business-domain workflows.",
]


def _task(tag, role, paths, commands, dependencies=None, read_only=False):
    body = {
        "id": tag,
        "title": f"Policy task {tag}",
        "role": role,
        "objective": "Implement and verify the bounded repository policy requirement.",
        "acceptance_criteria": ["The repository policy requirement is satisfied."],
        "allowed_paths": paths,
        "dependencies": dependencies or [],
        "policy_tags": [tag],
        "commands": commands,
        "read_only": read_only,
    }
    if commands:
        body["runtime"] = {
            "version": 1,
            "kind": "python-uv",
            "python_version": "3.13",
            "bootstrap_allowed": tag == "bootstrap-foundation",
        }
    return body


def _policy():
    def rule(role, read_only, paths, commands, dependencies):
        return {
            "role": role,
            "read_only": read_only,
            "required_exact_allowed_paths": paths,
            "required_commands": commands,
            "direct_dependency_tags": dependencies,
        }

    return {
        "version": 1,
        "repository": "cloudkitchen",
        "policy_source": "test fixture",
        "primary_key_policy": {"django_default": "BigAutoField"},
        "profiles": {"phase0": {
            "description": "Test Phase 0 policy.",
            "mutable_tasks_require_commands": True,
            "required_run_acceptance_criteria": CRITERIA,
            "task_rules": {
                "bootstrap-foundation": rule(
                    "backend", False,
                    ["pyproject.toml", "uv.lock", "apps/identity/"],
                    ["pytest -q"], [],
                ),
                "docker-ci-runbook": rule(
                    "backend", False, ["Dockerfile"], ["docker compose config"],
                    ["bootstrap-foundation"],
                ),
                "health-bigint-tests": rule(
                    "backend", False, ["tests/"], ["pytest -q"],
                    ["bootstrap-foundation"],
                ),
                "foundation-qa": rule(
                    "qa", True, ["."], ["pytest -q"],
                    ["docker-ci-runbook", "health-bigint-tests"],
                ),
                "foundation-review": rule(
                    "reviewer", True, ["."], [], ["foundation-qa"],
                ),
            },
        }},
    }


def _request(policy):
    rules = policy["profiles"]["phase0"]["task_rules"]
    tasks = []
    ids = {tag: tag for tag in rules}
    for tag, rule in rules.items():
        tasks.append(_task(
            tag,
            rule["role"],
            rule["required_exact_allowed_paths"],
            rule["required_commands"],
            [ids[value] for value in rule["direct_dependency_tags"]],
            rule["read_only"],
        ))
    return {
        "repository": "cloudkitchen",
        "base_ref": "main",
        "policy_profile": "phase0",
        "objective": "Build the bounded CloudKitchen Phase 0 delivery foundation.",
        "acceptance_criteria": CRITERIA,
        "task_dag": tasks,
    }


def test_phase0_repository_policy_accepts_complete_plan(monkeypatch, tmp_path):
    repository = tmp_path / "cloudkitchen" / ".factory"
    repository.mkdir(parents=True)
    policy = _policy()
    (repository / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path))

    validate_repository_policy(CreateRunRequest.model_validate(_request(policy)))


def test_phase0_repository_policy_rejects_empty_mutable_commands(monkeypatch, tmp_path):
    repository = tmp_path / "cloudkitchen" / ".factory"
    repository.mkdir(parents=True)
    policy = _policy()
    (repository / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path))
    body = _request(policy)
    body["task_dag"][0]["commands"] = []
    body["task_dag"][0].pop("runtime")

    with pytest.raises(ValueError, match="non-empty quality commands"):
        validate_repository_policy(CreateRunRequest.model_validate(body))


def test_phase0_repository_policy_rejects_generic_foundation_path(monkeypatch, tmp_path):
    repository = tmp_path / "cloudkitchen" / ".factory"
    repository.mkdir(parents=True)
    policy = _policy()
    (repository / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path))
    body = _request(policy)
    body["task_dag"][0]["allowed_paths"] = [
        value for value in body["task_dag"][0]["allowed_paths"]
        if value != "apps/identity/"
    ] + ["apps/foundation/"]

    with pytest.raises(ValueError, match="exact allowed_paths"):
        validate_repository_policy(CreateRunRequest.model_validate(body))


def test_phase0_repository_policy_rejects_redundant_qa_dependency(monkeypatch, tmp_path):
    repository = tmp_path / "cloudkitchen" / ".factory"
    repository.mkdir(parents=True)
    policy = _policy()
    (repository / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path))
    body = _request(policy)
    qa = next(task for task in body["task_dag"] if task["policy_tags"] == ["foundation-qa"])
    qa["dependencies"].append("bootstrap-foundation")

    with pytest.raises(ValueError, match="must depend directly"):
        validate_repository_policy(CreateRunRequest.model_validate(body))
