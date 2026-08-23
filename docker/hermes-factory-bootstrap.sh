#!/bin/sh
set -eu

factory_home="${HERMES_HOME:-/opt/data}"
factory_uid="${HERMES_UID:-10000}"
factory_gid="${HERMES_GID:-10000}"

mkdir -p "${factory_home}/skills" "${factory_home}/workspace"
if [ -d /factory-context/skills/software-factory ]; then
  rm -rf "${factory_home}/skills/software-factory"
  cp -a /factory-context/skills/software-factory "${factory_home}/skills/software-factory"
fi
if [ -f /factory-context/AGENTS.md ]; then
  cp /factory-context/AGENTS.md "${factory_home}/workspace/AGENTS.md"
fi

/opt/hermes/.venv/bin/python /usr/local/bin/configure-hermes-factory
chown -R "${factory_uid}:${factory_gid}" "${factory_home}/skills/software-factory" "${factory_home}/workspace/AGENTS.md" "${factory_home}/config.yaml"
