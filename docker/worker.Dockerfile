FROM node:22-bookworm-slim AS node
RUN npm install --global @openai/codex

FROM ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git ripgrep && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex && \
    python --version | grep -q '^Python 3\.13\.' && codex --version
RUN uv tool install ruff==0.15.10 && \
    cp -L "$(command -v ruff)" /usr/local/bin/ruff.worker && \
    rm /usr/local/bin/ruff && mv /usr/local/bin/ruff.worker /usr/local/bin/ruff && \
    chmod 0555 /usr/local/bin/ruff
RUN useradd --create-home --uid 10002 worker && mkdir -p /task /state /tmp/worker && chown -R worker:worker /task /state /tmp/worker
COPY docker/worker-entrypoint.sh /usr/local/bin/worker-entrypoint
RUN chmod 0555 /usr/local/bin/worker-entrypoint
ENV UV_CACHE_DIR=/state/uv-cache \
    UV_PYTHON_INSTALL_DIR=/state/uv-python \
    UV_PROJECT_ENVIRONMENT=/state/project-venv \
    UV_LINK_MODE=copy \
    XDG_CACHE_HOME=/state/cache \
    XDG_DATA_HOME=/state/data
USER worker
WORKDIR /task
ENTRYPOINT ["/usr/local/bin/worker-entrypoint"]
