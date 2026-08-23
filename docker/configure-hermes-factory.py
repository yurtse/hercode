"""Install factory MCP and webhook configuration through Hermes config.yaml."""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml


def main() -> None:
    config_path = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "config.yaml"
    existing = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    config = existing if isinstance(existing, dict) else {}
    config.setdefault("mcp_servers", {})["factory-executor"] = {
        "url": "http://factory-executor:8080/mcp/",
        "headers": {"X-Factory-Key": "${FACTORY_API_KEY}"},
        "enabled": True,
        "timeout": 60,
        "connect_timeout": 10,
    }

    webhook = config.setdefault("platforms", {}).setdefault("webhook", {})
    webhook["enabled"] = True
    extra = webhook.setdefault("extra", {})
    extra.update({"host": "0.0.0.0", "port": 8644})
    deliver = os.environ.get("FACTORY_NOTIFICATION_DELIVER", "log").strip().lower() or "log"
    deliver_chat_id = os.environ.get("FACTORY_NOTIFICATION_CHAT_ID", "").strip()
    route = {
        "events": ["factory_task_terminal"],
        "prompt": (
            "A trusted internal Factory Executor terminal task event arrived. "
            "Report this status without creating, approving, dispatching, retrying, "
            "cancelling, or modifying a run. Retrieve authoritative detail through "
            "the factory-executor MCP tools when needed.\n\n{__raw__}"
        ),
        "skills": ["software-factory"],
        "deliver": deliver,
    }
    if deliver_chat_id:
        route["deliver_extra"] = {"chat_id": deliver_chat_id}
    subscriptions_path = config_path.parent / "webhook_subscriptions.json"
    if subscriptions_path.exists():
        loaded = json.loads(subscriptions_path.read_text(encoding="utf-8"))
        subscriptions = loaded if isinstance(loaded, dict) else {}
    else:
        subscriptions = {}
    subscriptions["factory-notifications"] = route
    subscriptions_temporary = subscriptions_path.with_suffix(".json.tmp")
    subscriptions_temporary.write_text(
        json.dumps(subscriptions, indent=2) + "\n", encoding="utf-8"
    )
    subscriptions_temporary.replace(subscriptions_path)

    temporary = config_path.with_suffix(".yaml.tmp")
    temporary.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    temporary.replace(config_path)


if __name__ == "__main__":
    main()
