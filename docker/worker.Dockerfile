FROM ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie AS uv
FROM node:22-bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git python3 python-is-python3 python3-venv ripgrep && rm -rf /var/lib/apt/lists/* && \
    npm install --global @openai/codex
COPY --from=uv /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/
RUN uv tool install ruff==0.15.10 && cp -L /root/.local/bin/ruff /usr/local/bin/ruff && chmod 0555 /usr/local/bin/ruff
RUN useradd --create-home --uid 10002 worker && mkdir -p /task /state /tmp/worker && chown -R worker:worker /task /state /tmp/worker
COPY docker/worker-entrypoint.sh /usr/local/bin/worker-entrypoint
RUN chmod 0555 /usr/local/bin/worker-entrypoint
USER worker
WORKDIR /task
ENTRYPOINT ["/usr/local/bin/worker-entrypoint"]
