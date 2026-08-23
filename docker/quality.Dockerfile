FROM ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git && rm -rf /var/lib/apt/lists/* && \
    useradd --create-home --uid 10003 quality && mkdir -p /task && chown quality:quality /task
USER quality
WORKDIR /task
