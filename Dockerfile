# syntax=docker/dockerfile:1.7

FROM node:22.23.1-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 AS web-builder

WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.14.0-slim-bookworm@sha256:d13fa0424035d290decef3d575cea23d1b7d5952cdf429df8f5542c71e961576 AS python-builder

ARG UV_VERSION=0.7.20
ARG DEBIAN_MIRROR=http://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=http://deb.debian.org/debian-security
ARG PYPI_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PYPI_INDEX_URL} \
    UV_DEFAULT_INDEX=${PYPI_INDEX_URL} \
    UV_HTTP_TIMEOUT=120 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
RUN sed -i \
        "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g; \
         s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install --yes --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"
COPY pyproject.toml uv.lock alembic.ini README.md LICENSE NOTICE ./
COPY server ./server
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
        --reinstall-package alibabacloud-polardb-tool-agentic-server

FROM python:3.14.0-slim-bookworm@sha256:d13fa0424035d290decef3d575cea23d1b7d5952cdf429df8f5542c71e961576 AS runtime

ARG VERSION=0.0.6
ARG REVISION=unknown
LABEL org.opencontainers.image.title="Alibaba Cloud PolarDB Tool Agentic Server" \
      org.opencontainers.image.description="MCP and SQL-over-HTTP gateway for PolarDB MySQL" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.source="https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
RUN groupadd --gid 10001 pas \
    && useradd --uid 10001 --gid 10001 --no-create-home \
        --shell /usr/sbin/nologin pas \
    && mkdir -p /app/log /tmp /var/run/pas \
    && chown -R 10001:10001 /app/log /var/run/pas

COPY --from=python-builder /app/.venv /app/.venv
COPY --from=python-builder /src/server /app/server
COPY --from=python-builder /src/alembic.ini /app/alembic.ini
COPY --from=web-builder /src/web/dist /app/static
COPY LICENSE NOTICE README.md /app/

USER 10001:10001
EXPOSE 18760
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18760/readyz', timeout=3)"]
ENTRYPOINT ["pas"]
CMD ["serve"]
