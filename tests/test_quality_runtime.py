from pathlib import Path

from factory_executor.runtime import QualityRuntime


class _Container:
    def wait(self, timeout: int) -> dict:
        return {"StatusCode": 0}

    def logs(self, stdout: bool, stderr: bool) -> bytes:
        return b"ok"

    def remove(self, force: bool) -> None:
        return None


class _Containers:
    def __init__(self) -> None:
        self.kwargs = None

    def run(self, image: str, **kwargs):
        self.kwargs = kwargs
        return _Container()


class _Client:
    def __init__(self) -> None:
        self.containers = _Containers()


def test_quality_runtime_uses_writable_uv_cache(tmp_path: Path) -> None:
    client = _Client()
    workspace = tmp_path / "run" / "task"
    workspace.mkdir(parents=True)
    runtime = QualityRuntime(client, tmp_path, "F:/factory-workspaces")

    result = runtime._run_container("task", "run", workspace, ["uv", "sync", "--frozen"])

    assert result["returncode"] == 0
    assert client.containers.kwargs["read_only"] is True
    assert client.containers.kwargs["environment"]["UV_CACHE_DIR"] == "/tmp/uv-cache"
    assert "/tmp" in client.containers.kwargs["tmpfs"]
