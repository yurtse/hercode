"""Regression coverage for the strict Codex worker output schema."""

from __future__ import annotations

import json
import re
from pathlib import Path


def _worker_schema() -> dict:
    entrypoint = (Path(__file__).parents[1] / "docker" / "worker-entrypoint.sh").read_text(encoding="utf-8")
    match = re.search(r"cat > \"\$schema\" <<'JSON'\n(.*?)\nJSON", entrypoint, re.DOTALL)
    assert match, "worker output schema heredoc was not found"
    return json.loads(match.group(1))


def _object_nodes(schema: object):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for value in schema.values():
            yield from _object_nodes(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _object_nodes(value)


def test_worker_output_schema_is_strict_at_every_object_node() -> None:
    schema = _worker_schema()
    objects = list(_object_nodes(schema))
    assert objects
    assert all(node.get("additionalProperties") is False for node in objects)
    assert all(set(node.get("required", [])) == set(node.get("properties", {})) for node in objects)


def test_worker_output_schema_keeps_structured_test_evidence() -> None:
    schema = _worker_schema()
    test_item = schema["properties"]["tests"]["items"]
    assert test_item["additionalProperties"] is False
    assert {"command", "name", "status", "passed", "output", "details"} <= set(test_item["properties"])
    assert set(test_item["required"]) == set(test_item["properties"])
    assert test_item["properties"]["command"]["type"] == ["string", "null"]
