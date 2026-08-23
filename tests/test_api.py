from fastapi.testclient import TestClient

from factory_executor import app as app_module
from factory_executor.store import Store


def payload():
    return {
        "repository": "demo/project",
        "base_ref": "main",
        "objective": "Add a bounded test feature to the demo project.",
        "acceptance_criteria": ["Tests pass"],
        "task_dag": [{
            "id": "api-tests", "title": "Add API tests", "role": "backend",
            "objective": "Implement focused API tests for the new endpoint.",
            "acceptance_criteria": ["pytest passes"], "allowed_paths": ["tests/api/"],
            "commands": ["pytest -q"],
            "runtime": {"version": 1, "kind": "python-uv", "python_version": "3.13"}
        }]
    }


def test_create_requires_key(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_API_KEY", "test-key")
    app_module.store = Store(f"sqlite:///{tmp_path / 'factory.db'}")
    with TestClient(app_module.app) as client:
        assert client.post("/v1/runs", json=payload()).status_code == 401
        response = client.post("/v1/runs", json=payload(), headers={"X-Factory-Key": "test-key"})
        assert response.status_code == 201
        assert response.json()["approved"] is False
        task_id = response.json()["tasks"][0]["id"]
        stored = client.get(f"/v1/tasks/{task_id}", headers={"X-Factory-Key": "test-key"}).json()
        assert stored["contract"]["resolved_route"] == {"model": "gpt-5.6-terra", "reasoning_effort": "medium"}


def test_dispatch_requires_explicit_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_API_KEY", "test-key")
    monkeypatch.setenv("MAX_WORKERS", "3")
    monkeypatch.setenv("FACTORY_DISPATCH_ENABLED", "true")
    app_module.store = Store(f"sqlite:///{tmp_path / 'factory.db'}")
    headers = {"X-Factory-Key": "test-key"}
    with TestClient(app_module.app) as client:
        run_id = client.post("/v1/runs", json=payload(), headers=headers).json()["id"]
        assert client.post(f"/v1/runs/{run_id}/dispatch", headers=headers).status_code == 409
        assert client.post(f"/v1/runs/{run_id}/approve", headers=headers).status_code == 200


def test_dispatch_freeze_is_enforced(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_API_KEY", "test-key")
    monkeypatch.setenv("FACTORY_DISPATCH_ENABLED", "false")
    app_module.store = Store(f"sqlite:///{tmp_path / 'factory.db'}")
    headers = {"X-Factory-Key": "test-key"}
    with TestClient(app_module.app) as client:
        run_id = client.post("/v1/runs", json=payload(), headers=headers).json()["id"]
        client.post(f"/v1/runs/{run_id}/approve", headers=headers)
        response = client.post(f"/v1/runs/{run_id}/dispatch", headers=headers)
        assert response.status_code == 503
        assert "frozen" in response.json()["detail"]


def test_mcp_requires_key(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_API_KEY", "test-key")
    app_module.store = Store(f"sqlite:///{tmp_path / 'factory.db'}")
    with TestClient(app_module.app) as client:
        assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).status_code == 401


def test_run_events_are_authenticated_and_incremental(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_API_KEY", "test-key")
    app_module.store = Store(f"sqlite:///{tmp_path / 'factory.db'}")
    headers = {"X-Factory-Key": "test-key"}
    with TestClient(app_module.app) as client:
        run_id = client.post("/v1/runs", json=payload(), headers=headers).json()["id"]
        response = client.get(f"/v1/runs/{run_id}/events", headers=headers)
        assert response.status_code == 200
        assert response.json()["events"][0]["event"] == "run_created"
        event_id = response.json()["events"][0]["id"]
        assert client.get(f"/v1/runs/{run_id}/events?after_id={event_id}", headers=headers).json()["events"] == []


def test_factory_policy_exposes_exact_commands_and_runtime_wrapper():
    policy = app_module.get_factory_policy()

    assert policy["command_matching"] == "exact"
    assert "ruff check ." in policy["quality_gate_commands"]
    assert "python manage.py check" in policy["quality_gate_commands"]
    assert "docker compose up --build -d" in policy["quality_gate_commands"]
    assert policy["runtime_requirements"]["python_uv_wrapped_prefixes"] == [
        "python ",
        "pytest ",
        "ruff ",
    ]
