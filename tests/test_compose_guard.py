from pathlib import Path

import pytest

from factory_executor.compose_guard import ComposeGuard
from factory_executor.executor_types import ExecutionError


def guard_for(tmp_path: Path, rendered: str, monkeypatch) -> ComposeGuard:
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".factory").mkdir()
    guard = ComposeGuard(object(), tmp_path, "01234567-aaaa-bbbb-cccc-0123456789ab", "safe-task")
    monkeypatch.setattr(guard, "_subprocess", lambda *args, **kwargs: {
        "returncode": 0, "output": rendered, "duration_ms": 1,
    })
    return guard


def test_rejects_privileged_compose(monkeypatch, tmp_path):
    guard = guard_for(tmp_path, "services:\n  web:\n    image: example\n    privileged: true\n", monkeypatch)
    with pytest.raises(ExecutionError, match="privileged"):
        guard.validate()


def test_rejects_host_bind_mount(monkeypatch, tmp_path):
    guard = guard_for(tmp_path, "services:\n  web:\n    image: example\n    volumes:\n      - type: bind\n        source: /host\n        target: /data\n", monkeypatch)
    with pytest.raises(ExecutionError, match="bind mount"):
        guard.validate()


def test_accepts_bounded_named_volume_and_loopback_port(monkeypatch, tmp_path):
    guard = guard_for(tmp_path, "services:\n  web:\n    image: example\n    ports:\n      - target: 8000\n        published: 8000\n        host_ip: 127.0.0.1\n    volumes:\n      - type: volume\n        source: appdata\n        target: /data\n", monkeypatch)
    result = guard.validate()
    assert result["returncode"] == 0
    assert guard.override_file.is_file()
