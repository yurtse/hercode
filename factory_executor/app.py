from __future__ import annotations

import os
import asyncio
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, status
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from .contracts import (
    QUALITY_GATE_COMMANDS,
    CreateRunRequest,
    TaskContract,
    TaskStatus,
    validate_task_dag,
)
from .executor import ExecutionError, WorkerExecutor
from .metrics import collect_metrics
from .notifications import publish_terminal_task_event
from .routing import RoutingPolicyError, resolve_task_routes
from .repository_policy import public_repository_policy, validate_repository_policy
from .store import Store

store = Store()
logger = logging.getLogger(__name__)
reconcile_lock = threading.Lock()
factory_mcp = FastMCP(
    "Hermes Factory Executor",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        allowed_hosts=["factory-executor:8080", "localhost:8080", "127.0.0.1:8080"],
    ),
)


def dispatch_enabled() -> bool:
    return os.environ.get("FACTORY_DISPATCH_ENABLED", "false").lower() == "true"


def require_key(x_factory_key: str = Header(default="")) -> None:
    if x_factory_key != os.environ.get("FACTORY_API_KEY") or not x_factory_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid factory API key")


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.create_schema()
    stop_monitor = asyncio.Event()
    monitor = asyncio.create_task(worker_lifecycle_monitor(stop_monitor))
    if os.environ.get("FACTORY_MCP_LIFESPAN_ENABLED", "true").lower() != "true":
        try:
            yield
        finally:
            stop_monitor.set()
            await monitor
        return
    async with factory_mcp.session_manager.run():
        try:
            yield
        finally:
            stop_monitor.set()
            await monitor


app = FastAPI(title="Hermes Factory Executor", version="0.1.0", lifespan=lifespan)


def serialize_run(run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    tasks = store.tasks_for_run(run_id)
    states = {str(task.status) for task in tasks}
    derived_status = run.status
    if tasks and all(task.status == TaskStatus.SUCCEEDED for task in tasks):
        derived_status = "succeeded"
    elif TaskStatus.RUNNING in states:
        derived_status = "running"
    elif states & {TaskStatus.FAILED, TaskStatus.BLOCKED}:
        derived_status = "blocked"
    return {"id": run.id, "repository": run.repository, "base_ref": run.base_ref, "objective": run.objective,
            "approved": run.approved, "status": derived_status,
            "tasks": [{"id": task.id, "role": task.contract["role"], "status": task.status, "branch": task.branch,
                       "container_id": task.container_id, "result": task.result} for task in tasks]}


def validate_stored_repository_policy(run_id: str) -> None:
    """Recheck policy at approval/dispatch so pre-policy plans cannot bypass it."""
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    created = next(
        (event for event in store.events_for_run(run_id) if event.event == "run_created"),
        None,
    )
    policy_profile = (created.detail or {}).get("policy_profile") if created else None
    request = CreateRunRequest(
        repository=run.repository,
        base_ref=run.base_ref,
        objective=run.objective,
        acceptance_criteria=run.acceptance_criteria,
        task_dag=[TaskContract.model_validate(task.contract) for task in store.tasks_for_run(run_id)],
        policy_profile=policy_profile,
    )
    try:
        validate_repository_policy(request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=f"stored run violates repository policy: {exc}") from exc


def reconcile_tasks(tasks) -> list[dict]:
    """Reconcile a snapshot of running tasks under one process-wide lock."""
    with reconcile_lock:
        if not tasks:
            return []
        executor = WorkerExecutor(store)
        results = []
        for task in tasks:
            if task.status != TaskStatus.RUNNING:
                continue
            try:
                result = executor.reconcile(task)
            except ExecutionError as exc:
                store.transition_task(task.id, TaskStatus.FAILED, result={"error": str(exc)})
                result = {"task_id": task.id, "state": "failed"}
            results.append(result)
            if result.get("state") != "running":
                terminal_task = store.get_task(task.id)
                if terminal_task:
                    publish_terminal_task_event(terminal_task, store)
        return results


async def worker_lifecycle_monitor(stop_monitor: asyncio.Event) -> None:
    """Turn Docker worker exits into durable task transitions without chat polling."""
    interval = max(1, min(60, int(os.environ.get("WORKER_MONITOR_SECONDS", "5"))))
    while not stop_monitor.is_set():
        try:
            results = await asyncio.to_thread(reconcile_tasks, store.running_tasks())
            for result in results:
                if result.get("state") != "running":
                    logger.info("worker lifecycle reconciled: %s", result)
        except Exception:
            logger.exception("worker lifecycle monitor failed; will retry")
        try:
            await asyncio.wait_for(stop_monitor.wait(), timeout=interval)
        except TimeoutError:
            pass


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "dispatch_enabled": dispatch_enabled()}


@app.get("/v1/metrics", dependencies=[Depends(require_key)])
def get_metrics() -> dict:
    return collect_metrics(store)


def get_factory_policy() -> dict:
    """Return the executor-enforced, read-only task-contract policy surface."""
    return {
        "version": 1,
        "quality_gate_commands": sorted(QUALITY_GATE_COMMANDS),
        "command_matching": "exact",
        "runtime_requirements": {
            "commands_require_runtime_contract": True,
            "supported_kinds": ["python-uv"],
            "python_versions": ["3.12", "3.13"],
            "python_uv_wrapped_prefixes": ["python ", "pytest ", "ruff "],
            "wrapper": "uv run --frozen --no-sync --project /task --",
        },
        "compose": {
            "guarded_prefix": "docker compose ",
            "health_prefix": "curl --fail --show-error http://localhost:8000/healthz/",
        },
        "limits": {"maximum_concurrent_workers": 3, "maximum_commands_per_task": 24},
    }


@app.post("/v1/runs", dependencies=[Depends(require_key)], status_code=201)
def create_run(request: CreateRunRequest) -> dict:
    try:
        validate_task_dag(request.task_dag, int(os.environ.get("MAX_WORKERS", "3")))
        validate_repository_policy(request)
        resolved_tasks = resolve_task_routes(request.task_dag)
    except (ValueError, RoutingPolicyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    run = store.create_run(request.model_copy(update={"task_dag": resolved_tasks}))
    return serialize_run(run.id)


@app.get("/v1/runs/{run_id}", dependencies=[Depends(require_key)])
def get_run(run_id: str) -> dict:
    return serialize_run(run_id)


@app.get("/v1/runs/{run_id}/events", dependencies=[Depends(require_key)])
def get_run_events(run_id: str, after_id: int = 0) -> dict:
    if after_id < 0:
        raise HTTPException(status_code=422, detail="after_id must be non-negative")
    if not store.get_run(run_id):
        raise HTTPException(status_code=404, detail="run not found")
    events = store.events_for_run(run_id, after_id)
    return {"run_id": run_id, "events": [
        {"id": event.id, "task_id": event.task_id, "event": event.event,
         "detail": event.detail, "created_at": event.created_at.isoformat()}
        for event in events
    ]}


@app.post("/v1/runs/{run_id}/approve", dependencies=[Depends(require_key)])
def approve_plan(run_id: str) -> dict:
    validate_stored_repository_policy(run_id)
    if not store.approve(run_id):
        raise HTTPException(status_code=404, detail="run not found")
    return serialize_run(run_id)


@app.post("/v1/runs/{run_id}/dispatch", dependencies=[Depends(require_key)])
def dispatch_run(run_id: str) -> dict:
    if not dispatch_enabled():
        raise HTTPException(status_code=503, detail="factory dispatch is frozen for maintenance")
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    if not run.approved:
        raise HTTPException(status_code=409, detail="explicit plan approval is required before dispatch")
    validate_stored_repository_policy(run_id)
    tasks = store.tasks_for_run(run_id)
    active = sum(task.status == TaskStatus.RUNNING for task in tasks)
    launched: list[dict] = []
    executor = WorkerExecutor(store)
    for task in tasks:
        completed = {candidate.id for candidate in tasks if candidate.status == TaskStatus.SUCCEEDED}
        if active >= executor.max_workers:
            break
        if task.status != TaskStatus.READY or not set(task.contract.get("dependencies", [])).issubset(completed):
            continue
        try:
            launched.append({"task_id": task.id, "container_id": executor.launch(task)})
            active += 1
        except ExecutionError as exc:
            store.transition_task(task.id, TaskStatus.BLOCKED, result={"error": str(exc)})
            terminal_task = store.get_task(task.id)
            if terminal_task:
                publish_terminal_task_event(terminal_task, store)
    return {"run_id": run_id, "launched": launched, "run": serialize_run(run_id)}


@app.post("/v1/runs/{run_id}/reconcile", dependencies=[Depends(require_key)])
def reconcile_run(run_id: str) -> dict:
    if not store.get_run(run_id):
        raise HTTPException(status_code=404, detail="run not found")
    # Keep manual reconciliation available, while the lifecycle monitor makes
    # it unnecessary for ordinary worker completion/failure detection.
    results = reconcile_tasks(store.tasks_for_run(run_id))
    return {"results": results, "run": serialize_run(run_id)}


@app.get("/v1/tasks/{task_id}", dependencies=[Depends(require_key)])
def get_task(task_id: str) -> dict:
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return {"id": task.id, "run_id": task.run_id, "contract": task.contract, "status": task.status,
            "branch": task.branch, "worktree": task.worktree, "result": task.result, "evidence": task.evidence}


@app.post("/v1/tasks/{task_id}/cancel", dependencies=[Depends(require_key)])
def cancel_task(task_id: str) -> dict:
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.container_id:
        try:
            WorkerExecutor(store).client.containers.get(task.container_id).kill()
        except Exception:
            pass
    store.transition_task(task_id, TaskStatus.CANCELLED)
    return get_task(task_id)


class RepairRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=3000)


@app.post("/v1/tasks/{task_id}/repair", dependencies=[Depends(require_key)])
def request_repair(task_id: str, request: RepairRequest) -> dict:
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.contract["role"] != "qa" or task.status not in {TaskStatus.FAILED, TaskStatus.BLOCKED}:
        raise HTTPException(status_code=409, detail="only failed QA tasks may request a bounded repair")
    return {"task_id": task_id, "status": "requires_supervisor_decomposition", "reason": request.reason}


class MCPAuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
            expected = os.environ.get("FACTORY_API_KEY", "")
            if not expected or headers.get("x-factory-key") != expected:
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"detail":"invalid factory API key"}'})
                return
        await self.app(scope, receive, send)


@factory_mcp.tool(name="create_run")
def mcp_create_run(repository: str, base_ref: str, objective: str,
                   acceptance_criteria: list[str], task_dag: list[TaskContract],
                   policy_profile: str | None = None) -> dict:
    """Create a validated, unapproved software-factory run."""
    return create_run(CreateRunRequest(repository=repository, base_ref=base_ref, objective=objective,
                                       acceptance_criteria=acceptance_criteria, task_dag=task_dag,
                                       policy_profile=policy_profile))


@factory_mcp.tool(name="approve_plan")
def mcp_approve_plan(run_id: str) -> dict:
    """Record explicit human approval for an existing planned run."""
    return approve_plan(run_id)


@factory_mcp.tool(name="dispatch_run")
def mcp_dispatch_run(run_id: str) -> dict:
    """Dispatch eligible tasks when maintenance freeze is disabled."""
    return dispatch_run(run_id)


@factory_mcp.tool(name="get_run")
def mcp_get_run(run_id: str) -> dict:
    """Retrieve authoritative run and task status."""
    return get_run(run_id)


@factory_mcp.tool(name="get_run_events")
def mcp_get_run_events(run_id: str, after_id: int = 0) -> dict:
    """Retrieve durable lifecycle events after a ledger event ID."""
    return get_run_events(run_id, after_id)


@factory_mcp.tool(name="get_task")
def mcp_get_task(task_id: str) -> dict:
    """Retrieve a task contract, result, and deterministic evidence."""
    return get_task(task_id)


@factory_mcp.tool(name="cancel_task")
def mcp_cancel_task(task_id: str) -> dict:
    """Cancel one task without expanding its scope."""
    return cancel_task(task_id)


@factory_mcp.tool(name="request_repair")
def mcp_request_repair(task_id: str, reason: str) -> dict:
    """Request one bounded repair for an eligible failed QA task."""
    return request_repair(task_id, RepairRequest(reason=reason))


@factory_mcp.tool(name="get_factory_metrics")
def mcp_get_factory_metrics() -> dict:
    """Retrieve factory latency, gate, notification, and resource measurements."""
    return get_metrics()


@factory_mcp.tool(name="get_factory_policy")
def mcp_get_factory_policy() -> dict:
    """Retrieve exact accepted commands and runtime contract requirements."""
    return get_factory_policy()


@factory_mcp.tool(name="get_repository_policy")
def mcp_get_repository_policy(repository: str) -> dict:
    """Retrieve the machine-enforced policy committed by a managed repository."""
    return public_repository_policy(repository)


app.mount("/mcp", MCPAuthMiddleware(factory_mcp.streamable_http_app()))


def main() -> None:
    import uvicorn
    uvicorn.run("factory_executor.app:app", host="0.0.0.0", port=8080, reload=False)
