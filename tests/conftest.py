from __future__ import annotations

import os
from pathlib import Path

import pytest

from factory_executor.routing import load_policy


# FastMCP's session manager intentionally owns one process lifespan. Individual
# TestClient instances create multiple lifespans in one pytest process.
os.environ["FACTORY_MCP_LIFESPAN_ENABLED"] = "false"


@pytest.fixture(autouse=True)
def configured_routing_policy(monkeypatch: pytest.MonkeyPatch):
    """Keep tests independent of the Docker-only policy mount."""
    policy = Path(__file__).parents[1] / "factory_policy" / "model-routing.json"
    monkeypatch.setenv("FACTORY_MODEL_ROUTING_PATH", str(policy))
    load_policy.cache_clear()
    yield
    load_policy.cache_clear()

