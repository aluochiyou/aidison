FROM ghcr.io/github/github-mcp-server@sha256:d5a18c04b92714c309eb46a2305087e91a4dbd80420f6e462656699f95093520 AS github-mcp

FROM python:3.12-slim AS runtime

ARG UV_VERSION=0.11.30

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir "uv==${UV_VERSION}" \
    && groupadd --system aidison \
    && useradd --system --gid aidison --create-home aidison

WORKDIR /app

COPY pyproject.toml uv.lock README.md alembic.ini config.yaml ./
COPY migrations ./migrations
COPY src ./src

RUN uv sync --frozen --no-dev \
    && mkdir -p /data/artifacts \
    && chown -R aidison:aidison /data/artifacts

COPY --from=github-mcp --chown=aidison:aidison /server/github-mcp-server /server/github-mcp-server

USER aidison

CMD ["uvicorn", "aidison.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
