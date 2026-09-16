# syntax=docker/dockerfile:1

# A plain slim base. This service makes one kind of outbound call -- fetching a JWKS
# document -- and writes one small file. The less there is in the process holding somebody's
# personal data, the less there is in it to go wrong.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, in their own layer: application edits then rebuild in seconds rather
# than re-resolving the whole tree.
COPY pyproject.toml uv.lock README.md ./
# git: uv fetches the family's client packages from tagged git sources.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
# The token exists only for this RUN, in git's process environment, never a layer.
# Without a secret, public sources are fetched anonymously.
RUN --mount=type=secret,id=github_token,required=false \
    if [ -s /run/secrets/github_token ]; then \
        export GIT_CONFIG_COUNT=1 \
          GIT_CONFIG_KEY_0="url.https://x-access-token:$(cat /run/secrets/github_token)@github.com/.insteadOf" \
          GIT_CONFIG_VALUE_0="https://github.com/"; \
    fi \
    && uv sync --no-install-project --no-dev

COPY src/ src/
RUN --mount=type=secret,id=github_token,required=false \
    if [ -s /run/secrets/github_token ]; then \
        export GIT_CONFIG_COUNT=1 \
          GIT_CONFIG_KEY_0="url.https://x-access-token:$(cat /run/secrets/github_token)@github.com/.insteadOf" \
          GIT_CONFIG_VALUE_0="https://github.com/"; \
    fi \
    && uv sync --no-dev

# The database holds personal data IN PLAINTEXT -- see docs/adr/0003. It is written at
# runtime and must never live in an image layer. 0700 on the directory because the file
# mode is the only access control there is, and a world-readable directory would leak the
# database's existence and size even if the file itself were unreadable.
RUN useradd --create-home --uid 10001 userapi \
    && mkdir -p /var/lib/user-api \
    && chown -R userapi:userapi /var/lib/user-api /app \
    && chmod 700 /var/lib/user-api
VOLUME ["/var/lib/user-api"]

USER userapi

ENV USER_API_HOST=0.0.0.0 \
    USER_API_PORT=8002 \
    USER_API_DATABASE_PATH=/var/lib/user-api/user.db \
    USER_API_LOG_FORMAT=json

EXPOSE 8002

# USER_API_KEYRING_JWKS_URL and USER_API_KEYRING_ISSUER are deliberately NOT set here.
# They name the deployment's keyring, and baking one in makes an image that silently
# verifies tokens against the wrong service if it is ever run somewhere else.

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8002/healthy', timeout=4).status == 200 else 1)"

CMD ["user-api"]
