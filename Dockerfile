# Multi-stage build. The build stage installs wheels (offline if ./wheels/
# exists - see scripts/download_wheels.sh); the final stage copies only the
# installed packages and the application code, runs as a non-root user, and
# never contains the database file.
#
# Pin the base image by digest for reproducible, air-gapped-safe builds.
# Replace the digest below with the one you've verified/pulled on a
# connected machine (`docker pull python:3.12-slim && docker inspect --format '{{index .RepoDigests 0}}' python:3.12-slim`):
# python:3.12-slim@sha256:REPLACE_WITH_VERIFIED_DIGEST

ARG BASE_IMAGE=python:3.12-slim

# ---------------------------------------------------------------------------
# Build stage
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS build

WORKDIR /build

COPY requirements.txt ./
# wheels/ only exists on the build context when scripts/download_wheels.sh
# has been run on a connected machine; this stage's output (the venv) is the
# only thing copied into the final image, so having wheels/ present here
# never ends up in the shipped image either way.
COPY wheels/ ./wheels/

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && if [ -d wheels ] && [ -n "$(ls -A wheels 2>/dev/null)" ]; then \
         echo "Installing from local wheels/ (offline build)"; \
         /opt/venv/bin/pip install --no-index --find-links=wheels -r requirements.txt; \
       else \
         echo "Installing from PyPI (online build)"; \
         /opt/venv/bin/pip install -r requirements.txt; \
       fi

# ---------------------------------------------------------------------------
# Final stage
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS final

# Non-root application user (fixed uid for predictable volume permissions).
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid appuser --no-create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Air-gapped-safe defaults; override at `docker run`/compose time as needed.
ENV DOCS_DIR=/app/database_docs \
    DATABASE_URL=sqlite:////data/db.sqlite \
    MCP_TRANSPORT=stdio \
    MCP_PORT=8000 \
    LLM_BASE_URL=http://localhost:8080/v1 \
    API_ALLOW_REMOTE_SPEC=false \
    SECURITY_HIDE_DATABASE_DETAILS=true \
    SECURITY_EXPOSE_SQL=false \
    SECURITY_EXPOSE_COLUMN_NAMES=false \
    SECURITY_EXPOSE_TABLE_NAMES=false

# App files are owned by root and NOT writable by the app user - the
# container only ever needs to read them.
COPY --chown=root:root main.py errors.py config.yaml init_hr_backend_db.py ./
COPY --chown=root:root db/ ./db/
COPY --chown=root:root nlp/ ./nlp/
COPY --chown=root:root api/ ./api/
COPY --chown=root:root mcp_server/ ./mcp_server/
COPY --chown=root:root utils/ ./utils/
COPY --chown=root:root database_docs/ ./database_docs/
# hr_backend/ (the separate LDAP/committees/sessions/evaluations/reports
# REST app - see hr_backend/app.py) ships in the same image as the MCP
# server since it shares this image's Python environment/dependencies;
# docker-compose.yml runs it as its own service with its own command.
COPY --chown=root:root hr_backend/ ./hr_backend/

# The database is NEVER copied into the image - it is always mounted at
# runtime (see docker-compose.yml, /data volume, read-only).

USER appuser

ENTRYPOINT ["python", "main.py"]
