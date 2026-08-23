FROM ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie AS uv
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl docker-compose && rm -rf /var/lib/apt/lists/*
COPY --from=uv /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/
RUN uv tool install ruff==0.15.10 && cp -L /root/.local/bin/ruff /usr/local/bin/ruff && chmod 0555 /usr/local/bin/ruff
# Debian's compose package is the v1 `docker-compose` command. This narrow
# wrapper preserves the approved `docker compose ...` contract without adding
# a general Docker CLI or daemon to the executor; it uses the brokered socket.
RUN printf '%s\n' '#!/bin/sh' \
    'if [ "$1" = compose ]; then shift; exec docker-compose "$@"; fi' \
    'echo "only docker compose is available to quality gates" >&2; exit 64' > /usr/local/bin/docker && chmod 0555 /usr/local/bin/docker
COPY pyproject.toml ./
COPY factory_executor ./factory_executor
COPY factory_policy ./factory_policy
RUN pip install --no-cache-dir .
RUN mkdir -p /factory-workspaces
# This is the intentionally narrow Docker-socket holder. It runs no model
# prompts and exposes only the authenticated broker API below.
EXPOSE 8080
CMD ["factory-executor"]
