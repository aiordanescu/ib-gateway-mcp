# syntax=docker/dockerfile:1.26.0@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
# ib-gateway-mcp: the MCP server over streamable HTTP, meant to run next to an
# ib-gateway-docker container (see examples/docker-compose.yml).
#
# Images are pinned by digest; the tag is there for readers and for Dependabot, which
# refreshes both on the FROM lines (not on the syntax line above, which is bumped by
# hand). The Python minor and the Debian release are part of the tag, so moving either
# is a deliberate change. uv stays in step with [tool.uv] required-version and CI.

FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv

# One pin for both stages: the virtualenv is built against the Python it runs on.
FROM python:3.12.14-slim-trixie@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS base

# --- build: resolve the locked dependencies into a virtualenv ------------------------
FROM base AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
# Dependencies first, so a code change reuses this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- runtime: the virtualenv on a slim Python, as a non-root user --------------------
FROM base
ARG UID=10001
# /audit belongs to the server's user, so a named volume mounted there (the compose
# example's IBKR_MCP_AUDIT_LOG=/audit/audit.jsonl) is writable by it.
RUN groupadd --system --gid "${UID}" mcp \
    && useradd --system --uid "${UID}" --gid "${UID}" --no-create-home \
       --shell /usr/sbin/nologin mcp \
    && install -d -o "${UID}" -g "${UID}" -m 0700 /audit
COPY --from=build /opt/venv /opt/venv
# The HTTP transport listens on every interface inside the container, which makes a
# bearer token mandatory (IBKR_MCP_AUTH_TOKEN or IBKR_MCP_AUTH_TOKEN_FILE).
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    IB_HOST=ib-gateway \
    IB_PORT=4004 \
    IBKR_MCP_TRANSPORT=http \
    IBKR_MCP_HTTP_HOST=0.0.0.0 \
    IBKR_MCP_HTTP_PORT=8000
# Numeric, so Kubernetes' runAsNonRoot can verify it.
USER ${UID}:${UID}
EXPOSE 8000
# Liveness only: /healthz answers while the server runs, even when the gateway is down
# (weekly re-login, 2FA), so a gateway outage does not restart this container. Exec
# form: a failed request raises, and Python exits with 1, which Docker reads as unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('IBKR_MCP_HTTP_PORT', '8000'), timeout=4)"]
# Declared last, so a new version or commit only changes this metadata layer. CI and
# releases pass both: --build-arg VERSION=<package version> --build-arg REVISION=<git sha>.
ARG VERSION=unknown
ARG REVISION=unknown
LABEL org.opencontainers.image.title="ib-gateway-mcp" \
      org.opencontainers.image.description="MCP server for the Interactive Brokers TWS API, next to ib-gateway-docker" \
      org.opencontainers.image.source="https://github.com/aiordanescu/ib-gateway-mcp" \
      org.opencontainers.image.url="https://github.com/aiordanescu/ib-gateway-mcp" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
ENTRYPOINT ["ib-gateway-mcp"]
