from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from .contracts import ModelRoute, TaskContract

ALLOWED_MODELS = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
ALLOWED_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
POLICY_PATH = Path(__file__).resolve().parents[1] / "factory_policy" / "model-routing.json"


class RoutingPolicyError(ValueError):
    pass


@lru_cache(maxsize=4)
def load_policy(path_value: str | None = None) -> dict:
    path = Path(path_value or os.environ.get("FACTORY_MODEL_ROUTING_PATH", POLICY_PATH))
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutingPolicyError(f"unable to load model routing policy: {exc}") from exc
    if not isinstance(policy.get("roles"), dict) or not isinstance(policy.get("risk_profiles"), dict):
        raise RoutingPolicyError("model routing policy must contain roles and risk_profiles mappings")
    for section in [policy["roles"], *policy["risk_profiles"].values()]:
        if not isinstance(section, dict):
            raise RoutingPolicyError("model routing policy sections must be mappings")
        for role, route in section.items():
            if not isinstance(route, dict) or route.get("model") not in ALLOWED_MODELS or route.get("reasoning_effort") not in ALLOWED_EFFORTS:
                raise RoutingPolicyError(f"invalid route for role {role!r}")
    return policy


def resolve_route(task: TaskContract) -> ModelRoute:
    policy = load_policy()
    role = task.role.value
    baseline = policy["roles"].get(role)
    if baseline is None:
        raise RoutingPolicyError(f"model routing policy has no route for role {role!r}")
    override = policy["risk_profiles"].get(task.risk, {}).get(role, {})
    return ModelRoute.model_validate({**baseline, **override})


def resolve_task_routes(tasks: list[TaskContract]) -> list[TaskContract]:
    resolved: list[TaskContract] = []
    for task in tasks:
        if task.resolved_route is not None:
            raise RoutingPolicyError("resolved_route is executor-owned and must not be supplied by the caller")
        resolved.append(task.model_copy(update={"resolved_route": resolve_route(task)}))
    return resolved
