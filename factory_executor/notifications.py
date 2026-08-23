"""Best-effort delivery of terminal task events to Hermes' native webhook."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

import httpx

from .store import Store, Task

logger = logging.getLogger(__name__)


def publish_terminal_task_event(task: Task, store: Store | None = None) -> None:
    """Publish one durable terminal transition without making execution depend on Hermes."""
    url = os.environ.get("HERMES_FACTORY_WEBHOOK_URL", "").strip()
    secret = os.environ.get("HERMES_FACTORY_WEBHOOK_SECRET", "")
    if not url or not secret:
        return

    result: dict[str, Any] = task.result if isinstance(task.result, dict) else {}
    payload = {
        "event_type": "factory_task_terminal",
        "run_id": task.run_id,
        "task_id": task.id,
        "role": task.contract.get("role"),
        "title": task.contract.get("title"),
        "status": task.status,
        "branch": task.branch,
        "commit": result.get("commit"),
        "pull_request": result.get("pull_request"),
        "summary": result.get("summary") or result.get("error"),
        "updated_at": task.updated_at.isoformat(),
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
    delivery_id = f"factory-{task.id}-{int(task.updated_at.timestamp())}"
    started = time.monotonic()
    try:
        response = httpx.post(url, content=body, headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Signature-V2": signature,
            "X-Request-ID": delivery_id,
        }, timeout=10)
        response.raise_for_status()
        logger.info("published Hermes terminal event for task %s", task.id)
        if store:
            store.record_event(task.run_id, "notification_delivered", {
                "delivery_id": delivery_id,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "status_code": response.status_code,
            }, task.id)
    except httpx.HTTPError:
        # The ledger transition is already durable. A temporary Hermes outage
        # must never turn a completed worker into a failed factory task.
        logger.exception("could not publish Hermes terminal event for task %s", task.id)
        if store:
            store.record_event(task.run_id, "notification_failed", {
                "delivery_id": delivery_id,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }, task.id)
