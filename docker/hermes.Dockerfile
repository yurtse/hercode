ARG HERMES_UPSTREAM_IMAGE=nousresearch/hermes-agent@sha256:0c718ed0e390a9b4d87dc7f1bcc5c6314920d9d380699346d3887c8a1de50e95
FROM ${HERMES_UPSTREAM_IMAGE}

# Thin derivative only: upstream retains PID 1, s6 supervision, fixed SQLite,
# CLI, dashboard, gateway, and dependency ownership. Factory configuration is
# installed through the upstream cont-init lifecycle.
# Upstream's 02-reconcile-profiles may restore config.yaml from the selected
# profile. Install factory config afterwards so MCP and webhook routes survive
# every container start without modifying upstream source.
COPY --chmod=0755 docker/hermes-factory-bootstrap.sh /etc/cont-init.d/099-factory-bootstrap
COPY --chmod=0755 docker/configure-hermes-factory.py /usr/local/bin/configure-hermes-factory
