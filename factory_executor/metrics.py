from __future__ import annotations

from collections import Counter
from statistics import mean

import docker

from .store import Store


def _summary(values: list[float]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "mean_ms": None, "p95_ms": None, "max_ms": None}
    index = max(0, min(len(ordered) - 1, int(round(0.95 * len(ordered) + 0.5)) - 1))
    return {"count": len(values), "mean_ms": round(mean(values), 2), "p95_ms": ordered[index], "max_ms": ordered[-1]}


def collect_metrics(store: Store) -> dict:
    tasks = store.all_tasks()
    events = store.all_events()
    notification_ms = [float(event.detail["duration_ms"]) for event in events
                       if event.event == "notification_delivered" and "duration_ms" in event.detail]
    gate_ms: list[float] = []
    runtime_ms: dict[str, list[float]] = {"cold": [], "warm": []}
    for task in tasks:
        for item in task.evidence or []:
            if isinstance(item, dict) and "duration_ms" in item:
                gate_ms.append(float(item["duration_ms"]))
                state = item.get("cache_state")
                if state in runtime_ms:
                    runtime_ms[state].append(float(item["duration_ms"]))

    approved_at = {event.run_id: event.created_at for event in events if event.event == "plan_approved"}
    queue_ms = [(event.created_at - approved_at[event.run_id]).total_seconds() * 1000
                for event in events if event.event == "task_running" and event.run_id in approved_at]

    resources = []
    try:
        client = docker.from_env()
        for container in client.containers.list(filters={"label": "factory.component"}):
            stats = container.stats(stream=False)
            memory = stats.get("memory_stats", {})
            resources.append({
                "container_id": container.id[:12],
                "component": container.labels.get("factory.component"),
                "run_id": container.labels.get("factory.run"),
                "task_id": container.labels.get("factory.task"),
                "memory_usage_bytes": memory.get("usage"),
                "memory_limit_bytes": memory.get("limit"),
                "pids": stats.get("pids_stats", {}).get("current"),
            })
    except docker.errors.DockerException:
        resources = []

    return {
        "task_states": dict(Counter(str(task.status) for task in tasks)),
        "queue_latency": _summary(queue_ms),
        "gate_duration": _summary(gate_ms),
        "runtime_prepare_cold": _summary(runtime_ms["cold"]),
        "runtime_prepare_warm": _summary(runtime_ms["warm"]),
        "webhook_latency": _summary(notification_ms),
        "notification_failures": sum(event.event == "notification_failed" for event in events),
        "active_resources": resources,
        "notes": {
            "sqlite_lock_events": "collected by scripts/collect-load-metrics.ps1 from Hermes logs",
            "notification_tokens": "collected by scripts/collect-load-metrics.ps1 from Hermes session logs",
        },
    }
