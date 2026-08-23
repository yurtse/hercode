#!/bin/sh
set -eu

: "${TASK_CONTRACT_PATH:=/task/.factory/contract.json}"
: "${TASK_RESULT_PATH:=/task/.factory/result.json}"
mkdir -p /state/codex /task/.factory
# Authentication data is copied into a per-task home. The source mount is
# read-only and no task shares this writable directory.
if [ -d /codex-auth ]; then
  cp -a /codex-auth/. /state/codex/ 2>/dev/null || true
fi
export CODEX_HOME=/state/codex

if ! codex exec --help | grep -q -- '--json'; then
  echo 'Installed Codex CLI does not support --json' >&2
  exit 64
fi

schema=/task/.factory/worker-result.schema.json
cat > "$schema" <<'JSON'
{"type":"object","additionalProperties":false,"properties":{"outcome":{"type":"string","enum":["completed","blocked","failed"]},"summary":{"type":"string"},"changes_made":{"type":"boolean"},"acceptance_evidence":{"type":"array","items":{"type":"string"}},"tests":{"type":"array","items":{"type":"object","additionalProperties":false,"properties":{"command":{"type":["string","null"]},"name":{"type":["string","null"]},"status":{"type":["string","null"]},"passed":{"type":["boolean","null"]},"output":{"type":["string","null"]},"details":{"type":["string","null"]}},"required":["command","name","status","passed","output","details"]}},"blocking_reason":{"type":["string","null"]}},"required":["outcome","summary","changes_made","acceptance_evidence","tests","blocking_reason"]}
JSON

prompt="You are a bounded Codex factory worker. Read this task contract, work only in /task and only on allowed_paths, preserve existing work, and run relevant checks. Do not access credentials or modify factory policy. Do not run git commit, git push, or alter Git configuration: the isolated worker cannot access shared Git metadata, and the executor will validate and create the task commit after successful gates. Your final response must match the supplied schema. Contract:\n$(cat "$TASK_CONTRACT_PATH")"
route="$(python3 - "$TASK_CONTRACT_PATH" <<'PY'
import json
import sys

contract = json.load(open(sys.argv[1], encoding="utf-8"))
route = contract.get("resolved_route")
if not isinstance(route, dict) or not isinstance(route.get("model"), str) or not isinstance(route.get("reasoning_effort"), str):
    raise SystemExit("Task has no executor-resolved model route")
print(route["model"])
print(route["reasoning_effort"])
PY
)"
model="$(printf '%s\n' "$route" | sed -n '1p')"
reasoning_effort="$(printf '%s\n' "$route" | sed -n '2p')"
exec codex exec --json --output-schema "$schema" --output-last-message "$TASK_RESULT_PATH" \
  --model "$model" --config "model_reasoning_effort=\"$reasoning_effort\"" \
  --approve-for-me --skip-git-repo-check "$prompt"
